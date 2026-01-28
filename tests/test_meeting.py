"""Tests for Meeting class."""

import datetime
import pytest
from granola_mcp.core.meeting import Meeting


class TestMeetingStartTime:
    """Tests for Meeting.start_time property."""

    def test_start_time_from_google_calendar_event(self):
        """Meeting should parse start_time from google_calendar_event.start.dateTime."""
        meeting_data = {
            "id": "test-123",
            "title": "Test Meeting",
            "google_calendar_event": {
                "start": {
                    "dateTime": "2026-01-28T12:00:00-05:00",
                    "timeZone": "America/New_York"
                }
            }
        }

        meeting = Meeting(meeting_data)

        assert meeting.start_time is not None
        assert meeting.start_time.year == 2026
        assert meeting.start_time.month == 1
        assert meeting.start_time.day == 28

    def test_start_time_from_direct_start_field(self):
        """Meeting should parse start_time from direct start.dateTime field."""
        meeting_data = {
            "id": "test-456",
            "title": "Test Meeting 2",
            "start": {
                "dateTime": "2026-01-15T10:00:00-05:00"
            }
        }

        meeting = Meeting(meeting_data)

        assert meeting.start_time is not None
        assert meeting.start_time.day == 15

    def test_start_time_from_created_at(self):
        """Meeting should fall back to created_at if no calendar event."""
        meeting_data = {
            "id": "test-789",
            "title": "Test Meeting 3",
            "created_at": "2026-01-20T14:30:00Z"
        }

        meeting = Meeting(meeting_data)

        assert meeting.start_time is not None
        assert meeting.start_time.day == 20

    def test_start_time_none_when_no_time_fields(self):
        """Meeting should return None if no time fields available."""
        meeting_data = {
            "id": "test-000",
            "title": "No Time Meeting"
        }

        meeting = Meeting(meeting_data)

        assert meeting.start_time is None


class TestMeetingEndTime:
    """Tests for Meeting.end_time property."""

    def test_end_time_from_google_calendar_event(self):
        """Meeting should parse end_time from google_calendar_event.end.dateTime."""
        meeting_data = {
            "id": "test-123",
            "title": "Test Meeting",
            "google_calendar_event": {
                "start": {
                    "dateTime": "2026-01-28T12:00:00-05:00"
                },
                "end": {
                    "dateTime": "2026-01-28T13:00:00-05:00"
                }
            }
        }

        meeting = Meeting(meeting_data)

        assert meeting.end_time is not None
        assert meeting.end_time.hour == 12  # 13:00 EST = 12:00 CST

    def test_end_time_from_direct_end_field(self):
        """Meeting should parse end_time from direct end.dateTime field."""
        meeting_data = {
            "id": "test-456",
            "title": "Test Meeting 2",
            "end": {
                "dateTime": "2026-01-15T11:00:00-05:00"
            }
        }

        meeting = Meeting(meeting_data)

        assert meeting.end_time is not None

    def test_end_time_none_when_no_time_fields(self):
        """Meeting should return None if no end time fields available."""
        meeting_data = {
            "id": "test-000",
            "title": "No Time Meeting"
        }

        meeting = Meeting(meeting_data)

        assert meeting.end_time is None


class TestMeetingDuration:
    """Tests for Meeting.duration property."""

    def test_duration_from_calendar_times(self):
        """Meeting should calculate duration from calendar start/end times."""
        meeting_data = {
            "id": "test-123",
            "title": "Test Meeting",
            "google_calendar_event": {
                "start": {
                    "dateTime": "2026-01-28T12:00:00-05:00"
                },
                "end": {
                    "dateTime": "2026-01-28T13:00:00-05:00"
                }
            }
        }

        meeting = Meeting(meeting_data)

        # Duration calculation uses _calculate_duration_from_calendar
        # which should find the google_calendar_event times
        assert meeting.duration is not None
        assert meeting.duration.total_seconds() == 3600  # 1 hour


class TestMeetingBasics:
    """Tests for basic Meeting properties."""

    def test_id(self):
        """Meeting should return correct ID."""
        meeting = Meeting({"id": "abc-123", "title": "Test"})
        assert meeting.id == "abc-123"

    def test_title(self):
        """Meeting should return correct title."""
        meeting = Meeting({"id": "abc", "title": "My Meeting"})
        assert meeting.title == "My Meeting"

    def test_participants_from_list(self):
        """Meeting should parse participants from list."""
        meeting = Meeting({
            "id": "abc",
            "participants": ["alice@example.com", "bob@example.com"]
        })
        assert meeting.participants == ["alice@example.com", "bob@example.com"]

    def test_participants_from_attendees_dict(self):
        """Meeting should parse participants from attendees with email field."""
        meeting = Meeting({
            "id": "abc",
            "attendees": [
                {"email": "alice@example.com"},
                {"email": "bob@example.com"}
            ]
        })
        assert "alice@example.com" in meeting.participants
        assert "bob@example.com" in meeting.participants

    def test_has_transcript_false_when_no_transcript(self):
        """Meeting should report no transcript when none exists."""
        meeting = Meeting({"id": "abc", "title": "Test"})
        assert meeting.has_transcript() is False
