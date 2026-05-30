"""
Granola local-storage decryption (macOS).

Modern Granola (cache-v6 era) encrypts its on-disk state. The chain is:

  macOS keychain item ("Granola Safe Storage" / "Granola Key")
      -> Electron safeStorage / Chromium OSCrypt v10 key (PBKDF2-SHA1, "saltysalt")
      -> decrypts ``storage.dek`` (a base64 AES-256 key, the app's DEK)
      -> the DEK decrypts the ``*.enc`` files (AES-256-GCM) and tokens.

This module reads the keychain item exactly ONCE per process (targeted
``security find-generic-password`` — not a keychain dump), derives the DEK,
and decrypts the requested files. After the user clicks "Always Allow" once,
subsequent reads are silent, so there are no keychain prompt storms (the reason
the old ``granola`` npm CLI was blocked).

Everything here is read-only. Nothing is ever written back to Granola's files.
"""

import base64
import gzip
import json
import os
import subprocess
from hashlib import pbkdf2_hmac
from typing import Any, Dict, List, Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# Default macOS keychain coordinates for Granola's Electron safeStorage key.
KEYCHAIN_SERVICE = os.environ.get("GRANOLA_KEYCHAIN_SERVICE", "Granola Safe Storage")
KEYCHAIN_ACCOUNT = os.environ.get("GRANOLA_KEYCHAIN_ACCOUNT", "Granola Key")

_DEK_CACHE: Optional[bytes] = None


class GranolaCryptoError(Exception):
    """Raised when Granola local data cannot be decrypted."""


def granola_dir() -> str:
    """Return the Granola Application Support directory."""
    override = os.environ.get("GRANOLA_DIR")
    if override:
        return os.path.expanduser(override)
    return os.path.expanduser("~/Library/Application Support/Granola")


def _keychain_secret() -> str:
    """Read the Electron safeStorage secret from the macOS login keychain.

    Targeted single-item read; prompts at most once ("Always Allow" makes it
    silent thereafter). Never dumps the keychain.
    """
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-w",
             "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError as e:  # pragma: no cover - non-macOS
        raise GranolaCryptoError("`security` not found (Granola decryption is macOS-only)") from e
    except subprocess.TimeoutExpired as e:
        raise GranolaCryptoError(
            "Timed out reading the Granola keychain item. If a macOS dialog "
            "appeared, approve it (ideally 'Always Allow')."
        ) from e
    if out.returncode != 0:
        raise GranolaCryptoError(
            f"Could not read keychain item '{KEYCHAIN_SERVICE}'/'{KEYCHAIN_ACCOUNT}': "
            f"{out.stderr.strip() or 'not found'}"
        )
    return out.stdout.strip()


def _oscrypt_v10_decrypt(blob: bytes, secret: str) -> bytes:
    """Decrypt a Chromium OSCrypt / Electron safeStorage 'v10' blob (AES-128-CBC)."""
    if blob[:3] != b"v10":
        raise GranolaCryptoError(f"Unexpected safeStorage prefix: {blob[:4]!r}")
    key = pbkdf2_hmac("sha1", secret.encode(), b"saltysalt", 1003, 16)
    decryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    padded = decryptor.update(blob[3:]) + decryptor.finalize()
    pad = padded[-1]
    if 1 <= pad <= 16:
        padded = padded[:-pad]
    return padded


def get_dek(refresh: bool = False) -> bytes:
    """Return the 32-byte Granola Data Encryption Key (cached per process)."""
    global _DEK_CACHE
    if _DEK_CACHE is not None and not refresh:
        return _DEK_CACHE
    dek_path = os.path.join(granola_dir(), "storage.dek")
    if not os.path.exists(dek_path):
        raise GranolaCryptoError(f"storage.dek not found at {dek_path}")
    with open(dek_path, "rb") as f:
        blob = f.read()
    secret = _keychain_secret()
    dek_material = _oscrypt_v10_decrypt(blob, secret)
    # The DEK is stored as a base64 string of the raw 32-byte AES-256 key.
    try:
        dek = base64.b64decode(dek_material)
    except Exception:
        dek = dek_material
    if len(dek) not in (16, 24, 32):
        raise GranolaCryptoError(f"Unexpected DEK length: {len(dek)} bytes")
    _DEK_CACHE = dek
    return dek


def decrypt_blob(blob: bytes, dek: Optional[bytes] = None) -> bytes:
    """Decrypt a Granola ``*.enc`` blob: AES-256-GCM, nonce(12) || ct || tag(16)."""
    if dek is None:
        dek = get_dek()
    nonce, body = blob[:12], blob[12:]
    ciphertext, tag = body[:-16], body[-16:]
    decryptor = Cipher(algorithms.AES(dek), modes.GCM(nonce, tag)).decryptor()
    plaintext = decryptor.update(ciphertext) + decryptor.finalize()
    if plaintext[:2] == b"\x1f\x8b":
        plaintext = gzip.decompress(plaintext)
    return plaintext


def decrypt_file(path: str, dek: Optional[bytes] = None) -> bytes:
    """Decrypt an on-disk ``*.enc`` file and return plaintext bytes."""
    if not os.path.exists(path):
        raise GranolaCryptoError(f"Encrypted file not found: {path}")
    with open(path, "rb") as f:
        return decrypt_blob(f.read(), dek)


def load_json_enc(name: str, dek: Optional[bytes] = None) -> Dict[str, Any]:
    """Decrypt ``<granola_dir>/<name>`` and parse it as JSON."""
    return json.loads(decrypt_file(os.path.join(granola_dir(), name), dek))


def get_workos_tokens() -> Dict[str, Any]:
    """Return the current WorkOS token bundle from encrypted (or plaintext) storage.

    Prefers the live, app-maintained ``supabase.json.enc``; falls back to the
    legacy plaintext ``supabase.json`` if decryption is unavailable.
    """
    gdir = granola_dir()
    data: Optional[Dict[str, Any]] = None
    try:
        data = load_json_enc("supabase.json.enc")
    except Exception:
        plain = os.path.join(gdir, "supabase.json")
        if os.path.exists(plain):
            with open(plain) as f:
                data = json.load(f)
    if not data:
        raise GranolaCryptoError("No Granola auth data found (supabase.json[.enc]).")
    tokens = data.get("workos_tokens", data)
    if isinstance(tokens, str):
        tokens = json.loads(tokens)
    return tokens


def get_access_token() -> str:
    """Return the current Granola API bearer token (read-only, never refreshed).

    Granola's desktop app keeps this token fresh on disk; we only read it, so
    the app's session is never disturbed.
    """
    tokens = get_workos_tokens()
    token = tokens.get("access_token")
    if not token:
        raise GranolaCryptoError("No access_token in Granola auth data.")
    return token


def access_token_expiry(token: Optional[str] = None) -> Optional[int]:
    """Return the JWT ``exp`` (unix seconds) for the access token, if decodable."""
    if token is None:
        token = get_access_token()
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except Exception:
        return None


def load_cache_state(dek: Optional[bytes] = None) -> Dict[str, Any]:
    """Decrypt ``cache-v6.json.enc`` and return ``cache.state`` (transcripts, etc.)."""
    raw = json.loads(decrypt_file(os.path.join(granola_dir(), "cache-v6.json.enc"), dek))
    cache = raw.get("cache", raw)
    return cache.get("state", {})


def local_transcripts(dek: Optional[bytes] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Return the locally-cached transcripts: ``{document_id: [segment, ...]}``."""
    try:
        return load_cache_state(dek).get("transcripts", {}) or {}
    except Exception:
        return {}
