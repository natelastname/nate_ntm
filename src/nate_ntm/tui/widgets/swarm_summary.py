from __future__ import annotations

"""Runtime and swarm summary widget.

This widget renders a compact textual summary of the connected runtime and
swarm, driven entirely by cached state on a
:class:`~nate_ntm.tui.runtime_session.RuntimeSession` instance.
"""

from typing import Any

from textual.widgets import Static

from nate_ntm.tui.runtime_session import RuntimeSession


class SwarmSummary(Static):
    """Display a high-level summary of runtime and swarm health.

    The widget reads cached state from the provided :class:`RuntimeSession` on
    each render. It does not perform any network or transport operations.
    """

    def __init__(self, session: RuntimeSession, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._session = session

    @property
    def session(self) -> RuntimeSession:
        """Return the associated :class:`RuntimeSession`.

        Exposed primarily for tests and for higher-level screens that may want
        to introspect the session associated with this widget.
        """

        return self._session

    def render(self) -> str:  # pragma: no cover - exercised through Textual rendering
        session = self._session
        status = session.get_cached_runtime_status()
        overview = session.get_cached_swarm_overview()

        lines: list[str] = []

        # Connection state is derived purely from the RuntimeSession so that
        # operators can distinguish between a disconnected console and a
        # connected-but-degraded runtime.
        if session.is_connected:
            lines.append("Connection: connected")
        else:
            lines.append("Connection: disconnected")

        if status is None or overview is None:
            lines.append("Runtime: connecting…")
        else:
            lines.append(f"Runtime status: {status.status}")
            lines.append(f"Project: {status.project_path or '-'}")
            lines.append(f"Swarm: {status.swarm_id or '-'}")

            counts = overview.agent_counts
            lines.append(
                "Agents: "
                f"total={counts.total} "
                f"starting={counts.starting} "
                f"idle={counts.idle} "
                f"running={counts.running} "
                f"waiting={counts.waiting} "
                f"failed={counts.failed}"
            )

        # Surface degraded state flags inline so operators can see when the
        # control plane or event stream is unhealthy.
        if session.control_degraded:
            lines.append(f"[control degraded: {session.control_error or 'unknown error'}]")
        if session.events_degraded:
            lines.append(f"[events degraded: {session.events_error or 'unknown error'}]")

        return "\n".join(lines)
