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
