"""Agent event and in-memory event stream abstractions.

This module defines small, in-memory representations of agent events and
per-agent event streams. They are designed to:

* Match the shapes used in ``contracts/runtime-api.md`` (see the
  ``AgentEvent`` type and event streaming methods).
* Be easy to serialize to JSON for the runtime control API.
* Remain independent of specific transport or storage concerns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional


class AgentEventSource(str, Enum):
    """Origin of an :class:`AgentEvent`.

    Values are chosen to match the JSON contract in
    ``contracts/runtime-api.md``.
    """

    ACP = "ACP"
    AGENT_MAIL = "AgentMail"
    RUNTIME = "Runtime"
    CLIENT = "Client"


@dataclass(slots=True)
class AgentEvent:
    """A single event in an agent's event stream.

    This structure intentionally mirrors the ``AgentEvent`` type in the
    runtime API contract, with ``timestamp`` represented as a
    :class:`~datetime.datetime` instance in Python.
    """

    event_id: str
    """Unique identifier for the event within the stream."""

    timestamp: datetime
    """Time at which the event occurred or was observed (assumed UTC)."""

    agent_id: str
    """Agent associated with the event."""

    source: AgentEventSource
    """Origin of the event (ACP, AgentMail, Runtime, or Client)."""

    type: str
    """Event type string (e.g. ``"TurnStarted"``, ``"TurnCompleted"``)."""

    payload: Mapping[str, Any] = field(default_factory=dict)
    """Event-specific payload with summarized details."""

    def to_dict(self) -> Dict[str, Any]:
        """Render the event as a JSON-serializable mapping.

        The ``timestamp`` is formatted as an ISO-8601 string to match the
        runtime API contract. Callers are free to enrich the resulting
        dictionary with additional fields if needed.
        """

        # ``datetime.isoformat`` is sufficient here; callers can normalize
        # to UTC or add ``Z`` suffix if desired.
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp.isoformat(),
            "agent_id": self.agent_id,
            "source": self.source.value,
            "type": self.type,
            "payload": dict(self.payload),
        }


_DEFAULT_MAX_EVENTS = 200


@dataclass(slots=True)
class AgentEventStream:
    """Bounded, in-memory event stream for a single agent.

    Events are stored in arrival order. When ``max_events`` is reached,
    the oldest events are dropped as new ones are appended. This stream
    is *not* durable and is safe to discard between runtime restarts.
    """

    agent_id: str
    """Identifier of the agent this stream belongs to."""

    max_events: int = _DEFAULT_MAX_EVENTS
    """Maximum number of events retained in memory (must be > 0)."""

    _events: List[AgentEvent] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_events <= 0:
            raise ValueError("max_events must be positive")

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self._events)

    def __iter__(self) -> Iterable[AgentEvent]:  # pragma: no cover - trivial
        return iter(self._events)

    def append(self, event: AgentEvent) -> None:
        """Append an event, enforcing the stream's agent and size bound.

        Events for other agents are rejected to catch wiring mistakes
        early.
        """

        if event.agent_id != self.agent_id:
            raise ValueError(
                f"AgentEventStream for {self.agent_id!r} cannot accept event "
                f"for agent_id {event.agent_id!r}"
            )

        self._events.append(event)
        # Trim oldest events if we exceeded the limit.
        overflow = len(self._events) - self.max_events
        if overflow > 0:
            del self._events[0:overflow]

    def get_events(self, limit: Optional[int] = None) -> List[AgentEvent]:
        """Return the most recent events, up to ``limit``.

        If ``limit`` is ``None`` or larger than the current size, all
        events are returned. A non-positive ``limit`` yields an empty
        list.
        """

        if limit is None or limit >= len(self._events):
            return list(self._events)
        if limit <= 0:
            return []
        return self._events[-limit:]
