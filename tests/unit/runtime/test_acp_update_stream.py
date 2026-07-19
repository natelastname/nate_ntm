from __future__ import annotations

from datetime import datetime

import asyncio

import pytest

from nate_ntm.runtime.acp_types import SessionUpdate
from nate_ntm.runtime.acp_update_stream import (
    AcpSessionUpdateStream,
    ReceivedSessionUpdate,
    StreamClosedError,
    SubscriberOverflowError,
)


class _DummyUpdate(SessionUpdate):  # type: ignore[misc]
    """Minimal concrete ``SessionUpdate`` stand-in for tests.

    The real ACP ``SessionUpdate`` models inherit from the ACP SDK's
    ``BaseModel``. For unit tests we define a lightweight subclass so that
    the stream can be exercised without depending on specific ACP variants.
    """

    # ``SessionUpdate`` is currently an alias of ``acp.schema.BaseModel``.
    # Pydantic models accept arbitrary fields by default, so no additional
    # attributes are required here.


@pytest.mark.asyncio
async def test_publish_assigns_monotonic_sequence_and_retains_history() -> None:
    stream = AcpSessionUpdateStream(max_events=3)

    t0 = datetime(2024, 1, 1, 12, 0, 0)
    t1 = datetime(2024, 1, 1, 12, 0, 1)
    t2 = datetime(2024, 1, 1, 12, 0, 2)
    t3 = datetime(2024, 1, 1, 12, 0, 3)

    u0 = _DummyUpdate()
    u1 = _DummyUpdate()
    u2 = _DummyUpdate()
    u3 = _DummyUpdate()

    e0 = stream.publish(u0, received_at=t0)
    e1 = stream.publish(u1, received_at=t1)
    e2 = stream.publish(u2, received_at=t2)
    e3 = stream.publish(u3, received_at=t3)

    assert [e.sequence for e in (e0, e1, e2, e3)] == [1, 2, 3, 4]

    # With max_events=3, only the last three events are retained.
    # Close the stream so that subscriptions see a finite snapshot.
    stream.close()

    async with stream.subscribe() as updates:
        seen = [e async for e in updates]

    assert [e.sequence for e in seen] == [2, 3, 4]


@pytest.mark.asyncio
async def test_subscribe_replays_history_then_yields_live_updates() -> None:
    stream = AcpSessionUpdateStream(max_events=10)

    t0 = datetime(2024, 1, 1, 12, 0, 0)
    t1 = datetime(2024, 1, 1, 12, 0, 1)
    t2 = datetime(2024, 1, 1, 12, 0, 2)

    stream.publish(_DummyUpdate(), received_at=t0)
    stream.publish(_DummyUpdate(), received_at=t1)

    async with stream.subscribe() as updates:
        # First two are replayed history in order.
        first = await anext(updates)
        second = await anext(updates)

        assert first.sequence == 1
        assert second.sequence == 2

        # Now publish a live update and ensure it appears next.
        e2 = stream.publish(_DummyUpdate(), received_at=t2)
        third = await anext(updates)

        assert third.sequence == e2.sequence == 3


@pytest.mark.asyncio
async def test_close_rejects_future_publishes_and_allows_snapshot_subscription() -> None:
    stream = AcpSessionUpdateStream(max_events=10)

    t0 = datetime(2024, 1, 1, 12, 0, 0)
    stream.publish(_DummyUpdate(), received_at=t0)

    stream.close()

    # Further publishes are rejected.
    with pytest.raises(StreamClosedError):
        stream.publish(_DummyUpdate(), received_at=t0)

    # Subscribers created after close still see the retained snapshot but no
    # live updates.
    async with stream.subscribe() as updates:
        items = [e async for e in updates]

    assert len(items) == 1
    assert isinstance(items[0], ReceivedSessionUpdate)



@pytest.mark.asyncio
async def test_live_subscriber_waiting_for_next_item_is_unblocked_on_close() -> None:
    stream = AcpSessionUpdateStream(max_events=10)

    async with stream.subscribe() as updates:
        # No items have been published; the iterator will block waiting for
        # the first live update.
        pending = asyncio.create_task(anext(updates))

        # Allow the event loop to schedule the pending ``anext`` so that it is
        # actually waiting on the underlying queue.
        await asyncio.sleep(0)

        stream.close()

        # ``close()`` must actively wake the subscriber and make the pending
        # ``anext`` complete with ``StopAsyncIteration`` rather than hanging.
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(pending, timeout=0.1)


@pytest.mark.asyncio
async def test_live_subscriber_drains_queued_events_before_terminating_on_close() -> None:
    stream = AcpSessionUpdateStream(max_events=10)

    t0 = datetime(2024, 1, 1, 12, 0, 0)
    t1 = datetime(2024, 1, 1, 12, 0, 1)

    async with stream.subscribe() as updates:
        ev0 = stream.publish(_DummyUpdate(), received_at=t0)
        ev1 = stream.publish(_DummyUpdate(), received_at=t1)

        # Closing the stream while there are pending items in the live
        # queue should still allow the subscriber to drain those items
        # before observing end-of-stream.
        stream.close()

        first = await asyncio.wait_for(anext(updates), timeout=0.1)
        second = await asyncio.wait_for(anext(updates), timeout=0.1)

        assert first is ev0
        assert second is ev1

        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(updates), timeout=0.1)


@pytest.mark.asyncio
async def test_subscriber_overflow_raises_error_for_slow_consumer() -> None:
    # Use a tiny max_events so that the per-subscriber live queue capacity is
    # also tiny and easy to overflow.
    stream = AcpSessionUpdateStream(max_events=1)

    t0 = datetime(2024, 1, 1, 12, 0, 0)
    t1 = datetime(2024, 1, 1, 12, 0, 1)

    async with stream.subscribe() as updates:
        # Publish one update and leave it queued so that the live queue reaches
        # capacity for this subscriber.
        stream.publish(_DummyUpdate(), received_at=t0)

        # The second publish should overflow the subscriber queue. The
        # subscriber must then observe a ``SubscriberOverflowError`` rather
        # than silently losing updates.
        stream.publish(_DummyUpdate(), received_at=t1)

        with pytest.raises(SubscriberOverflowError):
            await asyncio.wait_for(anext(updates), timeout=0.1)


@pytest.mark.asyncio
async def test_subscribe_context_cleans_up_subscriber_even_if_iterator_unused() -> None:
    stream = AcpSessionUpdateStream(max_events=10)

    # No subscribers initially.
    assert len(stream._subscribers) == 0

    async with stream.subscribe() as updates:  # noqa: F841
        # Subscriber is registered for the duration of the context.
        assert len(stream._subscribers) == 1

    # After context exit, the subscriber queue is removed even though the
    # iterator was never consumed.
    assert len(stream._subscribers) == 0
