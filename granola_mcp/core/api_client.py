"""
Granola HTTP API client.

Reads the app-maintained bearer token from local encrypted storage (see
:mod:`granola_mcp.utils.granola_crypto`) and talks to ``api.granola.ai``.
This is strictly read-only: it never calls token-refresh/authenticate
endpoints, so Granola's desktop session (and its rotating refresh token) is
left untouched.

Stdlib-only HTTP (urllib) to keep the project's light dependency footprint.
"""

import gzip
import http.client
import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from ..utils.granola_crypto import get_access_token

API_BASE = "https://api.granola.ai"


def default_token_provider():
    """Pick a token source for the internal API.

    Prefers the DEK-free ``com.granola.cli`` keychain bucket (refreshable via
    WorkOS); falls back to the legacy ``supabase.json.enc`` reader (which needs
    the — now usually unavailable — local DEK). Returns a provider object:
    callable ``provider() -> token`` and, for the CLI bucket, ``provider.refresh()``.
    """
    try:
        from .cli_token import CliTokenProvider, cli_credentials_available
        if cli_credentials_available():
            return CliTokenProvider()
    except Exception:
        pass
    return get_access_token
# Mirrors the desktop client so the API treats us as a normal Granola client.
DEFAULT_CLIENT_HEADERS = {
    "User-Agent": "Granola/6 (GranolaMCP)",
    "X-Client-Version": "6.0.0",
    "X-Client-Type": "granolamcp",
}


def _env_int(name: str, default: int) -> int:
    """Read a positive int from the environment, falling back on bad/missing values."""
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


class GranolaAPIError(Exception):
    """Raised when a Granola API request fails."""


class GranolaAPI:
    """Minimal read-only client for the Granola document API."""

    # HTTP statuses worth retrying (transient server/rate-limit conditions).
    _RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

    def __init__(self, token_provider: Optional[Callable[[], str]] = None,
                 timeout: Optional[int] = None, retries: Optional[int] = None,
                 backoff: float = 1.5):
        self._token_provider = token_provider or default_token_provider()
        # Defaults are generous and env-overridable: transcript/panel payloads are
        # ~1 MB and slow networks (travel wifi, mobile, trains) need headroom.
        self.timeout = timeout if timeout is not None else _env_int("GRANOLA_HTTP_TIMEOUT", 60)
        self.retries = max(1, retries if retries is not None else _env_int("GRANOLA_HTTP_RETRIES", 4))
        self.backoff = backoff

    def _request(self, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        url = API_BASE + path
        data = json.dumps(body or {}).encode()
        last_err: Optional[GranolaAPIError] = None
        refreshed = False
        for attempt in range(self.retries):
            # Re-read the token each attempt so a mid-retry app refresh is picked up.
            headers = {
                "Authorization": "Bearer " + self._token_provider(),
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                **DEFAULT_CLIENT_HEADERS,
            }
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
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
                    # Token rejected. If the provider can refresh (CLI bucket via
                    # WorkOS), rotate once and retry; otherwise fail with guidance.
                    refresh = getattr(self._token_provider, "refresh", None)
                    if callable(refresh) and not refreshed:
                        refreshed = True
                        try:
                            refresh()
                            continue
                        except Exception as re:
                            raise GranolaAPIError(
                                f"Granola API auth failed (HTTP {e.code}) and token refresh "
                                f"failed: {re}. Detail: {detail[:200]}"
                            ) from re
                    raise GranolaAPIError(
                        f"Granola API auth failed (HTTP {e.code}). The token may be stale — "
                        f"open the Granola app (or re-auth its CLI) to refresh it. "
                        f"Detail: {detail[:200]}"
                    ) from e
                last_err = GranolaAPIError(
                    f"Granola API {path} failed (HTTP {e.code}): {detail[:300]}"
                )
                if e.code not in self._RETRY_STATUS:
                    raise last_err from e
            except (urllib.error.URLError, http.client.IncompleteRead,
                    ConnectionError, TimeoutError, socket.timeout) as e:
                # Slow/flaky network: read timeouts, dropped connections, partial
                # bodies (IncompleteRead). Common on travel wifi for ~1 MB payloads.
                reason = getattr(e, "reason", e)
                last_err = GranolaAPIError(
                    f"Granola API {path} unreachable ({type(e).__name__}: {reason})"
                )
            if attempt < self.retries - 1:
                time.sleep(self.backoff * (2 ** attempt))  # exponential backoff
        raise GranolaAPIError(
            f"{last_err} — gave up after {self.retries} attempts. On a slow connection, "
            f"retry (transcripts/summaries are ~1 MB) and keep the Granola app open so the "
            f"token stays fresh; tune GRANOLA_HTTP_TIMEOUT / GRANOLA_HTTP_RETRIES if needed."
        ) from last_err

    def get_documents(self, limit: Optional[int] = None, max_documents: int = 500,
                      include_last_viewed: bool = True) -> List[Dict[str, Any]]:
        """Fetch documents (meetings), newest first, following pagination.

        Args:
            limit: page size per request (default 100).
            max_documents: hard cap on total documents fetched.
            include_last_viewed: ask the API to include last-viewed metadata.
        """
        page = limit or 100
        out: List[Dict[str, Any]] = []
        offset = 0
        cursor: Optional[str] = None
        while len(out) < max_documents:
            body: Dict[str, Any] = {"limit": min(page, max_documents - len(out))}
            if include_last_viewed:
                body["include_last_viewed_panel"] = True
            if cursor:
                body["cursor"] = cursor
            else:
                body["offset"] = offset
            resp = self._request("/v2/get-documents", body)
            if not isinstance(resp, dict):
                break
            docs = resp.get("docs") or resp.get("documents") or []
            if not docs:
                break
            out.extend(docs)
            cursor = resp.get("next_cursor") or resp.get("cursor")
            offset += len(docs)
            if len(docs) < body["limit"] and not cursor:
                break
        return out[:max_documents]

    def get_document_transcript(self, document_id: str) -> List[Dict[str, Any]]:
        """Fetch the full transcript (list of segments) for a document."""
        resp = self._request("/v1/get-document-transcript", {"document_id": document_id})
        if isinstance(resp, list):
            return resp
        if isinstance(resp, dict):
            for key in ("transcript", "segments", "chunks", "data"):
                if isinstance(resp.get(key), list):
                    return resp[key]
        return []

    def get_document_panels(self, document_id: str) -> List[Dict[str, Any]]:
        """Fetch AI panels (summary/notes) for a document."""
        resp = self._request("/v1/get-document-panels", {"document_id": document_id})
        if isinstance(resp, list):
            return resp
        if isinstance(resp, dict):
            for key in ("panels", "data"):
                if isinstance(resp.get(key), list):
                    return resp[key]
        return []

    def get_summary_html(self, document_id: str) -> Optional[str]:
        """Return the AI summary as combined HTML from the document's panels.

        Mirrors the legacy cache parser: concatenate panel ``original_content``,
        skipping link-only panels (those starting with ``<hr>``).
        """
        parts: List[str] = []
        for panel in self.get_document_panels(document_id):
            if not isinstance(panel, dict):
                continue
            content = panel.get("original_content")
            if isinstance(content, str) and content.strip() and not content.strip().startswith("<hr>"):
                parts.append(content)
        return "\n\n".join(parts) if parts else None
