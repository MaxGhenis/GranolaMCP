"""Offline tests for the official Granola public API backend."""

import io
import json
import subprocess
import urllib.error
import urllib.parse
from types import SimpleNamespace

import pytest

from granola_mcp.core.meeting import Meeting
from granola_mcp.core import public_api
from granola_mcp.core.public_api import (
    GranolaPublicAPI,
    GranolaPublicAPIError,
    GranolaPublicAPINotFoundError,
    has_api_key,
    normalize_transcript,
    note_to_meeting,
    resolve_api_key,
)


class _Response:
    """Small urlopen-compatible response used by request tests."""

    def __init__(self, payload, headers=None, raw=None):
        self._raw = raw if raw is not None else json.dumps(payload).encode("utf-8")
        self.headers = headers or {}

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


@pytest.fixture(autouse=True)
def _block_external_io(monkeypatch):
    """Make an unmocked network or keychain access fail immediately."""

    def blocked_urlopen(*args, **kwargs):
        raise AssertionError("test attempted an unmocked network request")

    def blocked_subprocess(*args, **kwargs):
        raise AssertionError("test attempted an unmocked keychain request")

    monkeypatch.setattr(public_api.urllib.request, "urlopen", blocked_urlopen)
    monkeypatch.setattr(public_api.subprocess, "run", blocked_subprocess)


def _query(path):
    """Parse a request path's query into single-value strings."""
    return {
        key: values[0]
        for key, values in urllib.parse.parse_qs(
            urllib.parse.urlsplit(path).query
        ).items()
    }


def test_get_notes_uses_page_size_has_more_filters_and_deduplicates(monkeypatch):
    pages = [
        {
            "notes": [{"id": f"not_{index}"} for index in range(30)],
            "hasMore": True,
            "cursor": "page two/+",
        },
        {
            "notes": [{"id": "not_29"}, {"id": "not_30"}],
            "has_more": False,
            "cursor": "unused",
        },
    ]
    paths = []
    client = GranolaPublicAPI(key_provider=lambda: "grn_test")

    def request(path):
        paths.append(path)
        return pages.pop(0)

    monkeypatch.setattr(client, "_request", request)
    notes = client.get_notes(
        max_notes=31,
        page_size=100,
        created_after="2026-01-01T00:00:00Z",
        created_before="2026-02-01T00:00:00Z",
        updated_after="2026-01-02T00:00:00Z",
    )

    assert [note["id"] for note in notes] == [
        *(f"not_{index}" for index in range(30)),
        "not_30",
    ]
    assert [_query(path)["page_size"] for path in paths] == ["30", "1"]
    assert "limit" not in _query(paths[0])
    assert _query(paths[1])["cursor"] == "page two/+"
    for path in paths:
        assert _query(path)["created_after"] == "2026-01-01T00:00:00Z"
        assert _query(path)["created_before"] == "2026-02-01T00:00:00Z"
        assert _query(path)["updated_after"] == "2026-01-02T00:00:00Z"
    assert client.last_get_notes_truncated is False


def test_get_notes_stops_on_repeated_cursor_and_deduplicates(monkeypatch, caplog):
    pages = [
        {"notes": [{"id": "not_a"}], "hasMore": True, "cursor": "same"},
        {
            "notes": [{"id": "not_a"}, {"id": "not_b"}],
            "hasMore": True,
            "cursor": "same",
        },
    ]
    calls = []
    client = GranolaPublicAPI(key_provider=lambda: "grn_test")

    def request(path):
        calls.append(path)
        return pages.pop(0)

    monkeypatch.setattr(client, "_request", request)
    notes = client.get_notes(max_notes=10)

    assert [note["id"] for note in notes] == ["not_a", "not_b"]
    assert len(calls) == 2
    assert client.last_get_notes_truncated is True
    assert "repeated pagination cursor" in caplog.text


@pytest.mark.parametrize(
    "response",
    [
        {"notes": [{"id": "not_a"}], "hasMore": False, "cursor": "ignored"},
        {"notes": [{"id": "not_a"}], "hasMore": True},
    ],
)
def test_get_notes_stops_without_both_has_more_and_cursor(monkeypatch, response):
    client = GranolaPublicAPI(key_provider=lambda: "grn_test")
    calls = []

    def request(path):
        calls.append(path)
        return response

    monkeypatch.setattr(client, "_request", request)
    assert client.get_notes(max_notes=10) == [{"id": "not_a"}]
    assert len(calls) == 1


def test_get_notes_rejects_non_list_notes(monkeypatch):
    client = GranolaPublicAPI(key_provider=lambda: "grn_test")
    monkeypatch.setattr(
        client,
        "_request",
        lambda path: {"notes": {"id": "not_bad"}, "hasMore": False},
    )
    assert client.get_notes() == []


def test_get_notes_discloses_cap(monkeypatch, caplog):
    client = GranolaPublicAPI(key_provider=lambda: "grn_test")
    monkeypatch.setattr(
        client,
        "_request",
        lambda path: {
            "notes": [{"id": "not_a"}, {"id": "not_b"}],
            "hasMore": True,
            "cursor": "more",
        },
    )

    assert len(client.get_notes(max_notes=2)) == 2
    assert client.last_get_notes_truncated is True
    assert "reached the 2-note cap" in caplog.text


@pytest.mark.parametrize("value", [None, [], {}, "", 0])
def test_normalize_transcript_empty_or_wrong_top_level(value):
    assert normalize_transcript(value) == []


def test_normalize_transcript_skips_non_dict_members():
    segment = {
        "text": "Hello",
        "start_time": "2026-01-01T00:00:00Z",
        "end_time": "2026-01-01T00:00:01Z",
        "speaker": "Alex",
    }
    assert normalize_transcript([None, "x", 1, [], segment]) == [
        {
            "text": "Hello",
            "speaker": "Alex",
            "start_timestamp": "2026-01-01T00:00:00+00:00",
            "end_timestamp": "2026-01-01T00:00:01+00:00",
            "start_time": 0.0,
            "end_time": 1.0,
        }
    ]
    assert normalize_transcript(["x"]) == []


def test_normalize_transcript_handles_mixed_timezones_and_missing_fields():
    normalized = normalize_transcript(
        [
            {
                "text": "First",
                "start_time": "2026-01-01T00:00:00",
                "end_time": "2026-01-01T00:00:01Z",
                "speaker": {
                    "name": "Alex",
                    "source": "microphone",
                    "diarization_label": "Speaker 1",
                },
            },
            {
                "text": "Second",
                "start_time": "2026-01-01T01:00:02+01:00",
                "end_time": "not-a-time",
                "speaker": {"source": "system"},
                "diarization_label": "Speaker 2",
            },
            {"text": None, "speaker": None},
        ]
    )

    assert normalized[0]["source"] == "microphone"
    assert normalized[0]["diarization_label"] == "Speaker 1"
    assert normalized[0]["speaker"] == "Alex"
    assert normalized[1]["source"] == "system"
    assert normalized[1]["diarization_label"] == "Speaker 2"
    assert normalized[1]["speaker"] == "Speaker 2"
    assert normalized[1]["start_time"] == 2.0
    assert "end_timestamp" not in normalized[1]
    assert "end_time" not in normalized[1]
    assert normalized[2] == {"text": "", "speaker": "Unknown"}


def test_note_to_meeting_maps_calendar_title_summary_and_folder():
    mapped = note_to_meeting(
        {
            "id": "not_1d3tmYTlCICgjy",
            "title": None,
            "created_at": "2026-01-01T18:00:00Z",
            "summary_markdown": "## Decisions\n\n- Ship it",
            "calendar_event": {
                "event_title": "Calendar title",
                "scheduled_start_time": "2026-02-01T18:00:00Z",
                "scheduled_end_time": "2026-02-01T19:30:00Z",
            },
            "folder_membership": [
                {"folder": {"name": "Customer calls"}},
                {"folder_name": "Important"},
            ],
        }
    )
    meeting = Meeting(mapped)

    assert mapped["start_time"] == "2026-02-01T18:00:00Z"
    assert mapped["end_time"] == "2026-02-01T19:30:00Z"
    assert mapped["google_calendar_event"]["event_title"] == "Calendar title"
    assert mapped["ai_summary"] == "## Decisions\n\n- Ship it"
    assert mapped["folder_names"] == ["Customer calls", "Important"]
    assert mapped["folder_name"] == "Customer calls"
    assert meeting.title == "Calendar title"
    assert meeting.start_time.month == 2
    assert meeting.duration.total_seconds() == 90 * 60


@pytest.mark.parametrize("title", [None, "", "   "])
def test_note_to_meeting_uses_untitled_when_titles_are_empty(title):
    mapped = note_to_meeting({"id": "not_1d3tmYTlCICgjy", "title": title})
    assert mapped["title"] == "Untitled"
    assert Meeting(mapped).title == "Untitled"


def test_resolve_api_key_strips_and_validates_environment(monkeypatch):
    monkeypatch.setattr(public_api, "_keychain_api_key", lambda: None)

    monkeypatch.setenv("GRANOLA_API_KEY", "   ")
    assert resolve_api_key() is None
    assert has_api_key() is False

    monkeypatch.setenv("GRANOLA_API_KEY", "not-a-granola-key")
    assert resolve_api_key() is None
    assert has_api_key() is False

    monkeypatch.setenv("GRANOLA_API_KEY", "  grn_valid  ")
    assert resolve_api_key() == "grn_valid"
    assert has_api_key() is True


def test_resolve_api_key_prefers_valid_env_and_blank_falls_back(monkeypatch):
    calls = []

    def keychain():
        calls.append(True)
        return "grn_keychain"

    monkeypatch.setattr(public_api, "_keychain_api_key", keychain)
    monkeypatch.setenv("GRANOLA_API_KEY", "grn_environment")
    assert resolve_api_key() == "grn_environment"
    assert calls == []

    monkeypatch.setenv("GRANOLA_API_KEY", "  ")
    assert resolve_api_key() == "grn_keychain"
    assert calls == [True]


def test_keychain_account_lookup_continues_through_fallbacks(monkeypatch):
    monkeypatch.delenv("GRANOLA_API_KEY", raising=False)
    monkeypatch.setenv("GRANOLA_API_KEY_ACCOUNT", "configured")
    monkeypatch.setattr(public_api.getpass, "getuser", lambda: "current")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[-1] == "configured":
            raise subprocess.TimeoutExpired(command, 30)
        if command[-1] == "current":
            return SimpleNamespace(returncode=0, stdout="invalid\n", stderr="")
        return SimpleNamespace(returncode=0, stdout=" grn_fallback \n", stderr="")

    monkeypatch.setattr(public_api.subprocess, "run", run)
    assert resolve_api_key() == "grn_fallback"
    assert commands[0][-2:] == ["-a", "configured"]
    assert commands[1][-2:] == ["-a", "current"]
    assert "-a" not in commands[2]


def test_get_note_preserves_include_transcript_and_caches_key(monkeypatch):
    requests = []
    key_calls = []

    def urlopen(request, timeout):
        requests.append(request)
        return _Response({"note": {"id": "not_1d3tmYTlCICgjy"}})

    def key_provider():
        key_calls.append(True)
        return "  grn_cached  "

    monkeypatch.setattr(public_api.urllib.request, "urlopen", urlopen)
    client = GranolaPublicAPI(
        key_provider=key_provider,
        retries=1,
        requests_per_second=0,
    )

    client.get_note("not_1d3tmYTlCICgjy", include_transcript=True)
    client.get_note("not_1d3tmYTlCICgjy", include_transcript=False)

    assert requests[0].full_url.endswith(
        "/v1/notes/not_1d3tmYTlCICgjy?include=transcript"
    )
    assert requests[1].full_url.endswith("/v1/notes/not_1d3tmYTlCICgjy")
    assert requests[0].get_header("Authorization") == "Bearer grn_cached"
    assert key_calls == [True]


def test_get_note_raises_distinct_not_found_error(monkeypatch):
    def urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            404,
            "not found",
            {},
            io.BytesIO(b'{"error":"missing"}'),
        )

    monkeypatch.setattr(public_api.urllib.request, "urlopen", urlopen)
    client = GranolaPublicAPI(
        key_provider=lambda: "grn_test",
        retries=1,
        requests_per_second=0,
    )
    with pytest.raises(GranolaPublicAPINotFoundError):
        client.get_note("not_1d3tmYTlCICgjy")


def test_invalid_json_is_retried_and_wrapped(monkeypatch):
    calls = []

    def urlopen(request, timeout):
        calls.append(request)
        return _Response(None, raw=b"not-json")

    monkeypatch.setattr(public_api.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(public_api.time, "sleep", lambda seconds: None)
    client = GranolaPublicAPI(
        key_provider=lambda: "grn_test",
        retries=2,
        requests_per_second=0,
    )

    with pytest.raises(GranolaPublicAPIError, match="invalid response"):
        client.get_note("not_1d3tmYTlCICgjy")
    assert len(calls) == 2
