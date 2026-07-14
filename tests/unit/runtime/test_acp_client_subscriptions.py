from __future__ import annotations

from datetime import datetime
from pathlib import Path
import asyncio

import pytest

from nate_ntm.config.runtime_config import RuntimeConfig, load_runtime_config
from nate_ntm.runtime.acp_client import NateOhaAcpClient, _EVENT_STREAM_CLOSED
from nate_ntm.runtime.events import AgentEvent, AgentEventSource


def _make_config(tmp_path: Path) -> RuntimeConfig:
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)
    return load_runtime_config(project_path=project_root)


@pytest.mark.asyncio
async def test_subscribe_events_broadcasts_to_multiple_subscribers(tmp_path: Path) -> None:
    """Multiple subscribers receive the same emitted event independently.

    This exercise ensures that :meth:`NateOhaAcpClient.subscribe_events` uses
    per-subscriber queues with broadcast semantics rather than a single
    work-queue. A single call to :meth:`_emit_event` must deliver the event to
    every active subscriber for the agent.
    """

    config = _make_config(tmp_path)
    client = NateOhaAcpClient(config=config)

    agent_id = "agent-broadcast-1"
    event = AgentEvent(
        event_id="e1",
        timestamp=datetime(2024, 1, 1, 12, 0, 0),
        agent_id=agent_id,
        source=AgentEventSource.ACP,
        type="acp.test_event",
        payload={"value": 42},
    )

    async with client.subscribe_events(agent_id) as events1:
        async with client.subscribe_events(agent_id) as events2:
            # Emit a single event; both subscriptions should observe it
            # independently without consuming it for one another.
            client._emit_event(event)

            ev1 = await asyncio.wait_for(events1.__anext__(), timeout=1.0)
            ev2 = await asyncio.wait_for(events2.__anext__(), timeout=1.0)

    # Both subscribers must have seen the exact same event object.
    assert ev1 is event
    assert ev2 is event

    # After leaving the subscription contexts, all per-agent subscribers should
    # have been unregistered.
    assert agent_id not in client._event_subscribers


@pytest.mark.asyncio
async def test_subscribe_events_cleans_up_on_timeout(tmp_path: Path) -> None:
    """Timeouts in consumers do not leak subscriptions.

    The subscription context manager is responsible for unregistering
    subscribers even when consumers experience timeouts while awaiting events.
    """

    config = _make_config(tmp_path)
    client = NateOhaAcpClient(config=config)

    agent_id = "agent-timeout-1"

    async with client.subscribe_events(agent_id) as events:
        # No events are emitted; waiting for the next item with a short timeout
        # should raise asyncio.TimeoutError.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(events.__anext__(), timeout=0.01)

    # The agent's subscriber set should be empty once the context exits.
    assert agent_id not in client._event_subscribers


@pytest.mark.asyncio
async def test_subscribe_events_cleans_up_on_cancelled_consumer(tmp_path: Path) -> None:
    """Cancellation of a waiting consumer removes its subscription.

    When a task awaiting events from a subscription is cancelled, the
    subscription's iterator ``finally`` block and the context manager's
    teardown must still unregister the subscriber so that no stale queues
    remain.
    """

    config = _make_config(tmp_path)
    client = NateOhaAcpClient(config=config)

    agent_id = "agent-cancel-1"

    async with client.subscribe_events(agent_id) as events:
        task = asyncio.create_task(events.__anext__())

        # Allow the task to start and block on the internal queue before
        # cancelling it. ``sleep(0)`` is used here purely as a scheduling
        # yield, not as an event-synchronization mechanism.
        await asyncio.sleep(0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # After the subscription context exits, there should be no lingering
    # subscriber queues for the agent.
    assert agent_id not in client._event_subscribers


@pytest.mark.asyncio
async def test_close_event_subscribers_terminates_stream(tmp_path: Path) -> None:
    """Closing event subscribers terminates active iterators promptly.

    This simulates the lifecycle behavior used when an agent stops or fails.
    Calling the private ``_close_event_subscribers`` helper should cause any
    active subscription iterator to complete rather than wait indefinitely.
    """

    config = _make_config(tmp_path)
    client = NateOhaAcpClient(config=config)

    agent_id = "agent-close-1"

    async with client.subscribe_events(agent_id) as events:
        # Simulate agent shutdown by closing all subscribers for this agent.
        client._close_event_subscribers(agent_id)

        # The iterator should terminate with StopAsyncIteration on the next
        # attempted ``__anext__`` call instead of blocking.
        with pytest.raises(StopAsyncIteration):
            await events.__anext__()

    # No subscribers should remain registered for the agent once both the
    # iterator and the subscription context have unwound.
    assert agent_id not in client._event_subscribers


@pytest.mark.asyncio
async def test_close_event_subscribers_inserts_sentinel_when_queue_full(tmp_path: Path) -> None:
    """Closing subscribers inserts a sentinel even when the queue is full.

    This guards against regressions where a full per-agent queue would
    prevent the end-of-stream marker from being enqueued, leaving
    consumers blocked on ``queue.get()``.
    """

    config = _make_config(tmp_path)
    client = NateOhaAcpClient(config=config)

    agent_id = "agent-close-full-1"

    # Register a subscriber queue and fill it to capacity using the
    # normal event-emission path so that ``queue.full()`` is true when
    # ``_close_event_subscribers`` runs.
    queue = client._register_event_subscriber(agent_id)

    event = AgentEvent(
        event_id="e-base",
        timestamp=datetime(2024, 1, 1, 12, 0, 0),
        agent_id=agent_id,
        source=AgentEventSource.ACP,
        type="acp.test_event",
        payload={"value": 1},
    )

    for _ in range(queue.maxsize):
        client._emit_event(event)

    assert queue.qsize() == queue.maxsize

    client._close_event_subscribers(agent_id)

    # After closure, the sentinel should be present exactly once in the
    # queue despite it having been full.
    items: list[object] = []
    while not queue.empty():
        items.append(queue.get_nowait())

    assert _EVENT_STREAM_CLOSED in items
    assert items.count(_EVENT_STREAM_CLOSED) == 1
    assert len(items) == queue.maxsize


@pytest.mark.asyncio
async def test_subscribe_events_close_inserts_sentinel_when_queue_full(tmp_path: Path) -> None:
    """Exiting ``subscribe_events`` inserts a sentinel when the queue is full.

    This mirrors the behavior of ``_close_event_subscribers`` and
    ensures that per-subscriber teardown cannot leave blocked
    consumers when their queues are at capacity.
    """

    config = _make_config(tmp_path)
    client = NateOhaAcpClient(config=config)

    agent_id = "agent-sub-close-full-1"

    # Manually enter the async context manager so we can inspect the
    # underlying queue before and after ``__aexit__`` runs.
    cm = client.subscribe_events(agent_id)
    _events_iter = await cm.__aenter__()

    subscribers = client._event_subscribers.get(agent_id)
    assert subscribers is not None and len(subscribers) == 1
    (queue,) = tuple(subscribers)

    event = AgentEvent(
        event_id="e-base",
        timestamp=datetime(2024, 1, 1, 12, 0, 0),
        agent_id=agent_id,
        source=AgentEventSource.ACP,
        type="acp.test_event",
        payload={"value": 1},
    )

    for _ in range(queue.maxsize):
        client._emit_event(event)

    assert queue.qsize() == queue.maxsize

    # Exiting the context should enqueue the close sentinel even though
    # the queue is full.
    await cm.__aexit__(None, None, None)

    items: list[object] = []
    while not queue.empty():
        items.append(queue.get_nowait())

    assert _EVENT_STREAM_CLOSED in items
    assert items.count(_EVENT_STREAM_CLOSED) == 1
    assert len(items) == queue.maxsize

