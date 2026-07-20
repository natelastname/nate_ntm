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

This section maps the normative design in §§1–4 onto the current `nate-ntm` repository.

It is non-normative. Sections 1–4 define the required types, ownership rules, subscription contract, and externally observable behavior. This section identifies the files and call sites that must implement that contract without duplicating the production implementation.

### 9.1 Typed ACP update primitives

#### src/nate_ntm/runtime/acp_types.py

This module owns the runtime's internal `SessionUpdate` type alias.

Responsibilities:

- expose the narrowest authoritative ACP SDK type that covers `session/update` payloads;
- isolate the ACP SDK import path;
- prevent runtime modules from independently defining or importing competing update types;
- contain no serialization, normalization, buffering, or delivery logic.

Current expected shape:

```
from acp import schema as acp_schema

SessionUpdate = acp_schema.BaseModel

__all__ = ["SessionUpdate”]
```

`acp_schema.BaseModel` is a pragmatic upper bound because the currently installed ACP SDK does not expose a narrower common `SessionUpdate` union or base class. If the SDK introduces one, this alias must be updated to use it.

#### src/nate_ntm/runtime/acp_update_stream.py

This module owns the typed, per-session ACP update stream.

It must define:

```
@dataclass(frozen=True, slots=True)
class ReceivedSessionUpdate:
    sequence: int
    received_at: datetime
    update: SessionUpdate
```

```
class AcpUpdateStreamError(RuntimeError):
    …
```

```
class StreamClosedError(AcpUpdateStreamError):
    “""Raised when publishing to a closed stream.”""
```

```
class SubscriberOverflowError(AcpUpdateStreamError):
    “""Raised when a subscriber's live queue exceeds its capacity.”""
```

```
class AgentSessionNotActive(AcpUpdateStreamError):
    “""Raised when an active ACP session cannot be resolved for attachment.”""
```

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

The implementation must satisfy the invariants in §§3.3–3.7:

- one monotonically increasing sequence space per stream;
- bounded retained history;
- an atomic replay/live boundary;
- one bounded live queue per subscriber;
- explicit subscriber-local overflow failure;
- no silent update loss;
- persistent and idempotent closure;
- replay-after-close followed by natural termination;
- deterministic subscriber cleanup.

The module description should characterize the stream as the canonical typed, per-session ACP update path. It must not claim that Epic 008 removes the complete generic `AgentEvent` telemetry system or that a mux already consumes the stream.

### 9.2 Attach the stream to AcpAgentSession

#### src/nate_ntm/runtime/acp_client.py

`AcpAgentSession` owns the update stream for one concrete ACP session.

Expected shape:

```
@dataclass(slots=True)
class AcpAgentSession:
    agent_id: str
    conversation_id: str
    process: subprocess.Popen[bytes] | None
    connection: BaseAcpConnection
    protocol_client: NateNtmAcpProtocolClient
    update_stream: AcpSessionUpdateStream = field(
        default_factory=AcpSessionUpdateStream
    )
    status: str = “starting"
    stderr_task: asyncio.Task[None] | None = None
    exit_monitor_task: asyncio.Task[None] | None = None
```

Every session-creation path must allocate a fresh stream.

Every stop, failure, or replacement path must:

1. retain the concrete session being terminated;
2. close that session's update stream;
3. release the ACP connection and process resources;
4. remove or replace the session record.

The stream must not be stored on `AgentRuntimeState` or shared by multiple `AcpAgentSession` instances.

### 9.3 Publish ACP callbacks into the owning session stream

#### src/nate_ntm/runtime/acp_protocol_client.py

`NateNtmAcpProtocolClient.session_update()` receives the typed callback from the ACP SDK.

Define one sink contract:

```
SessionUpdateSink = Callable[
    [str, str, SessionUpdate, datetime],
    None,
]
```

The callback must forward:

- the logical agent identifier;
- the concrete ACP session identifier;
- the exact `SessionUpdate` object;
- the receipt timestamp.

It must not:

- serialize or normalize the update;
- construct a generic event envelope;
- infer a string event type;
- assign the stream sequence number.

#### src/nate_ntm/runtime/acp_client.py

`BaseAcpClient` must provide one publication handler:

```
def _on_session_update(
    self,
    agent_id: str,
    session_id: str,
    update: SessionUpdate,
    received_at: datetime,
) -> None:
    …
```

That handler must:

1. resolve the current `AcpAgentSession` for `agent_id`;
2. reject the callback if no active session can be resolved;
3. verify that the callback belongs to that concrete session;
4. reject or log callbacks from replaced sessions;
5. publish the exact update into `session.update_stream`.

The stream, not the protocol client or handler, assigns the canonical sequence number.

There must be one ACP publication path:

```
NateNtmAcpProtocolClient.session_update()
        ↓
BaseAcpClient._on_session_update()
        ↓
AcpAgentSession.update_stream.publish()
```

No second ACP buffer, subscriber registry, or publication path may remain.

### 9.4 Expose the canonical subscription API

#### src/nate_ntm/runtime/acp_client.py

Implement the API defined in §4.3:

```
@asynccontextmanager
async def subscribe_acp_updates(
    self,
    agent_id: str,
) -> AsyncIterator[AsyncIterator[ReceivedSessionUpdate]]:
    …
```

The implementation must:

1. resolve the current session exactly once;
2. accept only sessions whose status is `"starting"` or `"running"`;
3. raise `AgentSessionNotActive` for missing or inactive sessions;
4. capture the concrete session;
5. delegate to `session.update_stream.subscribe()`;
6. remain attached to that session until its stream terminates;
7. never follow automatically to a replacement session.

Subscribing to an already-closed captured stream is valid. The subscriber receives its final retained snapshot and then terminates naturally.

All ACP-aware consumers must enter through `subscribe_acp_updates()`.

A transitional compatibility adapter may consume this API and translate updates outward for an existing non-ACP telemetry consumer. Such an adapter must not introduce:

- another ACP source;
- another subscriber registry;
- another replay buffer;
- another overflow policy;
- another ACP subscription API;
- independent delivery semantics.

Legacy ACP-specific helpers such as `subscribe_events()`, `iter_events()`, or `wait_for_event()` must either be removed or reduced to thin delegation without their own buffering or subscription machinery.

### 9.5 Connection wiring

#### src/nate_ntm/runtime/acp_connection.py

Wherever `NateNtmAcpProtocolClient` is constructed, pass the owning client's `_on_session_update` method as the `SessionUpdateSink`.

The connection layer must not independently retain, translate, or fan out ACP updates.

Its responsibility ends after wiring the SDK callback to the runtime publication handler.

### 9.6 Validation checklist

The implementation is complete when the following focused tests pass.

#### Stream contract

- retained updates replay in sequence order;
- live updates follow retained history without gaps or duplicates;
- sequence numbers begin at 1 and are local to one stream;
- multiple subscribers receive independent ordered views.

#### Replay/live boundary

- publication racing with subscription produces one contiguous sequence;
- no update appears in both replay and live delivery;
- no update disappears at the registration boundary.

#### Overflow and closure

- a slow subscriber receives `SubscriberOverflowError`;
- overflow affects only that subscriber;
- another subscriber continues normally;
- publishing after closure raises `StreamClosedError`;
- subscribers already attached drain queued updates and terminate;
- subscribers attaching after closure replay retained history and terminate;
- cancellation unregisters the subscriber.

#### Adapter integration

- `subscribe_acp_updates()` raises `AgentSessionNotActive` for missing sessions;
- it raises the same error for inactive sessions;
- it binds to one concrete session;
- it does not follow a replacement session;
- ACP SDK callbacks reach the owning session stream exactly once;
- stale callbacks do not enter a replacement session's stream.

Use real representative ACP SDK update models rather than generic dictionaries or mocks of the stream contract.

### 9.7 File checklist

Primary implementation files:

```
src/nate_ntm/runtime/acp_types.py
src/nate_ntm/runtime/acp_update_stream.py
src/nate_ntm/runtime/acp_client.py
src/nate_ntm/runtime/acp_protocol_client.py
src/nate_ntm/runtime/acp_connection.py
```

Primary tests:

```
tests/unit/runtime/test_acp_update_stream.py
tests/unit/runtime/test_acp_client_subscriptions.py
tests/integration/runtime_acp/test_runtime_daemon_acp_async_real_path_epic005.py
```

Additional files should change only where they directly construct an `AcpAgentSession`, receive ACP `session/update` callbacks, or consume ACP updates.

Do not add a generalized event bus, a second stream implementation, or mux behavior in this epic.
