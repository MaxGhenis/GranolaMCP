"""
Data source for GranolaMCP.

``GranolaParser`` presents a stable interface (``get_meetings``,
``get_meeting_by_id``, ``validate_cache_structure``, ``reload`` ...) over two
backends, chosen automatically:

* **api** (default, current Granola): documents come from the Granola HTTP API
  using the locally-stored bearer token; transcripts come from the local
  decrypted cache (instant) with a lazy API fallback for older meetings. This
  is required because modern Granola no longer stores documents in the local
  cache — only recent transcripts — and encrypts everything on disk.
* **plaintext** (legacy): the original double-JSON ``cache-v3.json`` parsing,
  used when ``cache_path`` points at a readable plaintext cache file.

Downstream code (``Meeting``, the CLI formatters, and all MCP tools) is
unchanged: ``get_meetings`` returns dicts in the same shape they always did.
"""

import json
import os
from typing import Any, Dict, List, Optional

from ..utils.config import get_cache_path, validate_cache_path
from .meeting import set_transcript_loader, set_summary_loader


class GranolaParseError(Exception):
    """Custom exception for Granola parsing errors."""
    pass


def _resolve_mode(cache_path: Optional[str]) -> str:
    """Pick a backend. Honors ``GRANOLA_MODE``; otherwise auto-detects.

    Preference order (auto): plaintext cache file → official **public** API (if
    a ``grn_`` key is available) → legacy internal **api**. Public is favored
    because modern Granola encrypts local storage and moved the cache key out of
    ``storage.dek``, which breaks the internal ``api`` backend; the public API
    needs no local decryption and is the supported, stable path.
    """
    env = os.environ.get("GRANOLA_MODE")
    if env:
        return env.strip().lower()
    # Explicit plaintext cache file (legacy) -> plaintext backend.
    if cache_path and cache_path.endswith(".json") and ".enc" not in os.path.basename(cache_path) \
            and validate_cache_path(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                head = f.read(4096)
            if '"documents"' in head or '"cache"' in head:
                return "plaintext"
        except Exception:
            pass
    # Prefer the internal `api` backend when the DEK-free CLI token bucket is
    # available — it's the freshest source (surfaces just-ended / in-progress
    # meetings the official public API hasn't published yet). Fall back to the
    # supported public API (grn_ key) otherwise; `get_meetings` degrades from
    # `api` to `public` at runtime if the internal token can't be obtained.
    try:
        from .cli_token import cli_credentials_available
        if cli_credentials_available():
            return "api"
    except Exception:
        pass
    try:
        from .public_api import has_api_key
        if has_api_key():
            return "public"
    except Exception:
        pass
    return "api"


def _participants_from_people(people: Any) -> List[str]:
    """Flatten Granola's ``people`` object into a list of display names/emails."""
    names: List[str] = []

    def add(entry: Any) -> None:
        if isinstance(entry, str):
            label = entry
        elif isinstance(entry, dict):
            label = entry.get("name") or entry.get("email")
            if not label and isinstance(entry.get("details"), dict):
                person = entry["details"].get("person") or {}
                nm = person.get("name") or {}
                label = nm.get("fullName") if isinstance(nm, dict) else None
        else:
            label = None
        if label and label not in names:
            names.append(label)

    if isinstance(people, dict):
        add(people.get("creator"))
        for key in ("attendees", "others", "people", "guests"):
            val = people.get(key)
            if isinstance(val, list):
                for item in val:
                    add(item)
    elif isinstance(people, list):
        for item in people:
            add(item)
    return names


class GranolaParser:
    """Backend-aware accessor for Granola meeting data."""

    def __init__(self, cache_path: Optional[str] = None, mode: Optional[str] = None):
        self.cache_path = cache_path or get_cache_path()
        self.mode = (mode or _resolve_mode(self.cache_path)).lower()

        # api-backend state
        self._api = None
        self._local_tx: Optional[Dict[str, Any]] = None
        self._tx_cache: Dict[str, Optional[List[Dict[str, Any]]]] = {}
        self._panel_cache: Dict[str, Optional[str]] = {}

        # public-backend state
        self._public = None
        self._note_cache: Dict[str, Optional[Dict[str, Any]]] = {}

        # plaintext-backend state
        self._cache_data: Optional[Dict[str, Any]] = None
        self._raw_data: Optional[str] = None

        if self.mode == "api":
            # Let Meeting lazily pull transcripts / AI summaries on demand.
            set_transcript_loader(self.fetch_transcript)
            set_summary_loader(self.fetch_summary_html)
        elif self.mode == "public":
            set_transcript_loader(self.fetch_transcript_public)
            set_summary_loader(self.fetch_summary_public)

    # ------------------------------------------------------------------ api
    def _ensure_api(self):
        if self._api is None:
            from .api_client import GranolaAPI
            self._api = GranolaAPI()
        return self._api

    def _local_transcripts(self, force: bool = False) -> Dict[str, Any]:
        if self._local_tx is None or force:
            from ..utils.granola_crypto import local_transcripts
            self._local_tx = local_transcripts()
        return self._local_tx

    def fetch_transcript(self, meeting_id: Optional[str]) -> Optional[List[Dict[str, Any]]]:
        """Return transcript segments for a meeting (local cache, then API).

        Memoized per id (including negative results) so repeated access and
        bulk operations don't refetch.
        """
        if not meeting_id:
            return None
        if meeting_id in self._tx_cache:
            return self._tx_cache[meeting_id]
        segments = self._local_transcripts().get(meeting_id)
        if not segments:
            try:
                segments = self._ensure_api().get_document_transcript(meeting_id) or None
            except Exception:
                segments = None
        self._tx_cache[meeting_id] = segments
        return segments

    def fetch_summary_html(self, meeting_id: Optional[str]) -> Optional[str]:
        """Return the AI summary HTML for a meeting (via panels API), memoized."""
        if not meeting_id:
            return None
        if meeting_id in self._panel_cache:
            return self._panel_cache[meeting_id]
        html: Optional[str] = None
        try:
            html = self._ensure_api().get_summary_html(meeting_id)
        except Exception:
            html = None
        self._panel_cache[meeting_id] = html
        return html

    def _get_meetings_api(self, debug: bool = False) -> List[Dict[str, Any]]:
        api = self._ensure_api()
        max_docs = int(os.environ.get("GRANOLA_MAX_DOCUMENTS", "500"))
        docs = api.get_documents(max_documents=max_docs)
        local_tx = self._local_transcripts(force=True)
        if debug:
            print(f"DEBUG: API returned {len(docs)} documents; {len(local_tx)} local transcripts")

        meetings: List[Dict[str, Any]] = []
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            meeting = dict(doc)
            # Map Granola's `people` object to the participant list Meeting expects.
            if "people" in meeting and "participants" not in meeting:
                participants = _participants_from_people(meeting.get("people"))
                if participants:
                    meeting["participants"] = participants
            # Attach a locally-cached transcript when we have one (instant).
            doc_id = meeting.get("id")
            if doc_id and doc_id in local_tx:
                meeting["transcript_data"] = local_tx[doc_id]
            meetings.append(meeting)
        return meetings

    # ---------------------------------------------------------------- public
    def _ensure_public(self):
        if self._public is None:
            from .public_api import GranolaPublicAPI
            self._public = GranolaPublicAPI()
        return self._public

    def _get_note(self, note_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """Fetch and memoize a full note (with transcript) from the public API."""
        if not note_id:
            return None
        if note_id in self._note_cache:
            return self._note_cache[note_id]
        try:
            note = self._ensure_public().get_note(note_id, include_transcript=True)
        except Exception:
            note = None
        self._note_cache[note_id] = note
        return note

    def fetch_transcript_public(self, meeting_id: Optional[str]) -> Optional[List[Dict[str, Any]]]:
        """Lazy transcript loader for the public backend (normalized segments)."""
        note = self._get_note(meeting_id)
        if not note:
            return None
        from .public_api import normalize_transcript
        segments = normalize_transcript(note.get("transcript"))
        return segments or None

    def fetch_summary_public(self, meeting_id: Optional[str]) -> Optional[str]:
        """Lazy AI-summary loader for the public backend (markdown as-is)."""
        note = self._get_note(meeting_id)
        if not note:
            return None
        md = note.get("summary_markdown") or note.get("summary_text")
        return md if isinstance(md, str) and md.strip() else None

    def _get_meetings_public(self, debug: bool = False) -> List[Dict[str, Any]]:
        api = self._ensure_public()
        max_docs = int(os.environ.get("GRANOLA_MAX_DOCUMENTS", "500"))
        notes = api.get_notes(max_notes=max_docs)
        if debug:
            print(f"DEBUG: public API returned {len(notes)} notes")
        from .public_api import note_to_meeting
        return [note_to_meeting(n) for n in notes if isinstance(n, dict)]

    # ------------------------------------------------------------- plaintext
    def load_cache(self, force_reload: bool = False) -> Dict[str, Any]:
        """Load parsed cache data.

        For the api backend this returns the decrypted local cache state
        (transcripts + UI state) wrapped as ``{"state": ...}`` for
        compatibility with tools that introspect the cache directly.
        """
        if self.mode == "public":
            # The public API has no local cache; expose an empty state shape.
            return {"state": {}}
        if self.mode == "api":
            # Transcripts come over HTTP; the local encrypted cache is a bonus,
            # not a requirement. If it can't be decrypted (modern Granola removed
            # the DEK), degrade to an empty state instead of failing the CLI.
            from ..utils.granola_crypto import load_cache_state
            try:
                return {"state": load_cache_state()}
            except Exception:
                return {"state": {}}

        if self._cache_data is not None and not force_reload:
            return self._cache_data
        if not validate_cache_path(self.cache_path):
            raise GranolaParseError(f"Cache file not found or not readable: {self.cache_path}")
        try:
            with open(self.cache_path, "r", encoding="utf-8") as f:
                self._raw_data = f.read()
            outer = json.loads(self._raw_data)
            if not isinstance(outer, dict) or "cache" not in outer:
                raise GranolaParseError("Cache file missing required 'cache' field")
            content = outer["cache"]
            self._cache_data = json.loads(content) if isinstance(content, str) else content
            if not isinstance(self._cache_data, dict):
                raise GranolaParseError("Cache content must be a JSON object")
            return self._cache_data
        except GranolaParseError:
            raise
        except Exception as e:
            raise GranolaParseError(f"Error loading cache file: {e}") from e

    def _get_meetings_plaintext(self, debug: bool = False) -> List[Dict[str, Any]]:
        cache_data = self.load_cache()
        if "state" not in cache_data:
            raise GranolaParseError("Cache data missing required 'state' key")
        state = cache_data["state"]
        documents = state.get("documents", {})
        meetings_metadata = state.get("meetingsMetadata", {})
        transcripts = state.get("transcripts", {})
        document_panels = state.get("documentPanels", {})
        document_lists = state.get("documentLists", {})
        document_lists_metadata = state.get("documentListsMetadata", {})

        meeting_to_folder: Dict[str, Dict[str, Any]] = {}
        for list_id, meeting_ids in document_lists.items():
            folder_info = document_lists_metadata.get(list_id, {})
            folder_name = folder_info.get("title", "Unknown")
            if isinstance(meeting_ids, list):
                for meeting_id in meeting_ids:
                    meeting_to_folder[meeting_id] = {"folder_id": list_id, "folder_name": folder_name}

        meetings: List[Dict[str, Any]] = []
        for doc_id, doc_data in documents.items():
            meeting = doc_data.copy()
            if doc_id in meetings_metadata:
                for key, value in meetings_metadata[doc_id].items():
                    if key not in meeting or not meeting[key]:
                        meeting[key] = value
            if doc_id in transcripts:
                meeting["transcript_data"] = transcripts[doc_id]
            if doc_id in document_panels:
                panels = document_panels[doc_id]
                ai_summaries = []
                for _pid, panel in panels.items():
                    original = panel.get("original_content", "")
                    if original and isinstance(original, str) and not original.strip().startswith("<hr>"):
                        ai_summaries.append(original)
                if ai_summaries:
                    meeting["ai_summary_html"] = "\n\n".join(ai_summaries)
                for _pid, panel in panels.items():
                    content = panel.get("content")
                    if content and isinstance(content, dict):
                        meeting["panel_content"] = content
                        break
            folder = meeting_to_folder.get(doc_id)
            meeting["folder_name"] = folder["folder_name"] if folder else None
            meeting["folder_id"] = folder["folder_id"] if folder else None
            meetings.append(meeting)
        return meetings

    # --------------------------------------------------------------- public
    def _switch_to_public(self, reason: str = "") -> bool:
        """Degrade from the internal `api` backend to the public API, if possible.

        Returns True if the switch happened. Re-wires the lazy loaders so any
        Meeting created afterward pulls transcripts/summaries from the public API.
        """
        if self.mode == "public":
            return True
        try:
            from .public_api import has_api_key
            if not has_api_key():
                return False
        except Exception:
            return False
        self.mode = "public"
        self._note_cache.clear()
        set_transcript_loader(self.fetch_transcript_public)
        set_summary_loader(self.fetch_summary_public)
        return True

    def get_meetings(self, debug: bool = False) -> List[Dict[str, Any]]:
        """Return all meetings as dicts (combined document + transcript data)."""
        if self.mode == "api":
            try:
                return self._get_meetings_api(debug=debug)
            except Exception as e:
                # Internal API/token unavailable — degrade to the public API.
                if self._switch_to_public():
                    if debug:
                        print(f"DEBUG: internal api failed ({e}); using public API")
                    return self._get_meetings_public(debug=debug)
                raise
        if self.mode == "public":
            return self._get_meetings_public(debug=debug)
        return self._get_meetings_plaintext(debug=debug)

    def _get_meeting_by_id_public(self, meeting_id: str) -> Optional[Dict[str, Any]]:
        """Public backend: direct note fetch (one request), else short-id scan."""
        from .public_api import note_to_meeting
        note = self._get_note(meeting_id)
        if note:
            return note_to_meeting(note)
        for meeting in self._get_meetings_public():
            mid = meeting.get("id") if isinstance(meeting, dict) else None
            if mid and str(mid).startswith(meeting_id):
                full = self._get_note(mid)
                return note_to_meeting(full) if full else meeting
        return None

    def get_meeting_by_id(self, meeting_id: str) -> Optional[Dict[str, Any]]:
        if self.mode == "public":
            return self._get_meeting_by_id_public(meeting_id)

        if self.mode == "api":
            try:
                meetings = self._get_meetings_api()
            except Exception:
                if self._switch_to_public():
                    return self._get_meeting_by_id_public(meeting_id)
                raise
        else:
            meetings = self.get_meetings()

        for meeting in meetings:
            if isinstance(meeting, dict):
                for id_field in ("id", "meeting_id", "session_id", "uuid"):
                    if meeting.get(id_field) == meeting_id:
                        # Ensure the transcript is attached for single-meeting access.
                        if "transcript_data" not in meeting:
                            seg = self.fetch_transcript(meeting_id)
                            if seg:
                                meeting["transcript_data"] = seg
                        return meeting
        return None

    def validate_cache_structure(self) -> bool:
        """Cheap readiness check (no network for the api backend)."""
        try:
            if self.mode == "public":
                from .public_api import has_api_key
                return has_api_key()
            if self.mode == "api":
                # Readiness = an internal-API token is obtainable. Do NOT require
                # the local DEK (modern Granola no longer exposes it); transcripts
                # come over HTTP, not the local cache.
                try:
                    from .cli_token import cli_credentials_available
                    if cli_credentials_available():
                        return True
                except Exception:
                    pass
                from ..utils.granola_crypto import get_access_token
                return bool(get_access_token())
            cache_data = self.load_cache()
            return isinstance(cache_data, dict) and isinstance(self.get_meetings(), list)
        except Exception:
            return False

    def get_cache_info(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {
            "mode": self.mode,
            "cache_path": self.cache_path,
            "meeting_count": 0,
            "valid_structure": False,
        }
        if self.mode == "public":
            from .public_api import PUBLIC_API_BASE
            info["api_base"] = PUBLIC_API_BASE
            info["readable"] = info["valid_structure"] = self.validate_cache_structure()
            try:
                info["meeting_count"] = len(self.get_meetings())
                info["valid_structure"] = True
            except Exception:
                pass
            return info
        if self.mode == "api":
            from ..utils.granola_crypto import granola_dir
            info["granola_dir"] = granola_dir()
            info["readable"] = info["valid_structure"] = self.validate_cache_structure()
            try:
                info["meeting_count"] = len(self.get_meetings())
                info["valid_structure"] = True
            except Exception:
                pass
            return info
        info["exists"] = os.path.exists(self.cache_path)
        info["readable"] = validate_cache_path(self.cache_path)
        if info["exists"]:
            try:
                info["size_bytes"] = os.path.getsize(self.cache_path)
            except Exception:
                pass
        if info["readable"]:
            try:
                info["meeting_count"] = len(self.get_meetings())
                info["valid_structure"] = True
            except Exception:
                pass
        return info

    def reload(self) -> Dict[str, Any]:
        """Force a refresh of cached state."""
        self._tx_cache.clear()
        self._panel_cache.clear()
        self._note_cache.clear()
        self._local_tx = None
        if self.mode == "public":
            return {"state": {}}
        if self.mode == "api":
            from ..utils import granola_crypto
            granola_crypto._DEK_CACHE = None  # re-derive in case the app re-keyed
            return self.load_cache()
        return self.load_cache(force_reload=True)
