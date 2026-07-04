"""Unit tests for AgentEvent and AgentEventStream (T007).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from nate_ntm.runtime.events import (
    AgentEvent,
    AgentEventSource,
    AgentEventStream,
)


def test_agent_event_to_dict_round_trip_minimal_payload() -> None:
    ts = datetime(2026, 7, 3, 12, 34, 56)
    event = AgentEvent(
        event_id="e-1",
        timestamp=ts,
        agent_id="agent-1",
        source=AgentEventSource.ACP,
        type="TurnStarted",
    )

    data = event.to_dict()

    assert data["event_id"] == "e-1"
    assert data["agent_id"] == "agent-1"
    assert data["source"] == "ACP"
    assert data["type"] == "TurnStarted"
    assert data["payload"] == {}
    # ISO-8601 formatting for timestamp
    assert data["timestamp"].startswith("2026-07-03T12:34:56")


def test_agent_event_stream_appends_and_trims_to_max_events() -> None:
    stream = AgentEventStream(agent_id="agent-1", max_events=3)

    # Append 5 events; only the last 3 should be retained.
    for i in range(5):
        event = AgentEvent(
            event_id=f"e-{i}",
            timestamp=datetime(2026, 7, 3, 12, 0, i),
            agent_id="agent-1",
            source=AgentEventSource.RUNTIME,
            type="Test",
        )
        stream.append(event)

    events = stream.get_events()
    assert len(events) == 3
    assert [e.event_id for e in events] == ["e-2", "e-3", "e-4"]


def test_agent_event_stream_rejects_wrong_agent_id() -> None:
    stream = AgentEventStream(agent_id="agent-1", max_events=5)
    event = AgentEvent(
        event_id="e-1",
        timestamp=datetime(2026, 7, 3, 12, 0, 0),
        agent_id="other-agent",
        source=AgentEventSource.CLIENT,
        type="Test",
    )

    with pytest.raises(ValueError) as excinfo:
        stream.append(event)

    assert "agent-1" in str(excinfo.value)
    assert "other-agent" in str(excinfo.value)


def test_agent_event_stream_get_events_limit_behavior() -> None:
    stream = AgentEventStream(agent_id="agent-1", max_events=10)

    for i in range(5):
        event = AgentEvent(
            event_id=f"e-{i}",
            timestamp=datetime(2026, 7, 3, 12, 0, i),
            agent_id="agent-1",
            source=AgentEventSource.ACP,
            type="Test",
        )
        stream.append(event)

    # limit=None returns all
    assert [e.event_id for e in stream.get_events()] == [
        "e-0",
        "e-1",
        "e-2",
        "e-3",
        "e-4",
    ]

    # limit larger than size also returns all
    assert [e.event_id for e in stream.get_events(limit=10)] == [
        "e-0",
        "e-1",
        "e-2",
        "e-3",
        "e-4",
    ]

    # limit smaller than size returns only the most recent events
    assert [e.event_id for e in stream.get_events(limit=2)] == ["e-3", "e-4"]

    # non-positive limit yields empty list
    assert stream.get_events(limit=0) == []
    assert stream.get_events(limit=-1) == []


def test_agent_event_stream_rejects_non_positive_max_events() -> None:
    with pytest.raises(ValueError):
        AgentEventStream(agent_id="agent-1", max_events=0)

    with pytest.raises(ValueError):
        AgentEventStream(agent_id="agent-1", max_events=-5)
