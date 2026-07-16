"""Offline regression tests for remote-backend CLI behavior."""

import argparse
import importlib
import re
import threading
import time

import pytest

from granola_mcp.cli.commands.collect import CollectCommand
from granola_mcp.cli.commands._hydration import hydrate_meetings
from granola_mcp.cli.commands.json import JsonCommand
from granola_mcp.cli.commands.list import ListCommand
from granola_mcp.cli.commands.show import ShowCommand
from granola_mcp.cli.commands.stats import StatsCommand
from granola_mcp.cli.formatters.markdown import (
    format_summary_section,
    format_transcript_section,
)
from granola_mcp.core.meeting import Meeting, set_transcript_loader
from granola_mcp.core.transcript import Transcript


@pytest.mark.parametrize("mode", ["api", "public"])
def test_main_remote_backends_do_not_require_cache(
    monkeypatch, tmp_path, mode
):
    """Remote startup checks credentials without reading a local cache."""
    main_module = importlib.import_module("granola_mcp.cli.main")
    real_list_command = main_module.ListCommand
    calls = {"validate_file": 0, "load_cache": 0, "command": 0}

    class FakeParser:
        def __init__(self, cache_path):
            self.cache_path = cache_path
            self.mode = mode

        def validate_cache_structure(self):
            return True

        def load_cache(self):
            calls["load_cache"] += 1
            raise AssertionError("Remote startup must not load the local cache")

    class FakeCommand:
        add_arguments = staticmethod(real_list_command.add_arguments)

        def __init__(self, parser, args):
            assert parser.mode == mode

        def execute(self):
            calls["command"] += 1
            return 0

    def validate_file(_path):
        calls["validate_file"] += 1
        return False

    monkeypatch.setattr(main_module, "GranolaParser", FakeParser)
    monkeypatch.setattr(main_module, "ListCommand", FakeCommand)
    monkeypatch.setattr(main_module, "validate_cache_file", validate_file)

    missing = tmp_path / "does-not-exist.json.enc"
    assert main_module.main([
        "--cache-path", str(missing), "list", "--format", "ids"
    ]) == 0
    assert calls == {"validate_file": 0, "load_cache": 0, "command": 1}


def test_main_plaintext_keeps_cache_validation(monkeypatch, tmp_path):
    """Legacy plaintext startup still validates and parses the cache first."""
    main_module = importlib.import_module("granola_mcp.cli.main")
    real_list_command = main_module.ListCommand
    calls = []

    class FakeParser:
        mode = "plaintext"

        def __init__(self, cache_path):
            self.cache_path = cache_path

        def load_cache(self):
            calls.append("load")
            return {"state": {}}

        def get_cache_info(self):
            return {"meeting_count": 0}

    class FakeCommand:
        add_arguments = staticmethod(real_list_command.add_arguments)

        def __init__(self, parser, args):
            pass

        def execute(self):
            calls.append("command")
            return 0

    monkeypatch.setattr(main_module, "GranolaParser", FakeParser)
    monkeypatch.setattr(main_module, "ListCommand", FakeCommand)
    monkeypatch.setattr(
        main_module,
        "validate_cache_file",
        lambda path: calls.append("validate") or True,
    )

    cache_path = tmp_path / "cache.json"
    assert main_module.main([
        "--cache-path", str(cache_path), "list", "--format", "ids"
    ]) == 0
    assert calls == ["validate", "load", "command"]


@pytest.mark.parametrize("mode", ["api", "public"])
def test_json_rejects_remote_raw_cache(capsys, mode):
    """JSON does not represent a remote backend as an empty cache."""
    class FakeParser:
        def __init__(self):
            self.mode = mode

        def load_cache(self):
            raise AssertionError("Raw cache must not be loaded")

    args = argparse.Namespace(
        compact=False,
        indent=2,
        sort_keys=False,
        verbose=False,
    )
    assert JsonCommand(FakeParser(), args).execute() == 1
    assert f"Raw cache unavailable in the {mode} backend" in capsys.readouterr().err


def test_markdown_preserves_public_summary_markdown():
    """Trusted public summary Markdown remains renderable Markdown."""
    meeting = Meeting({
        "id": "not_1",
        "summary_markdown": "## Decisions\n- Ship it",
        "ai_summary": "## Decisions\n- Ship it",
    })
    rendered = format_summary_section(meeting)
    assert "## Decisions\n- Ship it" in rendered
    assert "\\#\\# Decisions" not in rendered


def test_no_speakers_omits_labels_in_show_and_markdown(capsys):
    """No-speaker output contains only timestamp and transcript text."""
    meeting = Meeting({
        "id": "not_1",
        "transcript_data": [{
            "speaker": "microphone",
            "text": "Hello there",
            "start_time": 0,
        }],
    })
    parser = type("Parser", (), {"mode": "public"})()
    args = argparse.Namespace(
        speaker=None,
        no_speakers=True,
        timestamps=True,
        verbose=False,
    )

    ShowCommand(parser, args)._show_transcript(meeting)
    shown = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    assert "[00:00] Hello there" in shown
    assert "microphone:" not in shown
    assert "Unknown:" not in shown

    exported = format_transcript_section(
        meeting,
        include_speakers=False,
        include_timestamps=True,
    )
    assert "*[00:00]* Hello there" in exported
    assert "microphone:**" not in exported
    assert "Unknown:**" not in exported


def test_public_show_notes_explains_capability_gap(capsys):
    """An explicit notes request explains the public API limitation."""
    parser = type("Parser", (), {"mode": "public"})()
    args = argparse.Namespace(verbose=False)
    ShowCommand(parser, args)._show_human_notes(Meeting({"id": "not_1"}))
    assert (
        "Human notes are not exposed by the public API backend"
        in capsys.readouterr().out
    )


@pytest.mark.parametrize("with_diarization", [False, True])
def test_collect_rejects_ambiguous_all_microphone_transcript(
    capsys, with_diarization
):
    """Collect skips all-microphone audio when source cannot identify the user."""
    parser = type("Parser", (), {"mode": "public"})()
    args = argparse.Namespace(min_words=1)
    command = CollectCommand(parser, args)
    segments = [
        {"text": "First speaker", "source": "microphone"},
        {"text": "Second speaker", "source": "microphone"},
    ]
    if with_diarization:
        segments[0]["diarization_label"] = "speaker_1"
        segments[1]["diarization_label"] = "speaker_2"
    transcript = Transcript(segments)
    assert command._filter_my_words(transcript) == []
    assert "cannot identify which speaker is you" in capsys.readouterr().err


def test_collect_keeps_microphone_when_source_is_distinguishing():
    """Collect still extracts microphone segments when other sources exist."""
    parser = type("Parser", (), {"mode": "public"})()
    args = argparse.Namespace(min_words=1)
    command = CollectCommand(parser, args)
    transcript = Transcript([
        {"text": "My words", "source": "microphone"},
        {"text": "Their words", "source": "system"},
    ])
    assert [s.text for s in command._filter_my_words(transcript)] == ["My words"]


def test_collect_explains_missing_source_metadata(capsys):
    """Collect explicitly skips transcripts that cannot identify the user."""
    parser = type("Parser", (), {"mode": "public"})()
    args = argparse.Namespace(min_words=1)
    command = CollectCommand(parser, args)
    assert command._filter_my_words(Transcript([{"text": "Unknown speaker"}])) == []
    assert "without source metadata" in capsys.readouterr().err


def _list_args(**overrides):
    values = {
        "last": None,
        "from_date": None,
        "to_date": None,
        "title_contains": None,
        "participant": None,
        "folder": None,
        "sort_by": "date",
        "reverse": False,
        "limit": None,
        "format": "ids",
        "no_header": False,
        "verbose": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_list_pushes_iso_date_bounds_to_public_listing(capsys):
    """List applies official API date bounds before its local date filter."""
    captured_filters = []

    class FakeParser:
        mode = "public"

        def get_meetings(self, debug=False, **kwargs):
            captured_filters.append(kwargs)
            return []

    command = ListCommand(
        FakeParser(),
        _list_args(from_date="2026-01-01", to_date="2026-01-31"),
    )
    assert command.execute() == 0
    assert captured_filters == [{
        "created_after": "2026-01-01T00:00:00-06:00",
        "created_before": "2026-01-31T00:00:00-06:00",
    }]
    capsys.readouterr()


def test_list_duration_sort_hydrates_with_paced_request_starts(capsys):
    """Duration sorting uses paced full details rather than shallow rows."""
    request_starts = []
    details = {
        "not_long": {
            "id": "not_long",
            "start_time": "2026-01-01T12:00:00Z",
            "end_time": "2026-01-01T12:02:00Z",
        },
        "not_short": {
            "id": "not_short",
            "start_time": "2026-01-01T12:00:00Z",
            "end_time": "2026-01-01T12:01:00Z",
        },
    }

    class FakeParser:
        mode = "public"

        def get_meetings(self, debug=False, **kwargs):
            return [{"id": "not_long"}, {"id": "not_short"}]

        def get_meeting_by_id(self, meeting_id):
            request_starts.append(time.monotonic())
            return details[meeting_id]

    command = ListCommand(FakeParser(), _list_args(sort_by="duration"))
    assert command.execute() == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["not_short", "not_long"]
    assert "Loading details for 2 meetings" in captured.err
    assert request_starts[1] - request_starts[0] >= 0.19


def test_internal_api_hydration_is_serial(capsys):
    """Internal token refresh cannot be entered by concurrent hydration."""
    state = {"active": 0, "max_active": 0}
    lock = threading.Lock()

    def load_transcript(meeting_id):
        with lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        time.sleep(0.01)
        with lock:
            state["active"] -= 1
        return [{"text": meeting_id, "source": "microphone"}]

    parser = type("Parser", (), {"mode": "api"})()
    set_transcript_loader(load_transcript)
    try:
        hydrated = hydrate_meetings(
            parser,
            [Meeting({"id": "one"}), Meeting({"id": "two"})],
        )
    finally:
        set_transcript_loader(None)

    assert [meeting.id for meeting in hydrated] == ["one", "two"]
    assert state["max_active"] == 1
    assert "Loading details for 2 meetings" in capsys.readouterr().err


def _stats_args(**overrides):
    values = {
        "last": None,
        "from_date": None,
        "to_date": None,
        "meetings_per_day": False,
        "meetings_per_week": False,
        "meetings_per_month": False,
        "duration_distribution": False,
        "participant_frequency": False,
        "time_patterns": False,
        "word_analysis": False,
        "summary": False,
        "all": False,
        "folder": None,
        "no_charts": True,
        "chart_width": None,
        "verbose": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_stats_pushes_iso_date_bounds_to_public_listing(capsys):
    """Stats applies official API date bounds before local analysis."""
    captured_filters = []

    class FakeParser:
        mode = "public"

        def get_meetings(self, **kwargs):
            captured_filters.append(kwargs)
            return []

    command = StatsCommand(
        FakeParser(),
        _stats_args(
            from_date="2026-01-01",
            to_date="2026-01-31",
            meetings_per_day=True,
        ),
    )
    assert command.execute() == 0
    assert captured_filters == [{
        "created_after": "2026-01-01T00:00:00-06:00",
        "created_before": "2026-01-31T00:00:00-06:00",
    }]
    capsys.readouterr()


def test_stats_duration_distribution_uses_hydrated_snapshot(capsys):
    """Duration statistics are calculated after public detail hydration."""
    calls = []

    class FakeParser:
        mode = "public"

        def get_meetings(self):
            return [{"id": "not_1"}]

        def get_meeting_by_id(self, meeting_id):
            calls.append(meeting_id)
            return {
                "id": meeting_id,
                "start_time": "2026-01-01T12:00:00Z",
                "end_time": "2026-01-01T13:30:00Z",
            }

    command = StatsCommand(
        FakeParser(),
        _stats_args(duration_distribution=True),
    )
    assert command.execute() == 0
    captured = capsys.readouterr()
    assert calls == ["not_1"]
    assert "1h 30m" in captured.out
    assert "Loading details for 1 meetings" in captured.err
