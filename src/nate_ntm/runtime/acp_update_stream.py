from __future__ import annotations

"""Typed, per-session ACP update stream primitives.

This module defines:

- :class:`ReceivedSessionUpdate`: a small receipt record that wraps a typed
  ACP ``SessionUpdate`` object with session-local sequence information;
- :class:`AcpSessionUpdateStream`: an in-memory, replay-capable stream that
  stores a bounded history of :class:`ReceivedSessionUpdate` values for a
  single concrete ACP session and exposes an async subscription API.

The stream is intended to be owned by :class:`AcpAgentSession` instances in
:mod:`nate_ntm.runtime.acp_client`. It replaces the older generic
``AgentEvent`` telemetry pipeline and is used for ACP transport and mux
forwarding only.
"""

import asyncio
from collections import deque
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator, Deque, List, Set

from .acp_types import SessionUpdate


@dataclass(frozen=True, slots=True)
class ReceivedSessionUpdate:
    """Receipt record for a single ACP ``SessionUpdate``.

    Attributes
    ----------
    sequence:
        1-based, monotonically increasing sequence number local to a single
        concrete ACP session.

    received_at:
        Timestamp indicating when the runtime observed this update.

    update:
        The exact typed ACP ``SessionUpdate`` model instance delivered by the
        SDK.
    """

    sequence: int
    received_at: datetime
    update: SessionUpdate


class AcpUpdateStreamError(RuntimeError):
    """Base error type for session update stream failures."""


class StreamClosedError(AcpUpdateStreamError):
    """Raised when publishing to or subscribing from a closed stream."""


class SubscriberOverflowError(AcpUpdateStreamError):
    """Raised when a subscriber's live queue exceeds its capacity."""


class AgentSessionNotActive(AcpUpdateStreamError):
    """Raised when attempting to attach to a non-existent ACP session."""


@dataclass(slots=True)
class AcpSessionUpdateStream:
    """Replay-capable, per-session stream of :class:`ReceivedSessionUpdate`.

    This stream is intended to be owned by exactly one concrete ACP session.

    Properties:
    - Maintains a bounded retained history of updates (oldest entries are
      dropped when the limit is exceeded).
    - Assigns monotonically increasing sequence numbers starting at 1.
    - Exposes an async subscription API that first replays retained history
      and then yields live updates until the stream is closed.

    Live delivery uses **bounded per-subscriber queues** so that slow
    consumers cannot cause unbounded memory growth. When a subscriber's
    queue overflows, that subscriber is terminated with
    :class:`SubscriberOverflowError` rather than silently dropping ACP
    updates.

    The retained-history size (``max_events``) and the per-subscriber
    live-queue capacity (``subscriber_queue_size``) are configurable
    independently, although the defaults keep them aligned.
    """

    max_events: int = 200
    subscriber_queue_size: int | None = None

    _events: Deque[ReceivedSessionUpdate] = field(default_factory=deque, init=False, repr=False)
    _next_sequence: int = field(default=1, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _close_error: BaseException | None = field(default=None, init=False, repr=False)
    _closed_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    # Per-subscriber live queues. Each queue receives new updates published
    # after the subscriber attaches. Subscribers always see a full replay of
    # the retained history first.
    _subscribers: Set[asyncio.Queue[object]] = field(default_factory=set, init=False, repr=False)

    def publish(self, update: SessionUpdate, *, received_at: datetime) -> ReceivedSessionUpdate:
        """Publish ``update`` into this session's stream.

        Returns the corresponding :class:`ReceivedSessionUpdate` instance.
        """

        if self._closed:
            raise StreamClosedError("cannot publish to closed AcpSessionUpdateStream")

        event = ReceivedSessionUpdate(
            sequence=self._next_sequence,
            received_at=received_at,
            update=update,
        )
        self._next_sequence += 1

        # Append to bounded retained history (drop oldest when full).
        self._events.append(event)
        if self.max_events > 0 and len(self._events) > self.max_events:
            # Drop oldest entries until within bound.
            while len(self._events) > self.max_events:
                self._events.popleft()

        # Fan out to subscribers using bounded per-subscriber queues. When a
        # subscriber queue overflows, poison that subscriber with a
        # :class:`SubscriberOverflowError` so the iterator terminates with an
        # explicit error instead of silently dropping updates.
        dead: list[asyncio.Queue[object]] = []
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Clear any pending items for this subscriber and enqueue an
                # overflow sentinel so that the consumer observes a terminal
                # error on the next read.
                while not queue.empty():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:  # pragma: no cover - defensive
                        break

                try:
                    queue.put_nowait(
                        SubscriberOverflowError(
                            "subscriber queue overflow in AcpSessionUpdateStream"
                        )
                    )
                except asyncio.QueueFull:  # pragma: no cover - defensive
                    # The queue was just drained, so this should not occur. If it
                    # does, drop the subscriber and continue.
                    pass

                dead.append(queue)

        for queue in dead:
            self._subscribers.discard(queue)

        return event

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[AsyncIterator[ReceivedSessionUpdate]]:
        """Subscribe to this stream.

        The returned async iterator will:
        - yield a snapshot of retained history as of subscription time;
        - then yield live updates until the stream is closed.
        """

        # Snapshot retained history at subscription time.
        snapshot: List[ReceivedSessionUpdate] = list(self._events)

        # If already closed, we still replay the retained history, but there
        # will be no live updates.
        if self._closed:
            live_queue: asyncio.Queue[object] | None = None
        else:
            # Determine the per-subscriber queue capacity. When an explicit
            # ``subscriber_queue_size`` is provided and is positive, it is used
            # directly. Otherwise, fall back to ``max_events`` and ensure a
            # minimum capacity of 1 so queues are always bounded.
            if self.subscriber_queue_size is not None and self.subscriber_queue_size > 0:
                maxsize = self.subscriber_queue_size
            elif self.max_events > 0:
                maxsize = self.max_events
            else:
                maxsize = 1

            live_queue = asyncio.Queue[object](maxsize=maxsize)
            self._subscribers.add(live_queue)

        async def _iterator() -> AsyncIterator[ReceivedSessionUpdate]:
            # First, drain the immutable snapshot.
            for ev in snapshot:
                yield ev

            # Then, if still live, consume the live queue.
            if live_queue is None:
                return

            try:
                while True:
                    # If there is a pending item, consume it without blocking.
                    if not live_queue.empty():
                        item = live_queue.get_nowait()
                        if isinstance(item, BaseException):
                            raise item
                        assert isinstance(item, ReceivedSessionUpdate)
                        yield item
                        continue

                    # No pending items. If the stream has been closed and
                    # there is nothing left to read, terminate the iterator
                    # so callers observe a natural end-of-stream signal.
                    if self._closed:
                        break

                    # Wait for either a new item or a stream-level close
                    # signal so that subscribers blocked in ``anext`` are
                    # promptly awakened when :meth:`close` is called.
                    get_task = asyncio.create_task(live_queue.get())
                    close_task = asyncio.create_task(self._closed_event.wait())

                    done, pending = await asyncio.wait(
                        {get_task, close_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    if get_task in done:
                        close_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await close_task

                        item = get_task.result()
                        if isinstance(item, BaseException):
                            raise item
                        assert isinstance(item, ReceivedSessionUpdate)
                        yield item
                    else:
                        # ``close_task`` completed first: cancel the pending
                        # ``get_task`` and, if the stream is now closed and the
                        # queue is still empty, terminate. If new items were
                        # enqueued concurrently with ``close``, the loop will
                        # pick them up on the next iteration.
                        get_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await get_task

                        if self._closed and live_queue.empty():
                            break
            finally:
                # Iterator-local cleanup is handled via ``aclose`` in the
                # surrounding context manager; no additional logic is required
                # here beyond normal loop termination.
                pass

        iterator = _iterator()
        try:
            yield iterator
        finally:
            # Ensure the subscriber is deterministically removed when the
            # subscription context exits, even if the iterator is never
            # consumed or exhausted.
            if live_queue is not None:
                self._subscribers.discard(live_queue)

            # Proactively close the async generator so that any internal
            # cleanup (e.g. cancellation of pending tasks) runs promptly.
            with suppress(Exception):
                await iterator.aclose()

    def close(self, error: BaseException | None = None) -> None:
        """Mark the stream as closed.

        After calling this method, further publishes will raise
        :class:`StreamClosedError`. Existing subscribers will be **actively
        unblocked** if they are currently waiting for the next update; they
        will first drain any already-queued items and then observe a natural
        end-of-stream signal. New subscribers will receive only the retained
        snapshot and then terminate.

        When ``error`` is provided, it is recorded for diagnostics and may
        be surfaced by higher-level components if needed.
        """

        if self._closed:
            return

        self._closed = True
        if error is not None and self._close_error is None:
            self._close_error = error

        # Wake any subscribers currently blocked in ``anext`` by signalling
        # the shared closed event; their iterators will terminate once any
        # queued items have been drained.
        self._closed_event.set()


__all__ = [
    "ReceivedSessionUpdate",
    "AcpUpdateStreamError",
    "StreamClosedError",
    "SubscriberOverflowError",
    "AgentSessionNotActive",
    "AcpSessionUpdateStream",
]
