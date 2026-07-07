from __future__ import annotations

"""Runtime-backed integration test for the overview → inspect → back flow.

This test drives the real Textual :class:`ConsoleApp` in headless mode using
:class:`textual.app.App.run_test` and a small fake :class:`RuntimeClient`.

The goal is to exercise the wiring between:

* ``ConsoleApp`` and its initial :class:`OverviewScreen`.
* The shared :class:`RuntimeSession` (including its background polling).
* The :class:`AgentTable` selection behavior.
* Navigation to :class:`AgentInspectScreen` and back to the overview.

The fake client avoids any real JSON-RPC or WebSocket traffic while still
behaving like a real runtime from the perspective of ``RuntimeSession`` and the
Textual UI.
"""

import asyncio
from typing import Any, AsyncIterator, Iterable, Mapping, Optional

import pytest

from nate_ntm.api.models import (
    AgentDetailEvent,
    AgentDetailResult,
    RuntimeStatusResult,
    SwarmOverviewResult,
)
from nate_ntm.api.runtime_client import EventsNotify
from nate_ntm.tui.app import ConsoleApp
from nate_ntm.tui.runtime_session import RuntimeSession
from nate_ntm.tui.screens import AgentInspectScreen, OverviewScreen
from nate_ntm.tui.screens.agent_inspect import AgentInspectView
from nate_ntm.tui.widgets import AgentTable


class _FakeRuntimeClient:
    """Test double for :class:`RuntimeClient` used by the integration flow.

    The fake implements just enough of the :class:`RuntimeClient` interface for
    :class:`RuntimeSession` and the Textual app to behave as if they were
    talking to a real runtime instance.
    """

    def __init__(
        self,
        *,
        status: RuntimeStatusResult,
        overview: SwarmOverviewResult,
        detail: AgentDetailResult,
    ) -> None:
        self._status = status
        self._overview = overview
        self._detail = detail
        self.calls: list[tuple[str, Mapping[str, Any] | None]] = []

    # Control-plane helpers -------------------------------------------------

    async def get_runtime_status(self) -> RuntimeStatusResult:
        self.calls.append(("runtime.get_status", None))
        return self._status

    async def get_swarm_overview(self) -> SwarmOverviewResult:
        self.calls.append(("swarm.get_overview", None))
        return self._overview

    async def get_agent_detail(
        self, agent_id: str, max_events: int = 100
    ) -> AgentDetailResult:
        self.calls.append(
            ("agent.get_detail", {"agent_id": agent_id, "max_events": max_events})
        )
        # For this thin slice we only support a single agent.
        if agent_id != self._detail.agent.agent_id:  # pragma: no cover - defensive
            raise RuntimeError(f"no detail configured for {agent_id!r}")
        return self._detail

    # Event-stream helper ---------------------------------------------------

    def iter_events(
        self,
        *,
        subscription_id: str | None = None,
        agent_ids: Iterable[str] | None = None,
        include_runtime: bool = True,
        reconnect: bool = True,
        reconnect_initial_backoff: float = 0.5,
        reconnect_max_backoff: float = 5.0,
    ) -> AsyncIterator[EventsNotify]:
        """Return an async iterator for runtime events.

        For this integration test we don't need live events, so the iterator
        simply terminates without yielding anything. It still behaves like a
        proper async generator so that :meth:`RuntimeSession.disconnect` can
        call ``aclose()`` on it.
        """

        self.calls.append(
            (
                "events.iter",
                {
                    "subscription_id": subscription_id,
                    "agent_ids": list(agent_ids) if agent_ids is not None else None,
                    "include_runtime": include_runtime,
                    "reconnect": reconnect,
                },
            )
        )

        async def _gen() -> AsyncIterator[EventsNotify]:
            # No events are produced in this test.
            if False:  # pragma: no cover
                yield None  # type: ignore[misc]

        return _gen()


def _make_sample_models() -> tuple[
    RuntimeStatusResult, SwarmOverviewResult, AgentDetailResult, AgentDetailEvent
]:
    """Construct minimal but valid model instances for the flow.

    The shapes mirror those used in ``tests/tui/unit/test_runtime_session.py`` so
    that we stay aligned with the public control API contract.
    """

    agent_counts_payload = {
        "total": 1,
        "starting": 0,
        "idle": 1,
        "running": 0,
        "waiting": 0,
        "failed": 0,
    }

    status = RuntimeStatusResult.model_validate(
        {
            "status": "running",
            "project_path": "/tmp/project",
            "swarm_id": "swarm-1",
            "agent_counts": agent_counts_payload,
        }
    )

    overview = SwarmOverviewResult.model_validate(
        {
            "swarm_id": "swarm-1",
            "project_path": "/tmp/project",
            "runtime_status": "running",
            "agent_counts": agent_counts_payload,
            "agents": [
                {
                    "agent_id": "agent-1",
                    "display_name": "Agent One",
                    "status": "idle",
                    "has_unread_mail": False,
                    "last_error": None,
                }
            ],
        }
    )

    detail = AgentDetailResult.model_validate(
        {
            "agent": {
                "agent_id": "agent-1",
                "display_name": "Agent One",
                "status": "idle",
                "agent_mail_identity": "agent-1@example.test",
                "conversation_id": "conv-1",
                "last_error": None,
            },
            "events": [
                {
                    "event_id": "evt-1",
                    "timestamp": "2026-07-07T12:00:00Z",
                    "agent_id": "agent-1",
                    "source": "runtime",
                    "type": "started",
                    "payload": {"info": "started"},
                }
            ],
        }
    )

    event = detail.events[0]

    return status, overview, detail, event


@pytest.mark.asyncio
async def test_overview_to_inspect_and_back_flow() -> None:
    """Drive overview → inspect → back using a fake runtime.

    The test runs ``ConsoleApp`` in headless mode and verifies that:

    * The overview screen populates from ``RuntimeSession``'s cached overview.
    * The agent table selection updates ``RuntimeSession.selected_agent_id``.
    * Pressing Enter triggers navigation to :class:`AgentInspectScreen`.
    * The inspection view renders detail loaded via ``RuntimeSession``.
    * Pressing ``b`` returns to the overview while preserving selection.
    """

    status, overview, detail, _event = _make_sample_models()

    fake_client = _FakeRuntimeClient(status=status, overview=overview, detail=detail)
    session = RuntimeSession(client=fake_client, poll_interval=0.01)

    app = ConsoleApp(session)

    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        # Connect the session once the Textual app is running. This starts the
        # background polling loop that populates the cached runtime status and
        # swarm overview used by the overview screen.
        await session.connect()

        # Allow background tasks and the UI to process the initial snapshots.
        await pilot.pause()

        # We should start on the overview screen.
        screen = pilot.app.screen
        assert isinstance(screen, OverviewScreen)

        # The agent table should render the single configured agent from the
        # fake overview snapshot.
        table = screen.query_one("#agent-table", AgentTable)
        table_text = table.render()
        assert "agent-1" in table_text
        assert "Agent One" in table_text

        # Drive selection via the shared RuntimeSession. This is equivalent to
        # having the operator move the selection within the agent table and keeps
        # the focus of this test on screen wiring rather than Textual's focus
        # semantics for individual widgets.
        session.select_agent("agent-1")
        await pilot.pause()

        assert session.selected_agent_id == "agent-1"

        # The rendered table should reflect the shared selection marker.
        table_text = table.render()
        assert "  > agent-1  Agent One" in table_text

        # Trigger the inspect action using the OverviewScreen binding (Enter).
        await pilot.press("enter")
        await pilot.pause()

        inspect_screen = pilot.app.screen
        assert isinstance(inspect_screen, AgentInspectScreen)

        # RuntimeSession.get_agent_detail() should have been used under the
        # hood to populate the detail cache via the fake client.
        assert any(name == "agent.get_detail" for name, _ in fake_client.calls)

        inspect_view = inspect_screen.query_one("#agent-inspect-view", AgentInspectView)
        detail_text = inspect_view.render()

        # The inspection view should render key fields from the detail payload.
        assert "Agent inspection:" in detail_text
        assert "id: agent-1" in detail_text
        assert "name: Agent One" in detail_text
        assert "status: idle" in detail_text
        assert "mail: agent-1@example.test" in detail_text
        assert "conversation: conv-1" in detail_text
        assert "Recent events" in detail_text

        # Navigate back to the overview using the AgentInspectScreen binding.
        await pilot.press("b")
        await pilot.pause()

        overview_again = pilot.app.screen
        assert isinstance(overview_again, OverviewScreen)

        # The RuntimeSession remains shared and continues to track the
        # currently selected agent.
        assert session.selected_agent_id == "agent-1"

        # Cleanly disconnect the session so that background tasks shut down.
        await session.disconnect()
