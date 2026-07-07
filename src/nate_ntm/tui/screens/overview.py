from __future__ import annotations

"""Default overview screen for the Textual runtime console.

The overview screen presents a compact, at-a-glance view of runtime and swarm
health backed entirely by cached state from a shared
:class:`~nate_ntm.tui.runtime_session.RuntimeSession` instance.

For this initial implementation the screen remains intentionally simple:

* A runtime/swarm summary at the top.
* A basic agent table in the middle.
* A placeholder selected-agent detail area (derived from cached overview).
* A live event list at the bottom.

The screen does **not** perform any direct runtime control calls; it observes
state changes solely via :class:`RuntimeSession` and its
:meth:`RuntimeSession.wait_for_update` notification mechanism.
"""

import asyncio
import contextlib
from typing import Any, Optional

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

from nate_ntm.tui.runtime_session import RuntimeSession
from nate_ntm.tui.widgets import AgentTable, EventView, SwarmSummary
from nate_ntm.tui.screens.agent_inspect import AgentInspectScreen


class AgentDetailPanel(Static):
    """Very simple selected-agent detail area.

    For this first slice we derive details from the cached swarm overview and
    always show the **first** agent (when present). A more interactive,
    selectable detail view will be added in the dedicated agent-inspection
    feature work.
    """

    def __init__(self, session: RuntimeSession, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._session = session

    @property
    def session(self) -> RuntimeSession:
        return self._session

    def render(self) -> str:  # pragma: no cover - exercised through Textual rendering
        overview = self._session.get_cached_swarm_overview()

        if overview is None or not overview.agents:
            return "Selected agent:\n  (none)"

        agent = overview.agents[0]

        lines = [
            "Selected agent:",
            f"  id: {agent.agent_id}",
            f"  name: {agent.display_name or '-'}",
            f"  status: {agent.status}",
        ]

        # The richer agent detail payload (including recent events and other
        # metadata) will be surfaced via :meth:`RuntimeSession.get_agent_detail`
        # in later user stories.

        return "\n".join(lines)


class OverviewScreen(Screen):
    """Default overview screen for the runtime console."""

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("enter", "inspect_agent", "Inspect selected agent"),
    ]

    def __init__(self, session: RuntimeSession, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._session = session
        self._update_task: Optional[asyncio.Task[None]] = None

    @property
    def session(self) -> RuntimeSession:
        return self._session

    def compose(self) -> ComposeResult:  # pragma: no cover - UI composition
        yield Header(show_clock=True)
        yield Vertical(
            SwarmSummary(self._session, id="swarm-summary"),
            Horizontal(
                AgentTable(self._session, id="agent-table"),
                AgentDetailPanel(self._session, id="agent-detail"),
                id="agent-row",
            ),
            EventView(self._session, id="event-view"),
            Footer(),
            id="overview-layout",
        )

    async def on_mount(self) -> None:  # pragma: no cover - Textual runtime hook
        """Start a background task that waits for session updates.

        The task blocks on :meth:`RuntimeSession.wait_for_update` and refreshes
        the screen whenever new state is available, avoiding manual polling in
        the UI layer.
        """

        self._update_task = asyncio.create_task(self._watch_session_updates())

    async def on_unmount(self) -> None:  # pragma: no cover - Textual runtime hook
        if self._update_task is not None:
            self._update_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._update_task
            self._update_task = None

    async def _watch_session_updates(self) -> None:
        last_seen: Optional[int] = None
        while True:
            try:
                last_seen = await self._session.wait_for_update(last_seen=last_seen)
            except asyncio.CancelledError:
                break
            except Exception:
                # For this thin slice we treat unexpected errors as terminal for
                # the watcher but keep the screen alive; more detailed logging
                # or UI surfacing can be added later.
                break

            # Trigger a re-render so dependent widgets can pick up the new
            # cached state from the session.
            self.refresh()

    def action_inspect_agent(self) -> None:
        """Open the agent inspection screen for the currently selected agent.

        The currently selected agent identifier is obtained from the shared
        :class:`RuntimeSession` via :attr:`RuntimeSession.selected_agent_id`.
        If no agent is selected, the action is a no-op.
        """

        agent_id = self._session.selected_agent_id
        if not agent_id:
            return

        self.app.push_screen(AgentInspectScreen(self._session))

    def action_quit(self) -> None:
        """Quit the application."""

        self.app.exit()
