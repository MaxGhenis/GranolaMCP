"""
Granola **official public API** client (``public-api.granola.ai``).

This is the supported, versioned Granola API, authenticated with a long-lived
personal/workspace API key (``grn_...``). Unlike the reverse-engineered
internal API (``api.granola.ai`` in :mod:`granola_mcp.core.api_client`), it does
**not** depend on the desktop app's rotating bearer token or on decrypting the
local cache — so it keeps working after Granola changes its on-disk storage
(e.g. the 2026 ``storage.dek`` removal that broke the local-cache/DEK path).

Key resolution order:

1. ``GRANOLA_API_KEY`` environment variable.
2. macOS keychain generic-password, service ``GRANOLA_API_KEY`` (account from
   ``GRANOLA_API_KEY_ACCOUNT`` env, else the current user, else any match).

Get a key at https://www.granola.ai (Business/Enterprise workspaces) → API keys.

Endpoints used (see https://docs.granola.ai):
* ``GET /v1/notes``            — list notes (paginated via ``cursor``).
* ``GET /v1/notes/{id}``       — a single note; ``?include=transcript`` attaches
  the diarized transcript. Also returns ``summary_markdown`` / ``attendees``.

The API only returns notes that already have a generated AI summary and
transcript — i.e. **finished** meetings. In-progress/live meetings are not
exposed here (they live only in the encrypted local cache).

Stdlib-only HTTP to keep the project dependency-light.
"""

import getpass
import gzip
import http.client
import json
import logging
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

PUBLIC_API_BASE = os.environ.get("GRANOLA_PUBLIC_API_BASE", "https://public-api.granola.ai")
KEY_SERVICE = os.environ.get("GRANOLA_API_KEY_SERVICE", "GRANOLA_API_KEY")
logger = logging.getLogger(__name__)


class GranolaPublicAPIError(Exception):
    """Raised when a Granola public API request fails."""


class GranolaPublicAPINotFoundError(GranolaPublicAPIError):
    """Raised when the public API definitively reports a missing note."""


def _normalize_api_key(value: Any) -> Optional[str]:
    """Return a stripped public API key when it has the documented prefix."""
    if not isinstance(value, str):
        return None
    key = value.strip()
    return key if len(key) > len("grn_") and key.startswith("grn_") else None


def _keychain_api_key() -> Optional[str]:
    """Read the ``grn_`` API key from the macOS keychain, if present."""
    accounts: List[Optional[str]] = []
    env_acct = os.environ.get("GRANOLA_API_KEY_ACCOUNT")
    if env_acct and env_acct.strip():
        accounts.append(env_acct.strip())
    try:
        accounts.append(getpass.getuser())
    except Exception:
        pass
    accounts.append(None)  # service-only match (first item for that service)

    seen = set()
    for acct in accounts:
        if acct in seen:
            continue
        seen.add(acct)
        cmd = ["security", "find-generic-password", "-w", "-s", KEY_SERVICE]
        if acct:
            cmd += ["-a", acct]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            return None
        except (subprocess.TimeoutExpired, PermissionError, OSError, UnicodeError):
            # A single account lookup can fail while a later account or the
            # service-only lookup remains usable.
            continue
        if out.returncode == 0:
            key = _normalize_api_key(out.stdout)
            if key:
                return key
    return None


def resolve_api_key() -> Optional[str]:
    """Return the Granola public API key from env or keychain (or None)."""
    return _normalize_api_key(os.environ.get("GRANOLA_API_KEY")) or _keychain_api_key()


def has_api_key() -> bool:
    """Cheap check used for backend auto-selection."""
    return bool(resolve_api_key())


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


class GranolaPublicAPI:
    """Minimal read-only client for the official Granola public API."""

    _RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

    def __init__(self, key_provider: Optional[Callable[[], Optional[str]]] = None,
                 timeout: Optional[int] = None, retries: Optional[int] = None,
                 backoff: float = 1.5, requests_per_second: float = 5.0):
        self._key_provider = key_provider or resolve_api_key
        self.timeout = timeout if timeout is not None else _env_int("GRANOLA_HTTP_TIMEOUT", 60)
        self.retries = max(1, retries if retries is not None else _env_int("GRANOLA_HTTP_RETRIES", 4))
        self.backoff = backoff
        # The public API documents a sustained five-request-per-second limit.
        # A lock makes this a client-wide request-start limit even when callers
        # fan detail requests out through a thread pool.
        rate = min(5.0, max(0.0, float(requests_per_second)))
        self._request_interval = 1.0 / rate if rate else 0.0
        self._rate_lock = threading.Lock()
        self._last_request_start: Optional[float] = None
        self._key_lock = threading.Lock()
        self._cached_key: Optional[str] = None
        self.last_get_notes_truncated = False

    def _key(self) -> str:
        if self._cached_key is not None:
            return self._cached_key
        with self._key_lock:
            if self._cached_key is None:
                key = _normalize_api_key(self._key_provider())
                if not key:
                    raise GranolaPublicAPIError(
                        "No valid Granola API key found. Set GRANOLA_API_KEY to "
                        "a 'grn_' key or store one in the macOS keychain under "
                        f"service '{KEY_SERVICE}'. Create a key at "
                        "https://www.granola.ai (Business/Enterprise workspace "
                        "→ API keys)."
                    )
                self._cached_key = key
        return self._cached_key

    def _pace_request(self) -> None:
        """Keep request starts at or below the configured public API rate."""
        if not self._request_interval:
            return
        with self._rate_lock:
            now = time.monotonic()
            if self._last_request_start is not None:
                wait = self._last_request_start + self._request_interval - now
                if wait > 0:
                    time.sleep(wait)
            self._last_request_start = time.monotonic()

    @staticmethod
    def _retry_after_seconds(error: urllib.error.HTTPError) -> Optional[float]:
        """Parse a Retry-After header as seconds, when one is present."""
        value = error.headers.get("Retry-After") if error.headers else None
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            try:
                import datetime
                import email.utils

                retry_at = email.utils.parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=datetime.timezone.utc)
                return max(
                    0.0,
                    (retry_at - datetime.datetime.now(datetime.timezone.utc)).total_seconds(),
                )
            except (TypeError, ValueError, OverflowError):
                return None

    def _request(self, path: str) -> Any:
        url = PUBLIC_API_BASE + path
        last_err: Optional[GranolaPublicAPIError] = None
        for attempt in range(self.retries):
            retry_after: Optional[float] = None
            headers = {
                "Authorization": "Bearer " + self._key(),
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": "GranolaMCP",
            }
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                self._pace_request()
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
                        raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8")) if raw else None
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read()
                    if detail[:2] == b"\x1f\x8b":
                        detail = gzip.decompress(detail)
                    detail = detail.decode("utf-8", "replace")
                except Exception:
                    detail = str(e)
                if e.code in (401, 403):
                    raise GranolaPublicAPIError(
                        f"Granola public API auth failed (HTTP {e.code}). Check that "
                        f"GRANOLA_API_KEY is a valid 'grn_' key and the workspace has API "
                        f"access. Detail: {detail[:200]}"
                    ) from e
                if e.code == 404:
                    # Note doesn't exist, or has no generated summary/transcript yet.
                    raise GranolaPublicAPINotFoundError(
                        f"Granola public API {path}: not found (HTTP 404). The "
                        f"note may still be processing. Detail: {detail[:200]}"
                    ) from e
                last_err = GranolaPublicAPIError(f"Granola public API {path} failed (HTTP {e.code}): {detail[:300]}")
                if e.code not in self._RETRY_STATUS:
                    raise last_err from e
                if e.code == 429:
                    retry_after = self._retry_after_seconds(e)
            except (urllib.error.URLError, http.client.IncompleteRead,
                    ConnectionError, TimeoutError, socket.timeout) as e:
                reason = getattr(e, "reason", e)
                last_err = GranolaPublicAPIError(f"Granola public API {path} unreachable ({type(e).__name__}: {reason})")
            except (gzip.BadGzipFile, EOFError, UnicodeError,
                    json.JSONDecodeError, OSError) as e:
                last_err = GranolaPublicAPIError(
                    f"Granola public API {path} returned an invalid response "
                    f"({type(e).__name__}: {e})"
                )
            if attempt < self.retries - 1:
                delay = retry_after if retry_after is not None else self.backoff * (2 ** attempt)
                time.sleep(delay)
        raise GranolaPublicAPIError(
            f"{last_err} — gave up after {self.retries} attempts. Retry, or tune "
            f"GRANOLA_HTTP_TIMEOUT / GRANOLA_HTTP_RETRIES."
        )

    # ------------------------------------------------------------------ notes
    def get_notes(self, max_notes: int = 500, page_size: int = 30, *,
                  created_after: Optional[str] = None,
                  created_before: Optional[str] = None,
                  updated_after: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return bounded notes newest-first, following cursor pagination."""
        self.last_get_notes_truncated = False
        if max_notes <= 0:
            return []
        out: List[Dict[str, Any]] = []
        seen_ids = set()
        seen_cursors = set()
        cursor: Optional[str] = None
        while len(out) < max_notes:
            request_page_size = min(30, max(1, page_size), max_notes - len(out))
            params: Dict[str, Any] = {"page_size": request_page_size}
            if cursor:
                params["cursor"] = cursor
            if created_after:
                params["created_after"] = created_after
            if created_before:
                params["created_before"] = created_before
            if updated_after:
                params["updated_after"] = updated_after
            path = "/v1/notes?" + urllib.parse.urlencode(params)
            resp = self._request(path)
            if not isinstance(resp, dict):
                break
            notes = resp.get("notes")
            if notes is None:
                notes = resp.get("data")
            if not isinstance(notes, list) or not notes:
                break

            page_had_extra = False
            for note in notes:
                if not isinstance(note, dict):
                    continue
                note_id = note.get("id")
                if isinstance(note_id, (str, int)) and note_id in seen_ids:
                    continue
                if len(out) >= max_notes:
                    page_had_extra = True
                    break
                out.append(note)
                if isinstance(note_id, (str, int)):
                    seen_ids.add(note_id)

            has_more = bool(resp.get("has_more", resp.get("hasMore", False)))
            next_cursor = resp.get("next_cursor") or resp.get("cursor")
            if len(out) >= max_notes and (has_more or page_had_extra):
                self.last_get_notes_truncated = True
                logger.warning(
                    "Granola public API note listing reached the %d-note cap; "
                    "additional notes were not fetched.",
                    max_notes,
                )
                break
            if not has_more or not isinstance(next_cursor, str) or not next_cursor:
                break
            if next_cursor in seen_cursors:
                self.last_get_notes_truncated = True
                logger.warning(
                    "Granola public API returned a repeated pagination cursor; "
                    "stopping before duplicate pages are fetched."
                )
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return out

    def get_note(self, note_id: str, include_transcript: bool = True) -> Optional[Dict[str, Any]]:
        """Return a single note, optionally with its transcript attached."""
        path = f"/v1/notes/{urllib.parse.quote(note_id, safe='')}"
        if include_transcript:
            path += "?include=transcript"
        resp = self._request(path)
        if isinstance(resp, dict):
            return resp.get("note") or resp
        return None


# --------------------------------------------------------------------------- #
# Normalization: map public-API shapes onto the dict shape Meeting/Transcript  #
# already understand (see core/meeting.py, core/transcript.py).                #
# --------------------------------------------------------------------------- #
def _parse_iso(ts: Any):
    import datetime
    if not isinstance(ts, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(
            ts[:-1] + "+00:00" if ts.endswith("Z") else ts
        )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed
    except Exception:
        return None


def normalize_transcript(segments: Any) -> List[Dict[str, Any]]:
    """Convert public-API transcript segments to the internal segment shape.

    Public segment: ``{text, start_time(ISO), end_time(ISO), speaker:{name,source}}``.
    Internal shape adds ``start_timestamp``/``end_timestamp`` (absolute, used for
    duration) and ``start_time``/``end_time`` as float seconds-from-start (used by
    ``--timestamps``), with ``speaker`` flattened to a display name.
    """
    if not isinstance(segments, list) or not segments:
        return []
    t0 = None
    for s in segments:
        if not isinstance(s, dict):
            continue
        t0 = _parse_iso(s.get("start_time"))
        if t0 is not None:
            break
    out: List[Dict[str, Any]] = []
    for s in segments:
        if not isinstance(s, dict):
            continue
        spk = s.get("speaker")
        if isinstance(spk, dict):
            source = s.get("source") or spk.get("source")
            diarization_label = (
                s.get("diarization_label") or spk.get("diarization_label")
            )
            name = spk.get("name")
            name = name.strip() if isinstance(name, str) and name.strip() else None
        elif isinstance(spk, str) and spk.strip():
            source = s.get("source")
            diarization_label = s.get("diarization_label")
            name = spk.strip()
        else:
            source = s.get("source")
            diarization_label = s.get("diarization_label")
            name = None
        source = (
            source.strip()
            if isinstance(source, str) and source.strip()
            else None
        )
        diarization_label = (
            diarization_label.strip()
            if isinstance(diarization_label, str) and diarization_label.strip()
            else None
        )
        speaker = name or diarization_label or source or "Unknown"
        start_iso, end_iso = s.get("start_time"), s.get("end_time")
        seg: Dict[str, Any] = {
            "text": s.get("text") if isinstance(s.get("text"), str) else "",
            "speaker": speaker,
        }
        if source is not None:
            seg["source"] = source
        if diarization_label is not None:
            seg["diarization_label"] = diarization_label
        st, en = _parse_iso(start_iso), _parse_iso(end_iso)
        if st is not None:
            seg["start_timestamp"] = st.isoformat()
        if en is not None:
            seg["end_timestamp"] = en.isoformat()
        if t0 is not None and st is not None:
            seg["start_time"] = (st - t0).total_seconds()
        if t0 is not None and en is not None:
            seg["end_time"] = (en - t0).total_seconds()
        out.append(seg)
    return out


def normalize_attendees(note: Dict[str, Any]) -> List[str]:
    """Flatten a note's attendees/owner into display-name strings."""
    names: List[str] = []

    def add(entry: Any) -> None:
        if isinstance(entry, str):
            label = entry
        elif isinstance(entry, dict):
            label = entry.get("name") or entry.get("email")
        else:
            label = None
        if label and label not in names:
            names.append(label)

    att = note.get("attendees")
    if isinstance(att, list):
        for a in att:
            add(a)
    if not names:
        add(note.get("owner"))
    return names


def _folder_names(note: Dict[str, Any]) -> List[str]:
    """Extract display names from public API folder-membership metadata."""
    memberships = note.get("folder_membership")
    if memberships is None:
        memberships = note.get("folder_memberships")
    if isinstance(memberships, dict):
        nested = memberships.get("memberships")
        entries = nested if isinstance(nested, list) else [memberships]
    elif isinstance(memberships, list):
        entries = memberships
    else:
        entries = [memberships]

    names: List[str] = []
    for entry in entries:
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, dict):
            folder = entry.get("folder")
            name = entry.get("folder_name") or entry.get("name") or entry.get("title")
            if not name and isinstance(folder, dict):
                name = folder.get("name") or folder.get("title")
        else:
            name = None
        if isinstance(name, str) and name.strip() and name.strip() not in names:
            names.append(name.strip())
    return names


def note_to_meeting(note: Dict[str, Any]) -> Dict[str, Any]:
    """Map a public-API note dict onto the meeting dict shape used downstream."""
    meeting = dict(note)
    # Calendar timing (start/end) if present, so duration/date are accurate.
    cal = note.get("calendar_event") or note.get("calendar")
    if isinstance(cal, dict):
        meeting["google_calendar_event"] = cal
        if cal.get("scheduled_start_time") not in (None, ""):
            meeting["start_time"] = cal["scheduled_start_time"]
        if cal.get("scheduled_end_time") not in (None, ""):
            meeting["end_time"] = cal["scheduled_end_time"]
    title = note.get("title")
    if not isinstance(title, str) or not title.strip():
        event_title = cal.get("event_title") if isinstance(cal, dict) else None
        meeting["title"] = (
            event_title.strip()
            if isinstance(event_title, str) and event_title.strip()
            else "Untitled"
        )
    # Participants.
    participants = normalize_attendees(note)
    if participants:
        meeting["participants"] = participants
    folder_names = _folder_names(note)
    if folder_names:
        meeting["folder_names"] = folder_names
        meeting["folder_name"] = folder_names[0]
    # AI summary (markdown) -> a field Meeting.summary reads directly.
    summary_md = note.get("summary_markdown") or note.get("summary_text")
    if isinstance(summary_md, str) and summary_md.strip():
        meeting["ai_summary"] = summary_md
    # Transcript (when included).
    if "transcript" in note:
        meeting["transcript_data"] = normalize_transcript(note.get("transcript"))
    return meeting
