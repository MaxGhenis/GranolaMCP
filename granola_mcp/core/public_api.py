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
import os
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

PUBLIC_API_BASE = os.environ.get("GRANOLA_PUBLIC_API_BASE", "https://public-api.granola.ai")
KEY_SERVICE = os.environ.get("GRANOLA_API_KEY_SERVICE", "GRANOLA_API_KEY")


class GranolaPublicAPIError(Exception):
    """Raised when a Granola public API request fails."""


def _keychain_api_key() -> Optional[str]:
    """Read the ``grn_`` API key from the macOS keychain, if present."""
    accounts: List[Optional[str]] = []
    env_acct = os.environ.get("GRANOLA_API_KEY_ACCOUNT")
    if env_acct:
        accounts.append(env_acct)
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
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    return None


def resolve_api_key() -> Optional[str]:
    """Return the Granola public API key from env or keychain (or None)."""
    return os.environ.get("GRANOLA_API_KEY") or _keychain_api_key()


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
                 backoff: float = 1.5):
        self._key_provider = key_provider or resolve_api_key
        self.timeout = timeout if timeout is not None else _env_int("GRANOLA_HTTP_TIMEOUT", 60)
        self.retries = max(1, retries if retries is not None else _env_int("GRANOLA_HTTP_RETRIES", 4))
        self.backoff = backoff

    def _key(self) -> str:
        key = self._key_provider()
        if not key:
            raise GranolaPublicAPIError(
                "No Granola API key found. Set the GRANOLA_API_KEY environment "
                "variable or store it in the macOS keychain under service "
                f"'{KEY_SERVICE}'. Create a key at https://www.granola.ai "
                "(Business/Enterprise workspace → API keys)."
            )
        return key

    def _request(self, path: str) -> Any:
        url = PUBLIC_API_BASE + path
        last_err: Optional[GranolaPublicAPIError] = None
        for attempt in range(self.retries):
            headers = {
                "Authorization": "Bearer " + self._key(),
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": "GranolaMCP",
            }
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
                        raw = gzip.decompress(raw)
                return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                detail = e.read()
                try:
                    if detail[:2] == b"\x1f\x8b":
                        detail = gzip.decompress(detail)
                    detail = detail.decode("utf-8", "replace")
                except Exception:
                    detail = str(detail)
                if e.code in (401, 403):
                    raise GranolaPublicAPIError(
                        f"Granola public API auth failed (HTTP {e.code}). Check that "
                        f"GRANOLA_API_KEY is a valid 'grn_' key and the workspace has API "
                        f"access. Detail: {detail[:200]}"
                    ) from e
                if e.code == 404:
                    # Note doesn't exist, or has no generated summary/transcript yet.
                    raise GranolaPublicAPIError(f"Granola public API {path}: not found (HTTP 404). "
                                                f"The note may still be processing. Detail: {detail[:200]}") from e
                last_err = GranolaPublicAPIError(f"Granola public API {path} failed (HTTP {e.code}): {detail[:300]}")
                if e.code not in self._RETRY_STATUS:
                    raise last_err from e
            except (urllib.error.URLError, http.client.IncompleteRead,
                    ConnectionError, TimeoutError, socket.timeout) as e:
                reason = getattr(e, "reason", e)
                last_err = GranolaPublicAPIError(f"Granola public API {path} unreachable ({type(e).__name__}: {reason})")
            if attempt < self.retries - 1:
                time.sleep(self.backoff * (2 ** attempt))
        raise GranolaPublicAPIError(
            f"{last_err} — gave up after {self.retries} attempts. Retry, or tune "
            f"GRANOLA_HTTP_TIMEOUT / GRANOLA_HTTP_RETRIES."
        )

    # ------------------------------------------------------------------ notes
    def get_notes(self, max_notes: int = 500, page_size: int = 100) -> List[Dict[str, Any]]:
        """Return notes newest-first, following ``cursor`` pagination."""
        out: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        while len(out) < max_notes:
            limit = min(page_size, max_notes - len(out))
            path = f"/v1/notes?limit={limit}"
            if cursor:
                path += f"&cursor={urllib.parse.quote(cursor)}"
            resp = self._request(path)
            if not isinstance(resp, dict):
                break
            notes = resp.get("notes") or resp.get("data") or []
            if not notes:
                break
            out.extend(notes)
            cursor = resp.get("next_cursor") or resp.get("cursor")
            if not cursor or len(notes) < limit:
                break
        return out[:max_notes]

    def get_note(self, note_id: str, include_transcript: bool = True) -> Optional[Dict[str, Any]]:
        """Return a single note, optionally with its transcript attached."""
        path = f"/v1/notes/{urllib.parse.quote(note_id)}"
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
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
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
        t0 = _parse_iso(s.get("start_time"))
        if t0 is not None:
            break
    out: List[Dict[str, Any]] = []
    for s in segments:
        if not isinstance(s, dict):
            continue
        spk = s.get("speaker")
        if isinstance(spk, dict):
            speaker = spk.get("name") or spk.get("source") or "Unknown"
        else:
            speaker = spk or "Unknown"
        start_iso, end_iso = s.get("start_time"), s.get("end_time")
        seg: Dict[str, Any] = {
            "text": s.get("text", ""),
            "speaker": speaker,
            "start_timestamp": start_iso,
            "end_timestamp": end_iso,
        }
        st, en = _parse_iso(start_iso), _parse_iso(end_iso)
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


def note_to_meeting(note: Dict[str, Any]) -> Dict[str, Any]:
    """Map a public-API note dict onto the meeting dict shape used downstream."""
    meeting = dict(note)
    # Calendar timing (start/end) if present, so duration/date are accurate.
    cal = note.get("calendar_event") or note.get("calendar")
    if isinstance(cal, dict):
        meeting["google_calendar_event"] = cal
    # Participants.
    participants = normalize_attendees(note)
    if participants:
        meeting["participants"] = participants
    # AI summary (markdown) -> a field Meeting.summary reads directly.
    summary_md = note.get("summary_markdown") or note.get("summary_text")
    if isinstance(summary_md, str) and summary_md.strip():
        meeting["ai_summary"] = summary_md
    # Transcript (when included).
    if "transcript" in note:
        meeting["transcript_data"] = normalize_transcript(note.get("transcript"))
    return meeting
