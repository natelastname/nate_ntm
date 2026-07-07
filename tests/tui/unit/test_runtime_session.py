from __future__ import annotations

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
from nate_ntm.tui.runtime_session import RuntimeSession


class _FakeRuntimeClient:
    """Test double for :class:`RuntimeClient`.

    This fake avoids any real network access by implementing the subset
    of the :class:`RuntimeClient` interface used by
    :class:`RuntimeSession`.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, Any] | None]] = []
        self._status: Optional[RuntimeStatusResult] = None
        self._overview: Optional[SwarmOverviewResult] = None
        self._agent_details: dict[str, AgentDetailResult] = {}
        self._events: Iterable[EventsNotify] | None = None

    # Control-plane helpers -----------------------------------------------------

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
        self.calls.append(("agent.get_detail", {"agent_id": agent_id, "max_events": max_events}))
        try:
            return self._agent_details[agent_id]
        except KeyError:  # pragma: no cover - defensive
            raise RuntimeError(f"no detail configured for {agent_id!r}")

    # Event-stream helper -------------------------------------------------------

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
        # Record that iter_events was requested; tests can inspect this
        # via ``calls``.
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


def _make_sample_models() -> tuple[RuntimeStatusResult, SwarmOverviewResult, AgentDetailResult, AgentDetailEvent]:
    """Construct minimal but valid model instances for use in tests.

    The shapes mirror the examples in ``tests/unit/api/test_client_typed.py``
    so that we stay aligned with the public control API contract.
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
            "events": [],
        }
    )

    event = AgentDetailEvent.model_validate(
        {
            "event_id": "evt-1",
            "timestamp": "2026-07-07T12:00:00Z",
            "agent_id": "agent-1",
            "source": "runtime",
            "type": "started",
            "payload": {"info": "started"},
        }
    )

    return status, overview, detail, event


def test_connect_starts_background_tasks_and_populates_cache() -> None:
    """connect() should start polling and event tasks and refresh snapshots."""

    async def main() -> None:
        status, overview, detail, event = _make_sample_models()

        fake = _FakeRuntimeClient()
        fake._status = status
        fake._overview = overview
        fake._agent_details["agent-1"] = detail

        notify = EventsNotify(subscription_id="sub-1", event=event)
        fake._events = [notify]

        session = RuntimeSession(client=fake, poll_interval=0.01, event_buffer_size=10)

        await session.connect()

        # Allow at least one polling iteration and one event to be
        # processed. The small poll_interval keeps this quick.
        await asyncio.sleep(0.05)

        assert session.is_connected
        assert session.get_cached_runtime_status() == status
        assert session.get_cached_swarm_overview() == overview
        assert session.get_recent_events() == [event]

        # Agent detail cache should be empty until explicitly requested.
        assert session.get_cached_agent_detail("agent-1") is None

        await session.disconnect()
        assert not session.is_connected

    asyncio.run(main())


def test_get_agent_detail_uses_cache_and_supports_force_refresh() -> None:
    """get_agent_detail() should cache results and support force_refresh."""

    async def main() -> None:
        status, overview, detail, event = _make_sample_models()

        fake = _FakeRuntimeClient()
        fake._status = status
        fake._overview = overview
        fake._agent_details["agent-1"] = detail

        session = RuntimeSession(client=fake, poll_interval=0.1)

        await session.connect()

        # First call fetches from the fake client and caches the result.
        result1 = await session.get_agent_detail("agent-1")
        assert result1 is detail
        assert session.get_cached_agent_detail("agent-1") is detail

        # Update the fake's mapping to a new object and ensure that a cached
        # lookup returns the original until force_refresh is used.
        new_detail = AgentDetailResult.model_validate(
            {
                "agent": {
                    "agent_id": "agent-1",
                    "display_name": "Agent One",
                    "status": "succeeded",
                    "agent_mail_identity": "agent-1@example.test",
                    "conversation_id": "conv-1",
                    "last_error": None,
                },
                "events": [],
            }
        )
        fake._agent_details["agent-1"] = new_detail

        result2 = await session.get_agent_detail("agent-1")
        assert result2 is detail  # still cached

        result3 = await session.get_agent_detail("agent-1", force_refresh=True)
        assert result3 is new_detail
        assert session.get_cached_agent_detail("agent-1") is new_detail

        await session.disconnect()

    asyncio.run(main())


def test_wait_for_update_signals_on_state_change() -> None:
    """wait_for_update() should wake when cached state changes."""

    async def main() -> None:
        status, overview, detail, event = _make_sample_models()

        fake = _FakeRuntimeClient()
        fake._status = status
        fake._overview = overview
        fake._agent_details["agent-1"] = detail

        # Use a single pre-configured event to drive an update.
        notify = EventsNotify(subscription_id="sub-1", event=event)
        fake._events = [notify]

        session = RuntimeSession(client=fake, poll_interval=0.01, event_buffer_size=10)

        await session.connect()

        # Capture the initial update sequence and then wait for a newer one
        # after any background task reports an update.
        initial_seq = session.update_seq

        new_seq = await session.wait_for_update(last_seen=initial_seq, timeout=1.0)
        assert new_seq > initial_seq

        # Give the background event consumer a brief window to process the
        # configured event; we only care that the wait_for_update() mechanism
        # wakes on *some* change, not which task produced it first.
        await asyncio.sleep(0.05)
        assert session.get_recent_events() == [event]

        await session.disconnect()

    asyncio.run(main())


def test_select_agent_updates_shared_selection_and_sequence() -> None:
    """select_agent() should update selection and bump the update sequence.

    The selection is a lightweight piece of shared UI state; it should not
    require the session to be connected in order to function.
    """

    fake = _FakeRuntimeClient()
    session = RuntimeSession(client=fake)

    assert session.selected_agent_id is None
    initial_seq = session.update_seq

    session.select_agent("agent-1")
    assert session.selected_agent_id == "agent-1"
    assert session.update_seq == initial_seq + 1

    # Selecting the same agent again should be a no-op for the sequence.
    session.select_agent("agent-1")
    assert session.update_seq == initial_seq + 1

    # Changing the selection should bump the sequence again.
    session.select_agent("agent-2")
    assert session.selected_agent_id == "agent-2"
    assert session.update_seq == initial_seq + 2



def test_disconnect_cancels_tasks_and_closes_event_iterator(monkeypatch: pytest.MonkeyPatch) -> None:
    """disconnect() should cancel tasks and close the event iterator."""

    async def main() -> None:
        status, overview, detail, event = _make_sample_models()

        fake = _FakeRuntimeClient()
        fake._status = status
        fake._overview = overview

        notify = EventsNotify(subscription_id="sub-1", event=event)
        fake._events = [notify]

        session = RuntimeSession(client=fake, poll_interval=0.1, event_buffer_size=10)

        # Wrap the original iter_events to track whether its generator was
        # explicitly closed via aclose().
        original_iter_events = fake.iter_events
        tracking_holder: dict[str, _TrackingGen] = {}

        class _TrackingGen:
            def __init__(self, agen: AsyncIterator[EventsNotify]) -> None:
                self._agen = agen
                self.closed = False

            def __aiter__(self) -> "_TrackingGen":
                return self

            async def __anext__(self) -> EventsNotify:
                return await self._agen.__anext__()

            async def aclose(self) -> None:
                self.closed = True
                await self._agen.aclose()

        def _wrapped_iter_events(*args: Any, **kwargs: Any) -> AsyncIterator[EventsNotify]:
            agen = original_iter_events(*args, **kwargs)
            tracking = _TrackingGen(agen)
            tracking_holder["tracking"] = tracking
            return tracking

        monkeypatch.setattr(fake, "iter_events", _wrapped_iter_events)

        await session.connect()

        # Give the event task a chance to start.
        await asyncio.sleep(0.05)

        await session.disconnect()

        tracking = tracking_holder.get("tracking")
        assert tracking is not None
        assert tracking.closed is True

    asyncio.run(main())


def test_control_and_event_degraded_flags_are_tracked_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control-plane and event-stream degradation are independent flags."""

    async def main() -> None:
        status, overview, detail, event = _make_sample_models()

        fake = _FakeRuntimeClient()
        fake._status = status
        fake._overview = overview

        # Event iterator that will raise once to simulate a hard failure.
        def failing_iter_events(**kwargs: Any) -> AsyncIterator[EventsNotify]:
            async def _gen() -> AsyncIterator[EventsNotify]:
                raise RuntimeError("event-stream failure")
                if False:  # pragma: no cover
                    yield None  # type: ignore[misc]

            return _gen()

        monkeypatch.setattr(fake, "iter_events", failing_iter_events)

        session = RuntimeSession(client=fake, poll_interval=0.01)

        await session.connect()

        # Allow background tasks to run and observe the failure.
        await asyncio.sleep(0.05)

        assert session.events_degraded is True
        assert "event-stream failure" in (session.events_error or "")

        # Control plane should still be healthy because polling succeeds.
        assert session.control_degraded is False

        await session.disconnect()

    asyncio.run(main())
