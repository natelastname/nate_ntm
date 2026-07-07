from __future__ import annotations

"""Agent inspection screen for the Textual runtime console.

This module provides a minimal :class:`AgentInspectScreen` implementation for
User Story 2 (agent inspection) together with a small detail-view widget.

The design intentionally keeps things simple for this slice:

* The currently selected agent is determined solely by
  :attr:`nate_ntm.tui.runtime_session.RuntimeSession.selected_agent_id`.
* Detailed information is fetched via :meth:`RuntimeSession.get_agent_detail`
  once when the screen is mounted, which populates the session's cache.
* Rendering reads only from cached session state via
  :meth:`RuntimeSession.get_cached_agent_detail` and does not perform any
  JSON-RPC or transport work directly.
* Navigation back to the overview screen is a single keypress away so that the
  operator can quickly return to the broader swarm context.

This is deliberately not a full future ACP/log/mail view; it is just enough
structure to support basic agent inspection while keeping the overview as the
primary context.
"""

import asyncio
import contextlib
from typing import Any, Optional

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

from nate_ntm.tui.runtime_session import RuntimeSession


class AgentInspectView(Static):
    """Render a simple detail view for the currently selected agent.

    The widget consults :attr:`RuntimeSession.selected_agent_id` to determine
    which agent to display and uses :meth:`RuntimeSession.get_cached_agent_detail`
    to obtain the latest-known detail, falling back to a lightweight message
    when no selection is present or detail has not yet been loaded.
    """

    def __init__(self, session: RuntimeSession, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._session = session

    @property
    def session(self) -> RuntimeSession:
        """Return the associated :class:`RuntimeSession`."""

        return self._session

    def render(self) -> str:  # pragma: no cover - exercised through Textual rendering
        session = self._session
        agent_id = session.selected_agent_id

        if agent_id is None:
            return "Agent inspection:\n  (no agent selected)"

        detail = session.get_cached_agent_detail(agent_id)
        if detail is None:
            return f"Agent inspection:\n  id: {agent_id}\n  (detail not yet loaded)"

        agent = detail.agent

        lines = [
            "Agent inspection:",
            f"  id: {agent.agent_id}",
            f"  name: {agent.display_name or '-'}",
            f"  status: {agent.status}",
            f"  mail: {agent.agent_mail_identity or '-'}",
            f"  conversation: {agent.conversation_id or '-'}",
        ]

        if agent.last_error:
            lines.append(f"  last_error: {agent.last_error}")

        # Show a very small window of recent events when available. The
        # :class:`AgentDetailResult` model already contains a bounded list of
        # events from the runtime; we simply display the most recent few.
        if detail.events:
            lines.append("")
            lines.append("  Recent events (most recent last):")
            # Display up to the last 5 events for brevity.
            for event in detail.events[-5:]:
                lines.append(
                    f"    - {event.timestamp}  type={event.type}  source={event.source}"
                )

        return "\n".join(lines)


class AgentInspectScreen(Screen):
    """Focused agent inspection screen.

    The screen observes a shared :class:`RuntimeSession` instance and uses the
    session's ``selected_agent_id`` as the source of truth for which agent is
    being inspected. Detailed information is retrieved via
    :meth:`RuntimeSession.get_agent_detail` when the screen is mounted, and all
    rendering is done from cached session state.
    """

    BINDINGS = [
        ("b", "back", "Back to overview"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, session: RuntimeSession, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._session = session
        self._load_task: Optional[asyncio.Task[None]] = None

    @property
    def session(self) -> RuntimeSession:
        """Return the associated :class:`RuntimeSession`."""

        return self._session

    def compose(self) -> ComposeResult:  # pragma: no cover - UI composition
        yield Header(show_clock=True)
        yield AgentInspectView(self._session, id="agent-inspect-view")
        yield Footer()

    async def on_mount(self) -> None:  # pragma: no cover - Textual runtime hook
        """Start a background task to load agent detail into the session cache.

        The task calls :meth:`RuntimeSession.get_agent_detail` for the currently
        selected agent (if any). Once the detail has been fetched and cached, the
        detail view is refreshed so that it can render the richer payload.
        """

        self._load_task = asyncio.create_task(self._load_initial_detail())

    async def on_unmount(self) -> None:  # pragma: no cover - Textual runtime hook
        if self._load_task is not None:
            self._load_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._load_task
            self._load_task = None

    async def _load_initial_detail(self) -> None:
        """Fetch detail for the currently selected agent, if any.

        This helper is separated out so that it can be exercised directly in
        unit tests without spinning up a full Textual application.
        """

        agent_id = self._session.selected_agent_id
        if agent_id is None:
            return

        try:
            await self._session.get_agent_detail(agent_id)
        except Exception:
            # The session records control-plane degradation; the detail view can
            # surface that state if needed. For this thin slice we keep error
            # handling minimal and rely on the last-known cached state.
            return

        # Ask the detail view to re-render now that additional information is
        # available in the session cache.
        view = self.query_one("#agent-inspect-view", AgentInspectView)
        view.refresh()

    def action_back(self) -> None:
        """Return to the previous screen (typically the overview)."""

        self.app.pop_screen()

    def action_quit(self) -> None:
        """Quit the application entirely."""

        self.app.exit()
