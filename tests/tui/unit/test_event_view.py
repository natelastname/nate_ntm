from __future__ import annotations

"""Unit tests for the live event view widget.

These tests exercise :class:`EventView`'s rendering in isolation from Textual's
runtime. The widget is intentionally thin and reads exclusively from a
`RuntimeSession`-like object; the tests rely on a small stub that provides the
subset of attributes and methods used by `EventView`.
"""

from typing import Any, List

from nate_ntm.api.models import AgentDetailEvent
from nate_ntm.tui.widgets import EventView


class _StubSession:
    """Minimal stub that mimics the `RuntimeSession` interface used by EventView."""

    def __init__(self) -> None:
        self._events: List[AgentDetailEvent] = []
        self.events_degraded: bool = False
        self.events_error: str | None = None
        self.control_degraded: bool = False
        self.control_error: str | None = None

    def get_recent_events(self, limit: int | None = None) -> list[AgentDetailEvent]:
        if limit is None or limit >= len(self._events):
            return list(self._events)
        return list(self._events[-limit:])


def _make_event(event_id: str, agent_id: str, type_: str, source: str, timestamp: str) -> AgentDetailEvent:
    return AgentDetailEvent.model_validate(
        {
            "event_id": event_id,
            "timestamp": timestamp,
            "agent_id": agent_id,
            "source": source,
            "type": type_,
            "payload": {"info": f"{type_} event"},
        }
    )


def test_event_view_empty_renders_placeholder() -> None:
    """When there are no events, EventView should show a clear placeholder."""

    session = _StubSession()
    view = EventView(session)

    text = view.render()

    assert "Events (most recent last):" in text
    assert "(none yet)" in text


def test_event_view_renders_event_fields() -> None:
    """EventView should render timestamp, agent_id, type, and source."""

    session = _StubSession()
    session._events.append(
        _make_event(
            event_id="evt-1",
            agent_id="agent-1",
            type_="started",
            source="runtime",
            timestamp="2026-07-07T12:00:00Z",
        )
    )

    view = EventView(session, limit=10)

    text = view.render()

    assert "Events (most recent last):" in text
    assert "2026-07-07T12:00:00Z" in text
    assert "agent=agent-1" in text
    assert "type=started" in text
    assert "source=runtime" in text


def test_event_view_includes_degraded_flags_and_errors() -> None:
    """EventView should surface degradation flags and error messages."""

    session = _StubSession()
    session.events_degraded = True
    session.events_error = "event-stream failure"
    session.control_degraded = True
    session.control_error = "control failure"

    view = EventView(session)
    text = view.render()

    # Header should reflect both ordering and degraded state.
    assert "Events (most recent last; events degraded; control degraded):" in text
    # Body should include specific error messages.
    assert "! events: event-stream failure" in text
    assert "! control: control failure" in text
