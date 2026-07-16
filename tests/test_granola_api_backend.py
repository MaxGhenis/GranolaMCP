"""
Tests for the encrypted-storage / API backend added for modern Granola.

All tests are offline and deterministic: they never touch the macOS keychain
or the network. Crypto is validated with a self-produced ciphertext, and the
API/loader plumbing is exercised with fakes.
"""

import gzip
import json
import os

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from granola_mcp.utils import granola_crypto as gc
from granola_mcp.core import parser as parser_mod
from granola_mcp.core.parser import GranolaParser, _participants_from_people, _resolve_mode
from granola_mcp.core.api_client import GranolaAPI
from granola_mcp.core.meeting import Meeting, set_transcript_loader, set_summary_loader


def _gcm_encrypt(plaintext: bytes, key: bytes) -> bytes:
    """Encrypt in Granola's framing: nonce(12) || ciphertext || tag(16)."""
    nonce = b"\x00" * 12
    enc = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    ct = enc.update(plaintext) + enc.finalize()
    return nonce + ct + enc.tag


def test_decrypt_blob_roundtrip():
    key = bytes(range(32))
    payload = json.dumps({"hello": "world", "n": 42}).encode()
    blob = _gcm_encrypt(payload, key)
    assert gc.decrypt_blob(blob, key) == payload


def test_decrypt_blob_gzip_roundtrip():
    key = b"\x01" * 32
    payload = b'{"big":"' + b"x" * 5000 + b'"}'
    blob = _gcm_encrypt(gzip.compress(payload), key)
    assert gc.decrypt_blob(blob, key) == payload


def test_participants_from_people_dict():
    people = {
        "creator": {"name": "Max Ghenis", "email": "max@policyengine.org"},
        "attendees": [
            {"email": "a@example.com"},
            {"details": {"person": {"name": {"fullName": "Jane Doe"}}}},
            {"name": "Max Ghenis"},  # duplicate should be ignored
        ],
    }
    assert _participants_from_people(people) == [
        "Max Ghenis", "a@example.com", "Jane Doe",
    ]


def test_participants_from_people_list_and_empty():
    assert _participants_from_people(["alice", {"name": "bob"}]) == ["alice", "bob"]
    assert _participants_from_people(None) == []
    assert _participants_from_people({}) == []


def test_resolve_mode(monkeypatch):
    monkeypatch.delenv("GRANOLA_MODE", raising=False)
    monkeypatch.setattr(
        "granola_mcp.core.cli_token.cli_credentials_available", lambda: False
    )
    monkeypatch.setattr(
        "granola_mcp.core.public_api.has_api_key", lambda: False
    )
    assert _resolve_mode("/x/cache-v6.json.enc") == "api"
    assert _resolve_mode(None) == "api"
    monkeypatch.setenv("GRANOLA_MODE", "plaintext")
    assert _resolve_mode("/x/cache-v6.json.enc") == "plaintext"


def test_get_summary_html_skips_link_panels():
    api = GranolaAPI(token_provider=lambda: "token")
    api.get_document_panels = lambda doc_id: [
        {"original_content": "<hr><a href='x'>link</a>"},   # link-only -> skipped
        {"original_content": "<h3>Summary</h3><ul><li>point</li></ul>"},
        {"original_content": "   "},                          # blank -> skipped
    ]
    html = api.get_summary_html("doc1")
    assert "Summary" in html and "<hr>" not in html


def test_meeting_lazy_transcript_loader():
    set_transcript_loader(lambda mid: [
        {"text": "Hello there", "source": "microphone", "start_timestamp": "2026-05-29T00:00:00Z"},
        {"text": "General Kenobi", "source": "system", "start_timestamp": "2026-05-29T00:00:01Z"},
    ] if mid == "m1" else None)
    try:
        m = Meeting({"id": "m1", "title": "T"})
        assert m.has_transcript() is True
        assert "General Kenobi" in m.transcript.full_text
        # A meeting the loader doesn't know about has no transcript and doesn't crash.
        assert Meeting({"id": "other"}).has_transcript() is False
    finally:
        set_transcript_loader(None)


def test_meeting_lazy_summary_loader():
    set_summary_loader(lambda mid: "<h3>Decisions</h3><ul><li>Ship it</li></ul>")
    try:
        m = Meeting({"id": "m1", "title": "T"})
        summary = m.summary
        assert "Decisions" in summary
        assert "Ship it" in summary
    finally:
        set_summary_loader(None)


def test_meeting_inline_summary_preferred_over_loader():
    calls = []
    set_summary_loader(lambda mid: calls.append(mid) or "<p>panel</p>")
    try:
        # An inline overview should be used without invoking the panel loader.
        m = Meeting({"id": "m1", "overview": "Inline overview text"})
        assert m.summary == "Inline overview text"
        assert calls == []
    finally:
        set_summary_loader(None)


def test_parser_get_meetings_api_shapes_documents(monkeypatch):
    """get_meetings (api mode) maps people->participants and attaches local transcripts."""
    sample_docs = [
        {"id": "d1", "title": "Standup", "created_at": "2026-05-29T16:00:00.000Z",
         "people": {"creator": {"name": "Max Ghenis"},
                    "attendees": [{"email": "teammate@example.com"}]}},
        {"id": "d2", "title": "1:1", "created_at": "2026-05-28T16:00:00.000Z", "people": {}},
    ]
    fake_tx = {"d1": [{"text": "hi", "source": "microphone"}]}

    p = GranolaParser(mode="api")
    monkeypatch.setattr(p, "_ensure_api", lambda: type("A", (), {
        "get_documents": lambda self, **kw: sample_docs,
    })())
    monkeypatch.setattr(p, "_local_transcripts", lambda force=False: fake_tx)

    meetings = p.get_meetings()
    assert [m["id"] for m in meetings] == ["d1", "d2"]
    assert meetings[0]["participants"] == ["Max Ghenis", "teammate@example.com"]
    assert meetings[0]["transcript_data"] == fake_tx["d1"]
    assert "transcript_data" not in meetings[1]
