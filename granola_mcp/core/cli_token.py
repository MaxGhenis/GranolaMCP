"""
Granola internal-API token provider backed by the ``com.granola.cli`` keychain
bucket (a WorkOS session independent of the desktop app's).

Why this exists
---------------
Modern Granola (desktop v7.427.0, 2026-07-16) removed the on-disk ``storage.dek``
and encrypts ``granola.db`` (SQLCipher). That broke the old way of getting the
internal-API bearer token — it lived in ``supabase.json.enc``, which needs the
now-inaccessible DEK to decrypt. But the internal API (``api.granola.ai``) itself
still works, and there is a *second*, DEK-free copy of a usable session in the
macOS keychain under service ``com.granola.cli`` (account ``credentials``):

    {"refreshToken": "...", "accessToken": "<JWT>", "clientId": "client_..."}

This module reads that bundle, hands out the access token, and — only when the
access token is expired or the server rejects it (401) — refreshes it via WorkOS.

Refresh-token rotation safety
-----------------------------
WorkOS **rotates** the refresh token on every exchange: the old one becomes
invalid and reusing it can trip reuse-detection and revoke the whole session. So
a refresh MUST persist the new bundle exactly once and reliably. We:

1. POST the refresh exchange, get ``{access_token, refresh_token}``.
2. Immediately write the new bundle to a local recovery file (0600) so a later
   keychain-write failure can't strand the single-use token.
3. Write it back to the keychain with ``security add-generic-password -U`` and
   verify by reading it back **in a separate process** (a same-command read can
   race and return empty).

Everything is macOS-only (uses ``security``). Non-macOS raises ``GranolaTokenError``.
"""

import base64
import gzip
import json
import os
import stat
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

CLI_KEYCHAIN_SERVICE = os.environ.get("GRANOLA_CLI_KEYCHAIN_SERVICE", "com.granola.cli")
CLI_KEYCHAIN_ACCOUNT = os.environ.get("GRANOLA_CLI_KEYCHAIN_ACCOUNT", "credentials")
# WorkOS user-management authenticate endpoint. api.workos.com is the canonical
# host; auth.granola.ai (Granola's WorkOS custom domain) is an accepted alt.
WORKOS_HOST = os.environ.get("GRANOLA_WORKOS_HOST", "https://api.workos.com")
# Where to stash a just-rotated bundle before the keychain write, so a failed
# write is recoverable rather than a lockout.
RECOVERY_PATH = os.environ.get(
    "GRANOLA_CLI_TOKEN_RECOVERY",
    os.path.expanduser("~/Library/Application Support/Granola/.granolamcp-cli-token.json"),
)
_EXP_SKEW = 120  # treat a token expiring within 2 min as already expired


class GranolaTokenError(Exception):
    """Raised when a usable internal-API token cannot be obtained."""


# --------------------------------------------------------------------- keychain
def _security(*args: str, input_text: Optional[str] = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["security", *args], capture_output=True, text=True, timeout=30)
    except FileNotFoundError as e:  # pragma: no cover - non-macOS
        raise GranolaTokenError("`security` not found (Granola CLI token is macOS-only)") from e
    except subprocess.TimeoutExpired as e:
        raise GranolaTokenError("Timed out talking to the macOS keychain") from e


def read_cli_bundle() -> Optional[Dict[str, Any]]:
    """Return the parsed ``com.granola.cli`` bundle, or None if absent/invalid."""
    out = _security("find-generic-password", "-w", "-s", CLI_KEYCHAIN_SERVICE,
                    "-a", CLI_KEYCHAIN_ACCOUNT)
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        data = json.loads(out.stdout.strip())
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _write_recovery(bundle: Dict[str, Any]) -> None:
    """Best-effort 0600 stash of a bundle so a rotated token is never stranded."""
    try:
        os.makedirs(os.path.dirname(RECOVERY_PATH), exist_ok=True)
        with open(RECOVERY_PATH, "w", encoding="utf-8") as f:
            json.dump(bundle, f)
        os.chmod(RECOVERY_PATH, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass  # recovery is a safety net, never a hard dependency


def write_cli_bundle(bundle: Dict[str, Any]) -> None:
    """Persist ``bundle`` to the keychain (update-in-place) and verify read-back.

    The verifying read is a *separate* ``security`` invocation — a read issued in
    the same command as the write can race and come back empty.
    """
    data = json.dumps(bundle, separators=(",", ":"))
    out = _security("add-generic-password", "-U",
                    "-s", CLI_KEYCHAIN_SERVICE, "-a", CLI_KEYCHAIN_ACCOUNT,
                    "-w", data)
    if out.returncode != 0:
        raise GranolaTokenError(
            f"Failed to write rotated Granola token to keychain: {out.stderr.strip()}. "
            f"A recovery copy was saved to {RECOVERY_PATH}."
        )
    back = read_cli_bundle()  # separate process — avoids the same-command read race
    if not back or back.get("refreshToken") != bundle.get("refreshToken"):
        raise GranolaTokenError(
            f"Keychain write-back verification failed (recovery copy at {RECOVERY_PATH})."
        )


# -------------------------------------------------------------------------- jwt
def jwt_exp(token: str) -> Optional[int]:
    """Return the JWT ``exp`` (unix seconds), or None if undecodable."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except Exception:
        return None


def is_expired(token: Optional[str], skew: int = _EXP_SKEW) -> bool:
    if not token:
        return True
    exp = jwt_exp(token)
    return exp is None or exp <= time.time() + skew


# ---------------------------------------------------------------------- refresh
def _refresh(bundle: Dict[str, Any]) -> Dict[str, Any]:
    """Exchange the refresh token for a new bundle and persist it (rotation-safe)."""
    client_id = bundle.get("clientId")
    refresh_token = bundle.get("refreshToken")
    if not client_id or not refresh_token:
        raise GranolaTokenError("Granola CLI bundle missing clientId/refreshToken; cannot refresh.")
    body = json.dumps({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
    }).encode()
    req = urllib.request.Request(
        WORKOS_HOST + "/user_management/authenticate",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "Accept-Encoding": "gzip"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
        data = json.loads(raw)
    except urllib.error.HTTPError as e:
        detail = e.read()[:300]
        raise GranolaTokenError(
            f"WorkOS refresh failed (HTTP {e.code}): {detail!r}. The com.granola.cli "
            f"refresh token may be revoked/rotated; re-auth Granola's CLI to repopulate it."
        ) from e
    except Exception as e:
        raise GranolaTokenError(f"WorkOS refresh request failed: {e}") from e

    new_access = data.get("access_token")
    new_refresh = data.get("refresh_token")
    if not new_access or not new_refresh:
        raise GranolaTokenError(f"WorkOS refresh returned no tokens (keys: {sorted(data)}).")

    new_bundle = dict(bundle)
    new_bundle["accessToken"] = new_access
    new_bundle["refreshToken"] = new_refresh
    # Stash BEFORE the keychain write so a write failure can't strand the
    # single-use rotated refresh token.
    _write_recovery(new_bundle)
    write_cli_bundle(new_bundle)
    return new_bundle


# --------------------------------------------------------------------- provider
class CliTokenProvider:
    """Callable token provider with 401-driven refresh, for :class:`GranolaAPI`.

    ``provider()`` returns a valid access token (refreshing if the cached one is
    expired). ``provider.refresh()`` forces a rotation — call it once on a 401.
    """

    def __init__(self, bundle_reader=read_cli_bundle):
        self._read = bundle_reader
        self._token: Optional[str] = None

    def _load(self, force_refresh: bool = False) -> str:
        bundle = self._read()
        if not bundle:
            raise GranolaTokenError(
                f"No Granola CLI credentials in keychain (service '{CLI_KEYCHAIN_SERVICE}'). "
                f"This bucket is populated by Granola's CLI login."
            )
        token = bundle.get("accessToken")
        if force_refresh or is_expired(token):
            bundle = _refresh(bundle)
            token = bundle["accessToken"]
        if not token:
            raise GranolaTokenError("Granola CLI bundle has no access token.")
        return token

    def __call__(self) -> str:
        if self._token is None:
            self._token = self._load()
        return self._token

    def refresh(self) -> str:
        """Force-rotate and return the new access token (call on HTTP 401)."""
        self._token = self._load(force_refresh=True)
        return self._token


def cli_credentials_available() -> bool:
    """True if the com.granola.cli bucket exists (used for backend auto-select)."""
    try:
        b = read_cli_bundle()
        return bool(b and b.get("refreshToken") and b.get("clientId"))
    except Exception:
        return False
