"""Bounded detail hydration for CLI bulk commands."""

import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import List

from ...core.meeting import Meeting
from ...core.parser import GranolaParser
from ..formatters.colors import print_info


_MIN_REQUEST_INTERVAL_SECONDS = 0.21
_MAX_WORKERS = 4


class _RequestStartLimiter:
    """Space possible request starts slightly below five per second."""

    def __init__(self, interval: float = _MIN_REQUEST_INTERVAL_SECONDS):
        self.interval = interval
        self._lock = threading.Lock()
        self._next_start = 0.0

    def wait(self) -> None:
        """Wait until the next external request may start."""
        with self._lock:
            now = time.monotonic()
            delay = self._next_start - now
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self._next_start = max(now, self._next_start) + self.interval


def disclose_source_cap(parser: GranolaParser, meeting_count: int) -> None:
    """Warn when a remote backend result may equal its configured list cap."""
    if getattr(parser, "mode", None) not in ("api", "public"):
        return
    try:
        cap = int(os.environ.get("GRANOLA_MAX_DOCUMENTS", "500"))
    except ValueError:
        return
    reported_truncation = bool(
        getattr(parser, "last_public_list_truncated", False)
    )
    if reported_truncation or (cap > 0 and meeting_count >= cap):
        print_info(
            f"Loaded {meeting_count} meetings, matching the configured "
            f"GRANOLA_MAX_DOCUMENTS cap ({cap}); older meetings may be omitted.",
            file=sys.stderr,
        )


def hydrate_meetings(
    parser: GranolaParser,
    meetings: List[Meeting],
    *,
    include_summary: bool = False,
) -> List[Meeting]:
    """Load remote meeting detail with bounded concurrency and paced starts.

    The public backend has a one-request detail endpoint, so it is used directly
    and failures are surfaced. The internal API retains its existing lazy
    transcript/summary providers; their possible request starts are paced too.
    Plaintext meetings need no hydration.
    """
    mode = getattr(parser, "mode", None)
    if mode not in ("api", "public") or not meetings:
        return meetings

    print_info(
        f"Loading details for {len(meetings)} meetings "
        "(bounded to fewer than 5 request starts/second)...",
        file=sys.stderr,
    )
    limiter = _RequestStartLimiter()

    def hydrate(meeting: Meeting) -> Meeting:
        if mode == "public":
            if not meeting.id:
                raise RuntimeError("Cannot load public note detail without an ID")
            limiter.wait()
            detail = parser.get_meeting_by_id(meeting.id)
            if detail is None:
                raise RuntimeError(
                    f"Public note detail is unavailable for {meeting.id}"
                )
            return Meeting(detail)

        # The internal backend's lazy providers preserve its established token
        # and refresh behavior. Pace each call that could start an HTTP request.
        limiter.wait()
        meeting.transcript
        if include_summary:
            limiter.wait()
            meeting.summary
        return meeting

    # The frozen internal-API token provider can rotate a WorkOS refresh token
    # and is intentionally not a concurrent primitive. Keep that backend
    # serial so overlapping expired-token reads cannot reuse one rotation
    # token. The public client owns thread-safe key and pacing locks.
    if mode == "api":
        return [hydrate(meeting) for meeting in meetings]

    workers = min(_MAX_WORKERS, len(meetings))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(hydrate, meetings))
