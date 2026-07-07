from __future__ import annotations

"""Simple agent table widget.

The widget renders a textual list of agents using cached swarm overview data
from :class:`~nate_ntm.tui.runtime_session.RuntimeSession` and supports a
lightweight notion of selection driven by the shared session.
"""

from typing import Any, Optional

from textual.binding import Binding
from textual.widgets import Static

from nate_ntm.tui.runtime_session import RuntimeSession


class AgentTable(Static):
    """Render a simple table of agents based on cached overview data.

    The table supports a lightweight notion of *selection* driven by the
    associated :class:`RuntimeSession`. The selected agent identifier is stored
    on the session (via :meth:`RuntimeSession.select_agent`) so that other
    screens and widgets can observe or react to the current selection.
    """

    BINDINGS = [
        Binding("up", "cursor_up", "Select previous agent", show=False),
        Binding("down", "cursor_down", "Select next agent", show=False),
    ]

    def __init__(self, session: RuntimeSession, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._session = session

    @property
    def session(self) -> RuntimeSession:
        """Return the associated :class:`RuntimeSession`."""

        return self._session

    @property
    def selected_agent_id(self) -> Optional[str]:
        """Return the currently selected agent id (if any).

        This simply forwards to :attr:`RuntimeSession.selected_agent_id` so
        tests and higher-level components can inspect the shared selection
        state.
        """

        return self._session.selected_agent_id

    def _move_selection(self, delta: int) -> None:
        """Move the current selection by ``delta`` rows, if possible.

        When no agent is selected yet, moving in a positive direction selects
        the first agent; moving in a negative direction selects the last
        agent.
        """

        overview = self._session.get_cached_swarm_overview()
        if overview is None or not overview.agents:
            return

        agents = list(overview.agents)
        current_id = self._session.selected_agent_id

        current_index: Optional[int] = None
        if current_id is not None:
            for idx, agent in enumerate(agents):
                if agent.agent_id == current_id:
                    current_index = idx
                    break

        if current_index is None:
            # No selection yet; choose an endpoint based on navigation
            # direction.
            new_index = 0 if delta >= 0 else len(agents) - 1
        else:
            new_index = max(0, min(len(agents) - 1, current_index + delta))

        new_id = agents[new_index].agent_id
        self._session.select_agent(new_id)
        # Request a re-render so that selection markers are updated.
        self.refresh()

    def action_cursor_up(self) -> None:
        """Select the previous agent in the list (if any)."""

        self._move_selection(-1)

    def action_cursor_down(self) -> None:
        """Select the next agent in the list (if any)."""

        self._move_selection(1)

    def render(self) -> str:  # pragma: no cover - exercised through Textual rendering
        overview = self._session.get_cached_swarm_overview()

        if overview is None:
            return "Agents: (overview not yet available)"

        if not overview.agents:
            return "Agents: (none)"

        lines: list[str] = ["Agents:"]
        selected_id = self._session.selected_agent_id

        for agent in overview.agents:
            # Agent overview entries include id, display_name, and status.
            display_name = agent.display_name or agent.agent_id
            marker = ">" if agent.agent_id == selected_id else "-"
            lines.append(
                f"  {marker} {agent.agent_id}  {display_name}  status={agent.status}"
            )

        return "\n".join(lines)
