"""Offline tests for public parser lookup and MCP detail routing."""

import pytest

from granola_mcp.core.parser import GranolaParseError, GranolaParser
from granola_mcp.core.public_api import (
    GranolaPublicAPIError,
    GranolaPublicAPINotFoundError,
)
from granola_mcp.core.meeting import (
    Meeting,
    set_summary_loader,
    set_transcript_loader,
)
from granola_mcp.mcp.tools import MCPToolError, MCPTools


FULL_ID = "not_Abcdef12345678"
OTHER_ID = "not_Abcdef12345679"


@pytest.fixture(autouse=True)
def _reset_global_meeting_loaders():
    """Keep parser-installed global lazy loaders isolated between tests."""
    set_transcript_loader(None)
    set_summary_loader(None)
    yield
    set_transcript_loader(None)
    set_summary_loader(None)


def _public_note(note_id=FULL_ID):
    return {
        "id": note_id,
        "title": "Detailed note",
        "created_at": "2026-07-15T14:00:00Z",
        "calendar_event": {
            "scheduled_start_time": "2026-07-15T14:00:00Z",
            "scheduled_end_time": "2026-07-15T14:30:00Z",
        },
        "attendees": [{"name": "Ada"}, {"name": "Grace"}],
        "summary_markdown": "## Decisions\n\nShip it.",
        "transcript": [
            {
                "speaker": {"source": "microphone"},
                "text": "Ship it today",
                "start_time": "2026-07-15T14:00:00Z",
                "end_time": "2026-07-15T14:00:03Z",
            }
        ],
    }


def test_transient_note_error_is_not_negative_cached():
    class API:
        def __init__(self):
            self.calls = 0

        def get_note(self, note_id, include_transcript=True):
            self.calls += 1
            if self.calls == 1:
                raise GranolaPublicAPIError("temporary 500")
            return _public_note(note_id)

    parser = GranolaParser(mode="public")
    parser._public = API()

    with pytest.raises(GranolaPublicAPIError, match="temporary"):
        parser._get_note(FULL_ID)
    assert parser._get_note(FULL_ID)["id"] == FULL_ID
    assert parser._public.calls == 2


def test_definitive_404_is_negative_cached():
    class API:
        def __init__(self):
            self.calls = 0

        def get_note(self, note_id, include_transcript=True):
            self.calls += 1
            raise GranolaPublicAPINotFoundError("missing")

    parser = GranolaParser(mode="public")
    parser._public = API()

    assert parser._get_note(FULL_ID) is None
    assert parser._get_note(FULL_ID) is None
    assert parser._public.calls == 1


def test_public_lazy_transcript_transient_error_retries_same_meeting():
    class API:
        def __init__(self):
            self.calls = 0

        def get_note(self, note_id, include_transcript=True):
            self.calls += 1
            if self.calls == 1:
                raise GranolaPublicAPIError("temporary timeout")
            return _public_note(note_id)

    parser = GranolaParser(mode="public")
    parser._public = API()
    meeting = Meeting({"id": FULL_ID, "title": "Shallow"})

    try:
        with pytest.raises(GranolaPublicAPIError, match="temporary timeout"):
            meeting.has_transcript()
        assert meeting.has_transcript() is True
        assert parser._public.calls == 2
    finally:
        set_transcript_loader(None)
        set_summary_loader(None)


def test_plaintext_parser_clears_stale_public_lazy_loaders():
    class API:
        def get_note(self, *args, **kwargs):
            raise GranolaPublicAPIError("must not run")

    public_parser = GranolaParser(mode="public")
    public_parser._public = API()

    GranolaParser(mode="plaintext", cache_path="unused.json")

    assert Meeting({"id": "legacy", "title": "Local"}).has_transcript() is False


def test_public_lazy_summary_transient_error_retries_same_meeting():
    class API:
        def __init__(self):
            self.calls = 0

        def get_note(self, note_id, include_transcript=True):
            self.calls += 1
            if self.calls == 1:
                raise GranolaPublicAPIError("temporary rate limit")
            return _public_note(note_id)

    parser = GranolaParser(mode="public")
    parser._public = API()
    meeting = Meeting({"id": FULL_ID, "title": "Shallow"})

    with pytest.raises(GranolaPublicAPIError, match="temporary rate limit"):
        _ = meeting.summary
    assert "Decisions" in meeting.summary
    assert parser._public.calls == 2


def test_full_public_id_uses_direct_get_without_list_scan():
    class API:
        def __init__(self):
            self.detail_calls = []

        def get_note(self, note_id, include_transcript=True):
            self.detail_calls.append((note_id, include_transcript))
            return _public_note(note_id)

        def get_notes(self, **kwargs):
            raise AssertionError("full IDs must not scan the note list")

    parser = GranolaParser(mode="public")
    parser._public = API()

    meeting = parser.get_meeting_by_id(FULL_ID)
    assert meeting["id"] == FULL_ID
    assert meeting["transcript_data"][0]["text"] == "Ship it today"
    assert parser._public.detail_calls == [(FULL_ID, True)]


def test_full_public_id_404_does_not_fall_back_to_prefix_scan():
    class API:
        def __init__(self):
            self.detail_calls = 0
            self.list_calls = 0

        def get_note(self, note_id, include_transcript=True):
            self.detail_calls += 1
            raise GranolaPublicAPINotFoundError("missing")

        def get_notes(self, **kwargs):
            self.list_calls += 1
            return [_public_note(FULL_ID)]

    parser = GranolaParser(mode="public")
    parser._public = API()

    with pytest.raises(GranolaParseError, match="Meeting not found"):
        parser.get_meeting_by_id(FULL_ID)
    assert parser._public.detail_calls == 1
    assert parser._public.list_calls == 0


def test_public_prefix_rejects_ambiguous_and_missing_matches():
    class API:
        last_get_notes_truncated = False

        def get_notes(self, **kwargs):
            return [_public_note(FULL_ID), _public_note(OTHER_ID)]

        def get_note(self, note_id, include_transcript=True):
            raise AssertionError("ambiguous/missing prefixes must not fetch detail")

    parser = GranolaParser(mode="public")
    parser._public = API()

    with pytest.raises(GranolaParseError, match="ambiguous"):
        parser.get_meeting_by_id("not_Abcdef")
    with pytest.raises(GranolaParseError, match="No meeting matches"):
        parser.get_meeting_by_id("not_missing")


def test_public_prefix_hydrates_the_only_match():
    class API:
        last_get_notes_truncated = False

        def __init__(self):
            self.detail_ids = []

        def get_notes(self, **kwargs):
            return [
                {"id": FULL_ID, "title": "Shallow"},
                {"id": "not_Zbcdef12345679", "title": "Other"},
            ]

        def get_note(self, note_id, include_transcript=True):
            self.detail_ids.append(note_id)
            return _public_note(note_id)

    parser = GranolaParser(mode="public")
    parser._public = API()

    meeting = parser.get_meeting_by_id("not_Abcdef12345678"[:-1])
    assert meeting["title"] == "Detailed note"
    assert parser._public.detail_ids == [FULL_ID]


def test_mcp_exact_id_tools_never_scan_or_reload_the_list():
    class Parser:
        mode = "public"

        def __init__(self):
            self.detail_ids = []

        def get_meeting_by_id(self, meeting_id):
            self.detail_ids.append(meeting_id)
            from granola_mcp.core.public_api import note_to_meeting

            return note_to_meeting(_public_note(meeting_id))

        def get_meetings(self, **kwargs):
            raise AssertionError("single-ID MCP tools must not scan the list")

        def reload(self):
            raise AssertionError("single-ID MCP tools must not reload the parser")

    parser = Parser()
    tools = MCPTools(parser)

    assert tools.get_meeting(FULL_ID)["id"] == FULL_ID
    assert tools.get_transcript(FULL_ID)["full_text"] == "Ship it today"
    assert tools.get_meeting_notes(FULL_ID)["meeting_id"] == FULL_ID
    assert tools.export_meeting(FULL_ID)["meeting_id"] == FULL_ID
    assert parser.detail_ids == [FULL_ID] * 4


def test_mcp_meeting_list_cache_avoids_reload_between_calls():
    class Parser:
        mode = "public"

        def __init__(self):
            self.list_calls = 0
            self.reload_calls = 0

        def get_meetings(self, **kwargs):
            self.list_calls += 1
            return [{"id": FULL_ID, "title": "Shallow"}]

        def reload(self):
            self.reload_calls += 1

    parser = Parser()
    tools = MCPTools(parser)

    assert tools._get_meetings()[0].id == FULL_ID
    assert tools._get_meetings()[0].id == FULL_ID
    assert parser.list_calls == 1
    assert parser.reload_calls == 0

    tools._get_meetings(force_reload=True)
    assert parser.list_calls == 2
    assert parser.reload_calls == 1


def test_mcp_bulk_analyses_disclose_public_source_cap():
    from granola_mcp.core.public_api import note_to_meeting

    class Parser:
        mode = "public"
        last_public_list_truncated = True

        def get_meetings(self, **kwargs):
            return [note_to_meeting(_public_note())]

        def get_meeting_by_id(self, meeting_id):
            return note_to_meeting(_public_note(meeting_id))

    tools = MCPTools(Parser())
    responses = [
        tools.list_participants(),
        tools.get_statistics("frequency"),
        tools.analyze_patterns("time"),
    ]

    for response in responses:
        assert response["retrieval"]["source_truncated"] is True
        assert response["retrieval"]["source_max_notes"] == 500


def test_mcp_exact_id_preserves_plaintext_reload():
    class Parser:
        mode = "plaintext"

        def __init__(self):
            self.reload_calls = 0

        def reload(self):
            self.reload_calls += 1

        def get_meeting_by_id(self, meeting_id):
            return {"id": meeting_id, "title": "Fresh plaintext note"}

    parser = Parser()
    meeting = MCPTools(parser)._get_single_meeting("legacy-id")

    assert meeting.title == "Fresh plaintext note"
    assert parser.reload_calls == 1


def test_mcp_public_hydration_surfaces_transient_detail_errors():
    class Parser:
        mode = "public"

        def get_meetings(self, **kwargs):
            return [
                {
                    "id": FULL_ID,
                    "title": "Shallow",
                    "start_time": "2026-07-15T14:00:00Z",
                }
            ]

        def get_meeting_by_id(self, meeting_id):
            raise GranolaPublicAPIError("temporary 503")

    with pytest.raises(MCPToolError, match="temporary 503"):
        MCPTools(Parser()).search_meetings(
            query="ship",
            from_date="2026-07-01",
            to_date="2026-07-31",
        )


def test_public_cache_info_separates_configuration_and_reachability(monkeypatch):
    monkeypatch.setattr("granola_mcp.core.public_api.has_api_key", lambda: True)
    parser = GranolaParser(mode="public")
    parser._public = type(
        "API",
        (),
        {
            "last_get_notes_truncated": False,
            "get_notes": lambda self, **kwargs: [],
        },
    )()

    before = parser.get_cache_info()
    assert before["configured"] is True
    assert before["reachable"] is None
    assert before["readable"] is False
    assert before["valid_structure"] is False

    assert parser.get_meetings() == []
    after = parser.get_cache_info()
    assert after["configured"] is True
    assert after["reachable"] is True
    assert after["valid_structure"] is True
    assert after["meeting_count"] == 0


def test_public_cache_info_records_failed_list_without_retrying(monkeypatch):
    monkeypatch.setattr("granola_mcp.core.public_api.has_api_key", lambda: True)
    parser = GranolaParser(mode="public")

    def fail(**kwargs):
        raise GranolaPublicAPIError("offline")

    parser._public = type("API", (), {"get_notes": lambda self, **kwargs: fail(**kwargs)})()
    with pytest.raises(GranolaPublicAPIError, match="offline"):
        parser.get_meetings()

    info = parser.get_cache_info()
    assert info["configured"] is True
    assert info["reachable"] is False
    assert info["readable"] is False
    assert info["meeting_count"] == 0


def test_public_rate_limiter_spaces_request_starts(monkeypatch):
    from granola_mcp.core import public_api

    clock = [0.0]
    sleeps = []

    monkeypatch.setattr(public_api.time, "monotonic", lambda: clock[0])

    def advance(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(public_api.time, "sleep", advance)
    client = public_api.GranolaPublicAPI(
        key_provider=lambda: "grn_test",
        requests_per_second=5,
    )

    client._pace_request()
    client._pace_request()
    client._pace_request()

    assert sleeps == pytest.approx([0.2, 0.2])
    assert clock[0] == pytest.approx(0.4)
