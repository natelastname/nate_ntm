from __future__ import annotations

"""Live event view widget.

This widget renders a simple, scroll-free view of the most recent events from
a :class:`~nate_ntm.tui.runtime_session.RuntimeSession` instance.

For this initial slice the widget simply lists recent events in reverse
chronological order (newest last in the buffer) with minimal formatting.
"""

from typing import Any

from textual.widgets import Static

from nate_ntm.tui.runtime_session import RuntimeSession


class EventView(Static):
    """Render a small, bounded list of recent runtime/agent events.

    The widget reads exclusively from a shared :class:`RuntimeSession` instance
    and does not perform any protocol or transport work directly. It presents a
    small window of the most recent events together with basic degradation
    indicators derived from :class:`RuntimeSession`'s flags.
    """

    def __init__(self, session: RuntimeSession, limit: int = 50, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._session = session
        self._limit = int(limit)

    @property
    def session(self) -> RuntimeSession:
        """Return the associated :class:`RuntimeSession`."""

        return self._session

    def render(self) -> str:
        """Render a textual view of recent events.

        The output includes a header that reflects both the ordering
        ("most recent last") and any degraded state indicated by the
        session's control and event-stream flags.
        """

        session = self._session
        events = session.get_recent_events(limit=self._limit)

        # Build a header that always documents the ordering and optionally
        # appends degradation information.
        status_fragments: list[str] = ["most recent last"]
        if session.events_degraded:
            status_fragments.append("events degraded")
        if session.control_degraded:
            status_fragments.append("control degraded")

        header = f"Events ({'; '.join(status_fragments)}):"
        lines: list[str] = [header]

        # Surface any available degradation messages so operators can see
        # that the event view may be incomplete.
        if session.events_degraded and session.events_error:
            lines.append(f"  ! events: {session.events_error}")
        if session.control_degraded and session.control_error:
            lines.append(f"  ! control: {session.control_error}")

        if not events:
            lines.append("  (none yet)")
            return "\n".join(lines)

        for event in events:
            # AgentDetailEvent exposes timestamp, agent_id, source, and type.
            timestamp = getattr(event, "timestamp", None)
            timestamp_str = (
                timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp or "?")
            )
            agent_id = getattr(event, "agent_id", "-")
            source = getattr(event, "source", "-")
            event_type = getattr(event, "type", "-")
            lines.append(
                f"  - {timestamp_str}  agent={agent_id}  type={event_type}  source={source}"
            )

        return "\n".join(lines)
