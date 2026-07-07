from __future__ import annotations

"""Integration test for the live event view embedded in the overview screen.

This test drives the real Textual :class:`ConsoleApp` in headless mode using
:class:`textual.app.App.run_test` and a small fake :class:`RuntimeClient`.

The goal is to exercise the wiring between:

* ``ConsoleApp`` and its initial :class:`OverviewScreen`.
* The shared :class:`RuntimeSession` event buffer.
* The :class:`EventView` widget used in the overview layout.

Rather than talking to a real runtime, the test feeds a finite sequence of
``EventsNotify`` instances through :class:`RuntimeSession` and verifies that
``EventView`` renders the expected entries.
"""

import asyncio
from typing import Any, AsyncIterator, Iterable, Mapping, Optional

import pytest

from nate_ntm.api.models import AgentDetailEvent, AgentDetailResult, RuntimeStatusResult, SwarmOverviewResult
from nate_ntm.api.runtime_client import EventsNotify
from nate_ntm.tui.app import ConsoleApp
from nate_ntm.tui.runtime_session import RuntimeSession
from nate_ntm.tui.screens import OverviewScreen
from nate_ntm.tui.widgets import EventView


class _FakeRuntimeClient:
    """Test double for :class:`RuntimeClient` used in the event flow test.

    The fake implements just enough of the :class:`RuntimeClient` interface for
    :class:`RuntimeSession` and the Textual app to believe they are connected to
    a real runtime instance.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, Any] | None]] = []
        self._status: Optional[RuntimeStatusResult] = None
        self._overview: Optional[SwarmOverviewResult] = None
        self._events: Iterable[EventsNotify] | None = None

    async def get_runtime_status(self) -> RuntimeStatusResult:
        if self._status is None:  # pragma: no cover - defensive
            raise RuntimeError("status not configured on fake client")
        self.calls.append(("runtime.get_status", None))
        return self._status

    async def get_swarm_overview(self) -> SwarmOverviewResult:
        if self._overview is None:  # pragma: no cover - defensive
            raise RuntimeError("overview not configured on fake client")
        self.calls.append(("swarm.get_overview", None))
        return self._overview

    async def get_agent_detail(self, agent_id: str, max_events: int = 100) -> AgentDetailResult:
        raise RuntimeError("get_agent_detail should not be called in event view test")

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

        For this integration test we use a finite list of preconfigured
        :class:`EventsNotify` instances to drive the event buffer.
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
            if self._events is None:
                return
            for item in self._events:
                await asyncio.sleep(0)
                yield item

        return _gen()


def _make_sample_models_with_events() -> tuple[RuntimeStatusResult, SwarmOverviewResult, list[EventsNotify]]:
    """Construct runtime status, overview, and a small sequence of events."""

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

    events: list[EventsNotify] = []
    for idx in range(1, 4):
        evt = AgentDetailEvent.model_validate(
            {
                "event_id": f"evt-{idx}",
                "timestamp": f"2026-07-07T12:00:0{idx}Z",
                "agent_id": "agent-1",
                "source": "runtime",
                "type": "tick",
                "payload": {"seq": idx},
            }
        )
        events.append(EventsNotify(subscription_id="sub-1", event=evt))

    return status, overview, events


@pytest.mark.asyncio
async def test_event_view_renders_events_from_session_buffer() -> None:
    """Drive events through RuntimeSession and into the overview EventView.

    The test runs ``ConsoleApp`` in headless mode with a fake runtime client.
    After connecting the shared :class:`RuntimeSession`, it verifies that the
    overview's :class:`EventView` widget renders the events buffered by the
    session.
    """

    status, overview, events = _make_sample_models_with_events()

    fake_client = _FakeRuntimeClient()
    fake_client._status = status
    fake_client._overview = overview
    fake_client._events = events

    session = RuntimeSession(client=fake_client, poll_interval=0.01, event_buffer_size=10)

    app = ConsoleApp(session)

    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        await session.connect()

        # Allow background tasks (polling and event consumption) and the UI to
        # process the initial snapshots and event notifications.
        await pilot.pause()
        await pilot.pause()

        screen = pilot.app.screen
        assert isinstance(screen, OverviewScreen)

        event_view = screen.query_one("#event-view", EventView)
        text = event_view.render()

        # Header and basic structure from EventView.
        assert "Events (most recent last):" in text

        # All three configured events should be present with their timestamps and
        # core identifying fields.
        assert "2026-07-07T12:00:01Z" in text
        assert "2026-07-07T12:00:02Z" in text
        assert "2026-07-07T12:00:03Z" in text
        assert "agent=agent-1" in text
        assert "type=tick" in text
        assert "source=runtime" in text

        await session.disconnect()
