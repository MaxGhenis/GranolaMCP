"""Offline tests for rotation-safe CLI token refreshes."""

import base64
import json
import subprocess
import time

import pytest

from granola_mcp.core import cli_token


def _jwt(exp: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode()
    return f"header.{payload.rstrip('=')}.signature"


class _Response:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self._payload


def _forbid_external_io(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("A real network or keychain call was attempted")

    monkeypatch.setattr(cli_token.urllib.request, "urlopen", fail)
    monkeypatch.setattr(cli_token.subprocess, "run", fail)


def test_valid_token_does_not_refresh(monkeypatch):
    _forbid_external_io(monkeypatch)
    refreshes = []
    monkeypatch.setattr(cli_token, "_refresh", lambda bundle: refreshes.append(bundle))
    bundle = {
        "accessToken": _jwt(int(time.time()) + 3600),
        "refreshToken": "refresh-old",
        "clientId": "client-test",
    }

    provider = cli_token.CliTokenProvider(bundle_reader=lambda: bundle)

    assert provider() == bundle["accessToken"]
    assert provider() == bundle["accessToken"]
    assert refreshes == []


def test_expired_token_refreshes_once_and_persists_rotated_token(monkeypatch):
    old_bundle = {
        "accessToken": _jwt(int(time.time()) - 3600),
        "refreshToken": "refresh-old",
        "clientId": "client-test",
    }
    new_access = _jwt(int(time.time()) + 3600)
    workos_calls = []
    events = []
    persisted = []

    def fake_urlopen(request, timeout):
        workos_calls.append((request, timeout))
        return _Response(
            {"access_token": new_access, "refresh_token": "refresh-new"}
        )

    def fake_recovery(bundle):
        events.append(("recovery", dict(bundle)))

    def fake_keychain_write(bundle):
        events.append(("keychain", dict(bundle)))
        persisted.append(dict(bundle))

    monkeypatch.setattr(cli_token.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(cli_token.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("A real keychain call was attempted")
    ))
    monkeypatch.setattr(cli_token, "_write_recovery", fake_recovery)
    monkeypatch.setattr(cli_token, "write_cli_bundle", fake_keychain_write)

    provider = cli_token.CliTokenProvider(bundle_reader=lambda: old_bundle)

    assert provider() == new_access
    assert provider() == new_access
    assert len(workos_calls) == 1
    request, timeout = workos_calls[0]
    assert timeout == 30
    assert request.full_url.endswith("/user_management/authenticate")
    assert json.loads(request.data) == {
        "grant_type": "refresh_token",
        "client_id": "client-test",
        "refresh_token": "refresh-old",
    }
    assert [event[0] for event in events] == ["recovery", "keychain"]
    assert len(persisted) == 1
    assert persisted[0]["refreshToken"] == "refresh-new"
    assert persisted[0]["accessToken"] == new_access


def test_write_back_readback_verification_failure_raises(monkeypatch):
    writes = []

    def fake_security(*args, **kwargs):
        writes.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(cli_token, "_security", fake_security)
    monkeypatch.setattr(
        cli_token,
        "read_cli_bundle",
        lambda: {"refreshToken": "different-token"},
    )

    with pytest.raises(cli_token.GranolaTokenError, match="verification failed"):
        cli_token.write_cli_bundle(
            {
                "accessToken": "access-new",
                "refreshToken": "refresh-new",
                "clientId": "client-test",
            }
        )

    assert len(writes) == 1
    assert writes[0][0] == "add-generic-password"


def test_recovery_file_is_written_before_keychain_failure(monkeypatch):
    events = []
    new_access = _jwt(int(time.time()) + 3600)

    monkeypatch.setattr(
        cli_token.urllib.request,
        "urlopen",
        lambda request, timeout: _Response(
            {"access_token": new_access, "refresh_token": "refresh-new"}
        ),
    )
    monkeypatch.setattr(
        cli_token,
        "_write_recovery",
        lambda bundle: events.append(("recovery", bundle["refreshToken"])),
    )

    def fail_keychain(bundle):
        events.append(("keychain", bundle["refreshToken"]))
        raise cli_token.GranolaTokenError("mock keychain write failed")

    monkeypatch.setattr(cli_token, "write_cli_bundle", fail_keychain)

    with pytest.raises(cli_token.GranolaTokenError, match="mock keychain"):
        cli_token._refresh(
            {
                "accessToken": "expired",
                "refreshToken": "refresh-old",
                "clientId": "client-test",
            }
        )

    assert events == [("recovery", "refresh-new"), ("keychain", "refresh-new")]
