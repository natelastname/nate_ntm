from __future__ import annotations

"""Integration test for the runtime shutdown flow from the overview screen.

This test exercises FR-009's requirement that operators can request a
*graceful* runtime shutdown from within the Textual console with an explicit
confirmation step.

Rather than talking to a real runtime, the test uses a small fake
:class:`RuntimeClient` together with ``ConsoleApp.run_test`` in headless mode.
"""

import asyncio
from typing import Any, AsyncIterator, Iterable, Mapping, Optional

import pytest

from nate_ntm.api.models import RuntimeStatusResult, SwarmOverviewResult
from nate_ntm.tui.app import ConsoleApp
from nate_ntm.tui.runtime_session import RuntimeSession
from nate_ntm.tui.screens import OverviewScreen
from nate_ntm.tui.screens.overview import RuntimeShutdownConfirmScreen
from textual.widgets import Static


class _FakeRuntimeClient:
    """Test double for :class:`RuntimeClient` used in the shutdown flow test.

    The fake implements the subset of the :class:`RuntimeClient` interface used
    by :class:`RuntimeSession` in this scenario: runtime status/overview
    polling, a minimal event iterator, and ``shutdown_runtime``.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, Any] | None]] = []
        self._status: Optional[RuntimeStatusResult] = None
        self._overview: Optional[SwarmOverviewResult] = None

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

    async def shutdown_runtime(self, timeout_seconds: int = 30) -> Mapping[str, Any]:
        self.calls.append(("runtime.shutdown", {"timeout_seconds": timeout_seconds}))
        return {"ok": True}

    def iter_events(
        self,
        *,
        subscription_id: str | None = None,
        agent_ids: Iterable[str] | None = None,
        include_runtime: bool = True,
        reconnect: bool = True,
        reconnect_initial_backoff: float = 0.5,
        reconnect_max_backoff: float = 5.0,
    ) -> AsyncIterator[Any]:
        """Return an async iterator for runtime events.

        For this shutdown-focused test we do not need actual events; the
        iterator simply terminates immediately after yielding control back to
        the loop once.
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

        async def _gen() -> AsyncIterator[Any]:
            await asyncio.sleep(0)
            if False:  # pragma: no cover - satisfy type checkers
                yield None  # type: ignore[misc]

        return _gen()


def _make_sample_models() -> tuple[RuntimeStatusResult, SwarmOverviewResult]:
    """Construct minimal but valid runtime status and overview models."""

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
            "agents": [],
        }
    )

    return status, overview


@pytest.mark.asyncio
async def test_overview_shutdown_flow_requests_runtime_shutdown_and_exits() -> None:
    """Requesting shutdown from the overview triggers runtime.shutdown and exit.

    The test drives ``ConsoleApp`` in headless mode, presses the bound
    shutdown key on the overview screen, confirms the shutdown in the
    confirmation screen, and verifies that ``RuntimeSession.shutdown_runtime``
    was called (via the fake client) and that the session was disconnected
    before the app exited.
    """

    status, overview = _make_sample_models()

    fake_client = _FakeRuntimeClient()
    fake_client._status = status
    fake_client._overview = overview

    session = RuntimeSession(client=fake_client, poll_interval=0.01, event_buffer_size=10)

    app = ConsoleApp(session)

    async with app.run_test(headless=True, size=(80, 24)) as pilot:
        await session.connect()

        # Allow background tasks (polling and event consumer) and the UI to
        # process the initial snapshots.
        await pilot.pause()
        await pilot.pause()

        screen = pilot.app.screen
        assert isinstance(screen, OverviewScreen)

        # Trigger the shutdown action from the overview.
        await pilot.press("x")

        # The active screen should now be the runtime-shutdown confirmation
        # screen with the expected prompt text.
        confirm_screen = pilot.app.screen
        assert isinstance(confirm_screen, RuntimeShutdownConfirmScreen)
        body = confirm_screen.query_one("#runtime-shutdown-confirm", Static)
        body_text = str(body.render())
        assert "Runtime shutdown" in body_text

        # Confirm shutdown.
        await pilot.press("y")

        # Allow the background shutdown task to complete and the app to exit.
        await pilot.pause()
        await pilot.pause()

    # After the app exits, the session should be disconnected and the fake
    # client should have recorded a runtime.shutdown call with the default
    # timeout.
    assert not session.is_connected
    assert ("runtime.shutdown", {"timeout_seconds": 30}) in fake_client.calls
