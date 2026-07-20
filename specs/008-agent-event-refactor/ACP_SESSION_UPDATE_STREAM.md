# ACP Session Update Stream — Epic 008 Implementation Plan

## Purpose

Introduce a typed, session-owned ACP update stream that becomes the canonical representation of ACP `session/update` traffic inside the runtime.

This epic refactors how ACP updates are represented and delivered **within** the nate-ntm runtime. It does *not* redesign runtime observability or remove generic runtime event APIs. Instead, it establishes one typed path for ACP session updates:

```
ACP SDK SessionUpdate
        ↓
NateNtmAcpProtocolClient.session_update
        ↓
current AcpAgentSession
        ↓
AcpSessionUpdateStream
        ↓
subscribe_acp_updates()
```

The exact typed `SessionUpdate` received from ACP must remain intact across this path. Sequencing, replay, and delivery semantics are owned by the in-memory `AcpSessionUpdateStream`; wire serialization remains the responsibility of the ACP SDK at external connection boundaries.

Previous Epic 001 and early Swarm/ACP mux designs described older decisions. They do not block this work. Update or supersede them after the code reflects the new design for the typed ACP session update stream.

------------------------------------------------------------------------

## 1. Required End State

At completion of this epic:

1. Every active `AcpAgentSession` owns one bounded, in-memory ACP update stream.
2. Every ACP `SessionUpdate` received for that session is published exactly once into that stream.
3. Subscribers receive:
   - retained updates in sequence order; then
   - live updates without a gap, duplicate, or reordering at the replay boundary.
4. Subscriber overflow never silently drops an ACP update;
   - overflow is surfaced as an explicit failure for that subscriber.
5. Closing or replacing an ACP session closes its stream and terminates its subscribers in a well-defined way.
6. A new ACP session receives a new stream and a new sequence space.
7. ACP `SessionUpdate` delivery to ACP-aware consumers inside this runtime MUST
   flow via `AcpSessionUpdateStream`. The typed stream is the canonical
   in-memory representation of ACP `session/update` traffic.
8. All ACP-aware consumers in this runtime MUST subscribe via
   `subscribe_acp_updates(agent_id)`. New ACP-specific subscription APIs MUST
   NOT be introduced. Existing helpers such as
   `subscribe_events`/`iter_events`/`wait_for_event` are considered legacy and
   MUST either delegate to this API or be removed once all call sites have
   migrated.

------------------------------------------------------------------------

## 2. Out of Scope

This epic intentionally does **not**:

- implement `SwarmACPMux`;
- implement any ACP multiplexing or fan-out layer on top of `AcpSessionUpdateStream`;
- remove runtime lifecycle event producers for non-ACP telemetry (for example,
  process or scheduler events);
- redesign runtime observability or logging strategies;
- redesign Agent Mail telemetry or its delivery mechanisms;
- remove the remaining generic runtime event APIs (for example, `/events`
  WebSocket streaming or JSON-RPC `events.*` methods).

Those changes belong in later, dedicated epics that can build on the typed ACP session update stream from this document.

------------------------------------------------------------------------

## 3. Core Design Decisions

### 3.1 The stream belongs to a concrete ACP session

The stream belongs to `AcpAgentSession`, not to the logical agent.

```
logical agent
    └── current AcpAgentSession
            └── AcpSessionUpdateStream
```

A restart, replacement, or newly established ACP session creates a new stream.

The old stream closes before the old session is discarded.

Any long-lived attachment to the stream (for example, a future mux or bridge) targets one concrete ACP session. It does not silently follow the logical agent into a replacement session. When the session closes, the attachment ends. Reattaching to a replacement session is an explicit higher-level operation owned by the caller.

**Ownership**

- Each `AcpAgentSession` exclusively owns one `AcpSessionUpdateStream`.
- The stream is created when the session is created.
- The stream is closed when the session terminates.
- A stream MUST NOT be shared between sessions.

### 3.2 Keep the real ACP type

Use the narrowest authoritative ACP SDK type that covers all `session/update` payloads. When the SDK exposes a dedicated `SessionUpdate` union or base class, import and re-export that; until then, alias to the SDK's documented common base model for all update variants.

Do not define:

```
SessionUpdate = Any
```

If a stable SDK alias already exists, import and re-export it from one internal module. If not, define an explicit union there.

Example shape (current SDK):

```
# src/nate_ntm/runtime/acp_types.py

from acp import schema as acp_schema

# Pragmatic upper bound: all concrete session/update models currently inherit
# from acp.schema.BaseModel. When the ACP SDK introduces a narrower
# SessionUpdate union, this alias MUST be updated to re-export that type
# instead.
SessionUpdate = acp_schema.BaseModel

__all__ = ["SessionUpdate"]
```

The exact import should match the installed ACP SDK and SHOULD be updated if
and when the SDK grows a dedicated `SessionUpdate` union or base class.

### 3.3 Use a small receipt record

The stream should retain the exact `SessionUpdate` object plus only the metadata required for ordering and diagnostics.

```
@dataclass(frozen=True, slots=True)
class ReceivedSessionUpdate:
    sequence: int
    received_at: datetime
    update: SessionUpdate
```

The wrapper exists for specific reasons:

- `sequence` defines total order within one concrete session;
- it makes replay boundaries observable;
- it makes duplicate and gap detection straightforward;
- `received_at` records when the runtime observed the update.

Do not add generic fields such as `source`, string `type`, arbitrary `payload`, or duplicated agent identity.

### 3.4 The stream assigns sequence numbers

Callers publish typed updates, not preconstructed records:

```
stream.publish(update, received_at=clock())
```

The stream assigns the next sequence number and constructs `ReceivedSessionUpdate`.

Sequence numbers are 1-based and monotonically increasing **per session** (that
is, per `AcpSessionUpdateStream`). There is no requirement for a single global
sequence across sessions; different sessions may reuse the same sequence values.

This creates one canonical sequencing path and prevents callers from supplying
duplicate or out-of-order sequence values.

### 3.5 Replay and live delivery must have an atomic boundary

Subscription registration must establish one sequence boundary atomically relative to publication.

A subscriber should contain two separate delivery areas:

```
subscriber
    ├── immutable replay snapshot
    └── bounded live queue
```

Under the stream's synchronization boundary:

1. copy the retained history into the subscriber's replay snapshot;
2. register the subscriber for subsequent live updates.

The subscriber iterator drains the replay snapshot before consuming the live queue.

Live updates must never be inserted into the replay snapshot, and replay updates must never compete for capacity in the bounded live queue.

This guarantees:

```
retained updates through sequence N
then live updates beginning at sequence N+1
```

### 3.6 Overflow is explicit failure

Each subscriber has a bounded live queue.

When publishing a live update:

- if the queue has capacity, enqueue the update;
- if the queue is full, transition that subscriber to a **terminal overflow
  state** so that the next ``anext`` (or equivalent) raises
  :class:`SubscriberOverflowError` instead of silently dropping data;
- do not block unrelated subscribers.

Once overflow has been signalled for a subscriber, the stream is free to drop
any queued or future updates for that subscriber. The important guarantee is
that loss is **never silent**: either the subscriber observes every update in
order, or it eventually observes :class:`SubscriberOverflowError` and can treat
its view of the stream as invalid.

The retained session history remains bounded independently of subscriber queues.

### 3.7 Closure is persistent

The stream must retain closed state:

```
_closed: bool
_close_error: BaseException | None
```

Required behavior:

- `publish()` after close raises `StreamClosedError`.
- Current subscribers finish retained and already-queued updates, then terminate.
- A subscriber created after close receives the final retained snapshot and then terminates immediately.
- Subscriber cancellation always unregisters and releases its queue.
- Calling `close()` more than once is harmless.

A session failure may close the stream with an associated cause. Normal session shutdown may close it without one.

### 3.8 Missing sessions are explicit errors

Attempting to subscribe to an agent that has no active ACP session should raise a clear exception such as:

```
AgentSessionNotActive
```

Do not return an empty iterator. An empty iterator would make an invalid attachment indistinguishable from a valid stream that ended normally.

------------------------------------------------------------------------

## 4. New Runtime Types

### 4.1 ACP type module

Create:

```
src/nate_ntm/runtime/acp_types.py
```

Responsibilities:

- expose the `SessionUpdate` type alias used by the runtime, following the
  guidance in §3.2 (narrowest authoritative ACP SDK type; never `Any`);
- isolate the exact ACP SDK import path so that other modules do not import
  from :mod:`acp` directly;
- contain no serialization or payload-normalization logic.

### 4.2 Session update stream module

Create:

```
src/nate_ntm/runtime/acp_update_stream.py
```

Define the following runtime types exactly as specified in §3.3. Section 9.1
provides a non-normative reference Python implementation of these types:

- ``ReceivedSessionUpdate`` – the small, immutable receipt record wrapping a
  typed :class:`SessionUpdate` plus sequence number and receipt timestamp.
- ``AcpUpdateStreamError(RuntimeError)`` – base error type for session update
  stream failures.
- ``StreamClosedError(AcpUpdateStreamError)`` – raised when publishing to a
  closed stream. Subscribing to a closed stream remains valid and replays the
  final retained snapshot before terminating.
- ``SubscriberOverflowError(AcpUpdateStreamError)`` – raised when a
  subscriber's live queue exceeds its configured capacity.
- ``AgentSessionNotActive(AcpUpdateStreamError)`` – raised when attempting to
  subscribe for an agent that does not have an active ACP session (including
  both missing and inactive sessions).

Define the stream:

```
class AcpSessionUpdateStream:
    def publish(
        self,
        update: SessionUpdate,
        *,
        received_at: datetime,
    ) -> ReceivedSessionUpdate:
        …

    @asynccontextmanager
    async def subscribe(
        self,
    ) -> AsyncIterator[AsyncIterator[ReceivedSessionUpdate]]:
        …

    def close(
        self,
        error: BaseException | None = None,
    ) -> None:
        …
```

The implementation must satisfy these invariants:

- bounded retained history;
- monotonic sequence numbers beginning at 1;
- atomic replay/live boundary;
- bounded live queue per subscriber;
- overflow terminates only the affected subscriber;
- no silent event loss;
- persistent close state;
- deterministic cleanup on cancellation.

Use the smallest synchronization mechanism that correctly protects publication, snapshot creation, registration, removal, and close state. Do not create a distributed or generalized event-bus abstraction.

### 4.3 ACP subscription API

Expose a single typed subscription API for ACP session updates on the runtime's
ACP client:

```
@asynccontextmanager
async def subscribe_acp_updates(
    self,
    agent_id: str,
) -> AsyncIterator[AsyncIterator[ReceivedSessionUpdate]]:
    ...
```

Normative behavior:

- Resolve the agent's currently active :class:`AcpAgentSession` from the
  session registry **exactly once** when the subscription begins.
- Treat sessions in `"starting"` or `"running"` state as active; all other
  states (including a missing session record) MUST cause the method to raise
  :class:`AgentSessionNotActive` as described in 
  §§3.8 and 4.2.
- Capture that concrete session and delegate to its
  :attr:`AcpAgentSession.update_stream` via :meth:`AcpSessionUpdateStream.subscribe`.
- Bind to that specific session only; when its stream closes, the subscription
  terminates. The method MUST NOT automatically move to a replacement session.
- Subscribing after a stream has already been closed is valid: callers receive
  the final retained snapshot (if any) and then observe a natural end of
  stream, not a :class:`StreamClosedError`.
- All ACP-aware consumers in this runtime MUST obtain ACP
  :class:`SessionUpdate` values via this API (or thin wrappers that delegate to
  it). There MUST NOT be a second canonical ACP subscription system.
- Convenience or compatibility helpers (for example, for existing
  :class:`AgentEvent` consumers) MAY exist, but they MUST delegate to
  :func:`subscribe_acp_updates` and MUST NOT define independent buffering or
  delivery semantics.

This API is the canonical subscription entrypoint for ACP `session/update`
traffic in the runtime. The underlying :class:`AcpSessionUpdateStream` and
:class:`ReceivedSessionUpdate` types are considered stable input for future
muxes and bridges.


------------------------------------------------------------------------

## 5. Implementation Phases

## Phase 1 — Add the typed session stream

Create `acp_types.py` and `acp_update_stream.py`.

Implement:

- `ReceivedSessionUpdate`;
- stream exceptions;
- bounded retained history;
- internally assigned sequence numbers;
- atomic replay/live subscription;
- bounded live subscriber queues;
- explicit overflow termination;
- persistent stream closure.

Do not modify the existing telemetry pipeline yet.

This phase should end with the stream independently usable and its core contract validated.

------------------------------------------------------------------------

## Phase 2 — Attach the stream to AcpAgentSession

Update:

```
src/nate_ntm/runtime/acp_client.py
```

Extend `AcpAgentSession` with a required stream field:

```
@dataclass(…)
class AcpAgentSession:
    …
    update_stream: AcpSessionUpdateStream
```

Create a new stream whenever a concrete ACP session is created.

When a session is stopped, fails, or is replaced:

1. close its update stream;
2. terminate or release its ACP connection;
3. remove or replace the session.

Do not retain the old stream on `AgentRuntimeState`.

The logical agent should expose only its current `AcpAgentSession`.

------------------------------------------------------------------------

## Phase 3 — Publish ACP updates directly into the session stream

Update:

```
src/nate_ntm/runtime/acp_protocol_client.py
src/nate_ntm/runtime/acp_client.py
```

Change `NateNtmAcpProtocolClient.session_update` so it forwards:

- agent identity needed to locate the session;
- ACP session identifier;
- the exact typed `SessionUpdate`;
- receipt timestamp.

It must not:

- construct generic envelope or telemetry objects;
- serialize the update;
- infer a generic string event type;
- assign the canonical session sequence.

In the ACP client, add one handler that:

1. locates the current `AcpAgentSession`;
2. verifies the callback belongs to that concrete session;
3. rejects or logs stale updates from replaced sessions;
4. publishes the exact update to `session.update_stream`.

The stream assigns the sequence number.

There should be one implementation path from `session_update()` to stream publication.

------------------------------------------------------------------------

## Phase 4 — Expose the typed subscription API

Add the typed ACP client API described in §4.3 to `BaseAcpClient`:

```
@asynccontextmanager
async def subscribe_acp_updates(
    self,
    agent_id: str,
) -> AsyncIterator[AsyncIterator[ReceivedSessionUpdate]]:
    ...
```

Implement this method to resolve the current `AcpAgentSession` for `agent_id`,
subscribe to that session's `update_stream`, and surface
`AgentSessionNotActive` when no active session exists, exactly as specified in
§4.3. This phase is about **wiring** the API into the concrete client
implementation, not redefining its contract.

Once all ACP subscription call sites have migrated, remove any remaining
ACP-specific subscription machinery (for example `_event_subscribers`,
`_EVENT_QUEUE_MAXSIZE`, `_EVENT_STREAM_CLOSED`, `subscribe_events`,
`iter_events`, `wait_for_event`) rather than keeping it in parallel with the
typed stream. These helpers may exist only in older specs or experimental
branches; they SHOULD NOT be (re)introduced as part of this epic.

Do not retain two canonical subscription systems. Convenience wrappers are
fine, but they MUST delegate to `subscribe_acp_updates()` and MUST NOT define
independent buffering or delivery semantics.

------------------------------------------------------------------------







## 6. Validation Strategy

Keep validation focused on a few end-to-end guarantees for the typed ACP session stream and its adapter-level subscription API. Avoid recreating the older generic event tests field by field; instead, assert the new stream contract directly.

### 6.1 Stream contract test

Add one focused test covering:

1. publish retained updates;
2. subscribe;
3. verify retained updates arrive in order;
4. publish live updates;
5. verify live updates follow retained history without gaps or duplicates;
6. close the stream;
7. verify the iterator terminates.

Use real representative ACP `SessionUpdate` models.

### 6.2 Replay/live race test

Add one concurrency test that repeatedly publishes while a subscriber attaches.

Assert that the subscriber observes a contiguous, ordered sequence with no duplicates at the replay/live boundary.

This is the most important concurrency test in the refactor.

### 6.3 Overflow and closure test

Add one test covering:

- a slow subscriber fills its bounded live queue;
- the subscriber terminates with `SubscriberOverflowError`;
- another subscriber continues receiving updates;
- publication after stream close raises `StreamClosedError`;
- subscribing after close replays the final retained history and terminates.

Existing tests should otherwise be updated only where behavior changed (for
example, where ACP updates are now observed via `subscribe_acp_updates`
rather than via any legacy generic event telemetry).

------------------------------------------------------------------------

## 7. Expected File Changes

Primary implementation files for this epic:

```
src/nate_ntm/runtime/acp_types.py
src/nate_ntm/runtime/acp_update_stream.py
src/nate_ntm/runtime/acp_client.py
src/nate_ntm/runtime/acp_protocol_client.py
src/nate_ntm/runtime/acp_connection.py
```

Key test modules:

```
tests/unit/runtime/test_acp_update_stream.py
tests/unit/runtime/test_acp_client_subscriptions.py
tests/integration/runtime_acp/test_runtime_daemon_acp_async_real_path_epic005.py
```

Other tests and runtime components may be updated as needed where they directly consume ACP session updates or rely on ACP adapter subscription behavior.

------------------------------------------------------------------------

## 8. Future Work

This document deliberately stops at `AcpSessionUpdateStream` and `subscribe_acp_updates()`. Later epics are expected to:

- introduce `SwarmACPMux` (or an equivalent component) that consumes `subscribe_acp_updates()` and forwards typed `SessionUpdate` values to external ACP connections;
- migrate downstream consumers (for example, runtime APIs or tooling) to depend
  on the typed stream rather than any legacy generic event envelopes;
- remove the remaining generic runtime event system (for example,
  `AgentEvent`, `AgentEventStream`, `/events` WebSocket streaming, and related
  JSON-RPC methods) once nothing depends on it;
- update documentation across the repo (for example, the runtime orchestrator spec and the Swarm/ACP mux spec) so they describe the typed ACP session update stream as the canonical path.

Those future changes should treat this epic's types and semantics as stable inputs rather than modifying the stream contract.


------------------------------------------------------------------------

## 9. Repo-Specific Implementation Notes and File Checklist

This section ties the abstract phases and invariants above to concrete files,
types, and call sites in the current `nate-ntm` repo. It is **non-normative**:
§§1–4 define the canonical types and behavior. If any example or description in
this section ever conflicts with those sections, treat §§1–4 as authoritative.
Its purpose is to give enough context that you can implement the refactor
without re-reading other design docs.

### 9.1 New types and stream primitives

**`src/nate_ntm/runtime/acp_types.py`**

Create a small module that owns the import of the real ACP `SessionUpdate` type:

```python
# src/nate_ntm/runtime/acp_types.py

from __future__ import annotations

"""ACP type aliases used by the runtime.

This module isolates the concrete ACP SDK import paths so that the rest of the
runtime can depend on a single, internal abstraction rather than importing
from :mod:`acp` directly.

In particular, ``SessionUpdate`` represents the typed ACP models delivered to
``Client.session_update`` callbacks. The exact type is provided by the ACP SDK
and may evolve over time; this module SHOULD be kept in sync with the
installed SDK version.
"""

from acp import schema as acp_schema

# NOTE:
# -----
# The ACP SDK used by this project does not currently expose a dedicated
# ``SessionUpdate`` union type. Instead, individual update models such as
# ``UserMessageChunk``, ``UsageUpdate``, and ``ToolCallStart`` all inherit
# from ``acp.schema.BaseModel``.
#
# We therefore treat ``BaseModel`` as the common supertype for all
# ``session/update`` payload models. This keeps the runtime strongly typed
# against ACP SDK models (rather than ``Any``) while remaining forward
# compatible with additional update variants.
SessionUpdate = acp_schema.BaseModel

__all__ = ["SessionUpdate"]
```

If a future ACP SDK release introduces a dedicated ``SessionUpdate`` union
(or moves the base model), update this module to re-export that precise union
or base type. All other runtime code should continue to import
``SessionUpdate`` from :mod:`nate_ntm.runtime.acp_types` rather than reaching
into the ACP SDK directly.

**`src/nate_ntm/runtime/acp_update_stream.py`**

Sections 1–4 define the required externally observable behavior for
`AcpSessionUpdateStream` and `ReceivedSessionUpdate` (types, sequence and
replay invariants, overflow semantics, closure behavior, and the
`subscribe_acp_updates()` adapter API).

Section 9 is **non-normative**: it provides a reference implementation that
illustrates one concrete way to satisfy those requirements in this repository.
Other implementations MAY differ internally as long as they satisfy the same
external behavior.

Define the typed receipt record and the per-session stream implementation:

```python
# src/nate_ntm/runtime/acp_update_stream.py

from __future__ import annotations

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
```

This is intentionally high-level pseudo-code with concrete method names, types, and behavior. When implementing, you should enforce:

- No silent loss of ACP updates.
- Explicit `SubscriberOverflowError` surfaced to the subscriber when its queue overflows (for example, by enqueuing a terminal exception sentinel as in the current implementation).
- Persistent closed state and deterministic behavior for publish/subscribe-after-close.

**Suggested focused unit tests:** `tests/unit/runtime/test_acp_update_stream.py`.

- Validate replay+live ordering.
- Validate overflow behavior (using artificially tiny `max_events`).
- Validate close semantics and subscribe-after-close semantics.

### 9.2 Where to attach the stream on AcpAgentSession

**File:** `src/nate_ntm/runtime/acp_client.py`

`AcpAgentSession` is the concrete per-agent ACP session record. Enrich it with a required typed stream:

```python
# src/nate_ntm/runtime/acp_client.py

from dataclasses import dataclass
from nate_ntm.runtime.acp_update_stream import AcpSessionUpdateStream


@dataclass(slots=True)
class AcpAgentSession:
    agent_id: str
    conversation_id: str
    process: subprocess.Popen[bytes] | None
    connection: BaseAcpConnection
    protocol_client: NateNtmAcpProtocolClient
    status: str = "starting"
    stderr_task: asyncio.Task[None] | None = None
    exit_monitor_task: asyncio.Task[None] | None = None

    # New: one update stream per concrete session.
    update_stream: AcpSessionUpdateStream = field(default_factory=AcpSessionUpdateStream)
```

Ensure that all session-creation paths (e.g. inside `BaseAcpClient` or its concrete subclasses) construct `AcpAgentSession` without overriding `update_stream`:

```python
session = AcpAgentSession(
    agent_id=agent_id,
    conversation_id=conversation_id,
    process=process,
    connection=connection,
    protocol_client=protocol_client,
)
```

On session shutdown, failure, or replacement, explicitly close the stream before discarding the session:

```python
session.update_stream.close(error=maybe_exception)
```

The runtime should not keep this stream on `AgentRuntimeState`; it is scoped to the concrete ACP session, not to the logical agent.

### 9.3 Protocol client callback wiring

**Files:**

- `src/nate_ntm/runtime/acp_protocol_client.py`
- `src/nate_ntm/runtime/acp_connection.py`

`NateNtmAcpProtocolClient` receives `session_update` callbacks from the ACP SDK.
Wire those callbacks into the owning `BaseAcpClient` via a typed sink that
forwards real `SessionUpdate` values into the owning `AcpAgentSession`'s
`AcpSessionUpdateStream`.

**Define a sink type alias:**

```python
# src/nate_ntm/runtime/acp_protocol_client.py

from datetime import datetime
from typing import Callable

from nate_ntm.runtime.acp_types import SessionUpdate

SessionUpdateSink = Callable[[str, str, SessionUpdate, datetime], None]
```

**Update the protocol client:**

```python
class NateNtmAcpProtocolClient(...):
    def __init__(
        self,
        agent_id: str,
        event_sink: SessionUpdateSink,
        clock: Callable[[], datetime] | None = None,
        ...,
    ) -> None:
        self._agent_id = agent_id
        self._event_sink = event_sink
        self._clock = clock or datetime.utcnow

    async def session_update(self, session_id: str, update: SessionUpdate, **_: object) -> None:
        # Forward the typed update into the sink; higher layers own publishing
        # into the session's AcpSessionUpdateStream.
        received_at = self._clock()
        self._event_sink(self._agent_id, session_id, update, received_at)
```

**In the ACP client (`acp_client.py`), provide the sink implementation:**

```python
from nate_ntm.runtime.acp_update_stream import (
    AcpSessionUpdateStream,
    AgentSessionNotActive,
    ReceivedSessionUpdate,
    StreamClosedError,
)
from nate_ntm.runtime.acp_types import SessionUpdate


class BaseAcpClient:
    ...

    def _on_session_update(
        self,
        agent_id: str,
        session_id: str,
        update: SessionUpdate,
        received_at: datetime,
    ) -> None:
        """Internal hook for typed ACP ``session/update`` notifications.

        This method is wired into :class:`NateNtmAcpProtocolClient` and is
        responsible for forwarding each typed :class:`SessionUpdate` into the
        owning :class:`AcpAgentSession`'s :class:`AcpSessionUpdateStream`.

        It SHOULD NOT drop updates silently and SHOULD avoid mutating
        :class:`SessionUpdate` instances; callers can rely on a faithful,
        ordered view of each session's ACP updates.
        """

        session = self._sessions.get(agent_id)
        if session is None:
            # Agent has no active session in this adapter.
            raise AgentSessionNotActive(
                f"Received ACP session update for inactive agent {agent_id!r}"
            )

        bound_session_id = (session.conversation_id or "").strip()
        if bound_session_id and bound_session_id != session_id:
            # Stale callback for a replaced session; log and drop.
            logger.warning(
                "acp_session_update_for_stale_session",
                extra={
                    "agent_id": agent_id,
                    "expected_session_id": bound_session_id,
                    "actual_session_id": session_id,
                },
            )
            return

        try:
            receipt = session.update_stream.publish(update, received_at=received_at)
        except StreamClosedError:
            # The stream has already been closed, typically because the session
            # is shutting down. Treat this as benign but log at debug level for
            # diagnostics.
            logger.debug(
                "acp_update_after_stream_closed",
                extra={"agent_id": agent_id, "session_id": session_id},
            )
            return
        except Exception as exc:  # pragma: no cover - defensive
            # Any unexpected failure when publishing to the stream is treated
            # as terminal for that stream so that subscribers observe a
            # consistent closure signal.
            session.update_stream.close(exc)
            logger.error(
                "acp_update_stream_publish_error",
                extra={"agent_id": agent_id, "session_id": session_id},
            )
            raise
```

Then, when constructing `NateNtmAcpProtocolClient` in `acp_connection.py` or within `BaseAcpClient` startup code, pass the bound `_on_session_update` method as `event_sink`.

### 9.4 Replacing the subscription API

**File:** `src/nate_ntm/runtime/acp_client.py`

The legacy ACP **subscription** helpers (`subscribe_events`, `iter_events`, `wait_for_event`, and any per-agent ACP event queues carried over from earlier designs) should migrate to a single typed subscription path built on `AcpSessionUpdateStream`. Runtime lifecycle producers such as `BaseAcpClient.on_event` and `_emit_event` remain available for non-ACP telemetry.

Suggested implementation (non-normative; see §4.3 for the canonical contract):

```python
from contextlib import asynccontextmanager
from typing import AsyncIterator

from nate_ntm.runtime.acp_update_stream import (
    AcpSessionUpdateStream,
    AgentSessionNotActive,
    ReceivedSessionUpdate,
)


class BaseAcpClient:
    ...

    @asynccontextmanager
    async def subscribe_acp_updates(
        self,
        agent_id: str,
    ) -> AsyncIterator[AsyncIterator[ReceivedSessionUpdate]]:
        """Subscribe to the typed ACP update stream for ``agent_id``.

        For full contract details (including error semantics for inactive
        sessions), see §4.3.
        """

        session = self._sessions.get(agent_id)
        if session is None or session.status not in {"starting", "running"}:
            raise AgentSessionNotActive(f"No active ACP session for agent {agent_id!r}")

        stream = session.update_stream
        async with stream.subscribe() as updates:
            yield updates
```

Update tests that consume ACP session updates to use this API instead of the old `subscribe_events`/`iter_events` helpers. Future SwarmACPMux implementations should also attach via `subscribe_acp_updates()`.

