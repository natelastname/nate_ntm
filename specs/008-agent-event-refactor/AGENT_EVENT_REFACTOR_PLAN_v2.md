# Agent Event Refactor — Implementation Plan

## Purpose

Replace the generic runtime `AgentEvent` telemetry system with one typed ACP update stream owned by each concrete ACP session.

    This is an intentional breaking cleanup. Do not preserve the old event API through compatibility adapters, parallel event models, or reconstructed telemetry envelopes.

The final architecture should have one representation and one delivery path for ACP updates:

```
ACP SDK SessionUpdate
        ↓
NateNtmAcpProtocolClient.session_update
        ↓
current AcpAgentSession
        ↓
bounded session-owned update stream
        ↓
SwarmACPMux
        ↓
external ACP connection
```

The exact typed `SessionUpdate` received from ACP must remain intact throughout this path. Serialization is the responsibility of the ACP SDK at the external connection boundary.

Previous Epic 001 and Epic 008 specifications describe older decisions. They do not block implementation. Update them after the code reflects the new design.

------------------------------------------------------------------------

## 1. Required End State

At completion:

1. Every active `AcpAgentSession` owns one bounded, in-memory ACP update stream.
2. Every ACP `SessionUpdate` received for that session is published exactly once.
3. Subscribers receive:
  - retained updates in sequence order;
    - then live updates without a gap, duplicate, or reordering at the replay boundary.
4. Subscriber overflow never silently drops an ACP update.
5. A subscriber whose bounded live queue overflows is terminated with an explicit error.
6. Closing or replacing an ACP session closes its stream and terminates its subscribers.
7. A new ACP session receives a new stream and a new sequence space.
8. `SwarmACPMux` consumes the session stream but owns no replay history.
9. The generic `AgentEvent`, `AgentEventSource`, and `AgentEventStream` telemetry layer is removed.
10. Runtime lifecycle and Agent Mail information remain represented by their existing state models and logs.
11. The runtime control API no longer exposes generic event history or generic event subscriptions.
12. No replacement generic runtime event channel is introduced.

------------------------------------------------------------------------

## 2. Core Design Decisions

### 2.1 The stream belongs to a concrete ACP session

The stream belongs to `AcpAgentSession`, not to the logical agent.

```
logical agent
    └── current AcpAgentSession
            └── AcpSessionUpdateStream
```

A restart, replacement, or newly established ACP session creates a new stream.

The old stream closes before the old session is discarded.

A mux attachment targets one concrete ACP session. It does not silently follow the logical agent into a replacement session. When the session closes, the attachment ends. Reattaching to a replacement session is an explicit higher-level operation.

### 2.2 Keep the real ACP type

Use the actual ACP SDK `SessionUpdate` type or a precise internal union of the SDK update models.

Do not define:

```
SessionUpdate = Any
```

If a stable SDK alias already exists, import and re-export it from one internal module. If not, define an explicit union there.

Example shape:

```
# src/nate_ntm/runtime/acp_types.py

from acp import SessionUpdate

__all__ = ["SessionUpdate”]
```

The exact import should match the installed ACP SDK.

### 2.3 Use a small receipt record

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

### 2.4 The stream assigns sequence numbers

Callers publish typed updates, not preconstructed records:

```
stream.publish(update, received_at=clock())
```

The stream assigns the next sequence number and constructs `ReceivedSessionUpdate`.

This creates one canonical sequencing path and prevents callers from supplying duplicate or out-of-order sequence values.

### 2.5 Replay and live delivery must have an atomic boundary

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

### 2.6 Overflow is explicit failure

Each subscriber has a bounded live queue.

When publishing a live update:

- if the queue has capacity, enqueue the update;
- if the queue is full, close that subscriber with a `SubscriberOverflowError`;
- never remove an older update to make room;
- never silently skip the new update;
- do not block unrelated subscribers.

An overflowing subscriber is no longer capable of preserving the ACP stream contract, so terminating it is safer than continuing with hidden data loss.

The retained session history remains bounded independently of subscriber queues.

### 2.7 Closure is persistent

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

### 2.8 Missing sessions are explicit errors

Attempting to subscribe to an agent that has no active ACP session should raise a clear exception such as:

```
AgentSessionNotActive
```

Do not return an empty iterator. An empty iterator would make an invalid attachment indistinguishable from a valid stream that ended normally.

------------------------------------------------------------------------

## 3. New Runtime Types

### 3.1 ACP type module

Create:

```
src/nate_ntm/runtime/acp_types.py
```

Responsibilities:

- expose the concrete `SessionUpdate` type used by the runtime;
- isolate the exact ACP SDK import path;
- contain no serialization or payload-normalization logic.

### 3.2 Session update stream module

Create:

```
src/nate_ntm/runtime/acp_update_stream.py
```

Define:

```
@dataclass(frozen=True, slots=True)
class ReceivedSessionUpdate:
    sequence: int
    received_at: datetime
    update: SessionUpdate
```

Define exceptions:

```
class AcpUpdateStreamError(RuntimeError):
    pass

class StreamClosedError(AcpUpdateStreamError):
    pass

class SubscriberOverflowError(AcpUpdateStreamError):
    pass

class AgentSessionNotActive(AcpUpdateStreamError):
    pass
```

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

------------------------------------------------------------------------

## 4. Implementation Phases

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

- construct `AgentEvent`;
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

Add a typed ACP client API:

```
@asynccontextmanager
async def subscribe_acp_updates(
    self,
    agent_id: str,
) -> AsyncIterator[AsyncIterator[ReceivedSessionUpdate]]:
    …
```

Behavior:

- locate the agent's current `AcpAgentSession`;
- raise `AgentSessionNotActive` if no active session exists;
- capture that concrete session;
- delegate to its `update_stream.subscribe()`;
- remain attached only to that session;
- terminate when that session stream closes.

Do not make this method automatically move to a replacement session.

Remove the old ACP-client subscriber mechanism after all consumers have migrated:

- `_event_subscribers`;
- `_EVENT_QUEUE_MAXSIZE`;
- `_EVENT_STREAM_CLOSED`;
- `_emit_event`;
- `subscribe_events`;
- `iter_events`;
- `wait_for_event`;
- `on_event`.

Do not retain both subscription systems.

------------------------------------------------------------------------

## Phase 5 — Wire SwarmACPMux to the typed stream

Update the mux so its forwarding loop is conceptually:

```
async with acp_client.subscribe_acp_updates(agent_id) as events:
    async for event in events:
        await downstream.session_update(
            session_id=mux_session_id,
            update=event.update,
        )
```

Requirements:

- the mux forwards the exact `SessionUpdate`;
- ACP SDK serialization happens only at the downstream ACP connection;
- the mux owns no replay buffer;
- the mux owns no generic event conversion;
- the mux does not reconstruct updates from dictionaries;
- session closure terminates the forwarding attachment;
- subscriber overflow surfaces as an attachment failure rather than hidden event loss.

Delete any mux-side replay mechanism made obsolete by this stream.

------------------------------------------------------------------------

## Phase 6 — Delete generic event telemetry

After ACP consumers use the typed stream, remove the old system completely.

Delete from:

```
src/nate_ntm/runtime/events.py
```

- `AgentEventSource`;
- `AgentEvent`;
- `AgentEventStream`.

Delete:

```
src/nate_ntm/runtime/acp_event_translation.py
tests/unit/runtime/test_acp_event_translation.py
```

Remove:

- `translate_acp_update`;
- `_model_to_payload`;
- `_update_kind`;
- reconstruction of `SessionUpdate` from `AgentEvent.payload`;
- string types such as `acp.agent_message_chunk`;
- generic payload dictionaries.

Do not repurpose `runtime/events.py` merely to preserve its import path. Delete it unless it still contains unrelated, genuinely useful code.

------------------------------------------------------------------------

## Phase 7 — Remove non-ACP event producers

Update:

```
src/nate_ntm/runtime/agents.py
src/nate_ntm/runtime/state.py
src/nate_ntm/runtime/daemon.py
```

Remove lifecycle, Agent Mail, process, client, and runtime telemetry producers that construct `AgentEvent`.


------------------------------------------------------------------------

## 9. Repo-Specific Implementation Notes and File Checklist

This section ties the abstract phases and invariants above to concrete files, types, and call sites in the current `nate-ntm` repo. It is meant to be enough context that you can implement the refactor without re-reading other design docs.

### 9.1 New types and stream primitives

**`src/nate_ntm/runtime/acp_types.py`**

Create a small module that owns the import of the real ACP `SessionUpdate` type:

```python
# src/nate_ntm/runtime/acp_types.py

from __future__ import annotations

# Adjust this import to match the real ACP SDK location.
# Example only:
# from acp import SessionUpdate
from acp import SessionUpdate  # TODO: confirm SDK path

__all__ = ["SessionUpdate"]
```

If the actual SDK exposes the type from a different module (for example, `from acp.session import SessionUpdate` or `from acp.models import SessionUpdate`), update the import accordingly. The key requirement is that this module re-exports the precise union or base type that represents all ACP session updates you receive in `session_update()`.

**`src/nate_ntm/runtime/acp_update_stream.py`**

Define the typed receipt record and the per-session stream implementation:

```python
# src/nate_ntm/runtime/acp_update_stream.py

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator, Deque, List, Optional

from collections import deque

from .acp_types import SessionUpdate


@dataclass(frozen=True, slots=True)
class ReceivedSessionUpdate:
    sequence: int
    received_at: datetime
    update: SessionUpdate


class AcpUpdateStreamError(RuntimeError):
    pass


class StreamClosedError(AcpUpdateStreamError):
    pass


class SubscriberOverflowError(AcpUpdateStreamError):
    pass


class AgentSessionNotActive(AcpUpdateStreamError):
    pass


@dataclass(slots=True)
class AcpSessionUpdateStream:
    max_events: int = 200

    _events: Deque[ReceivedSessionUpdate] = field(default_factory=lambda: deque(maxlen=200), init=False, repr=False)
    _next_sequence: int = field(default=1, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _close_error: BaseException | None = field(default=None, init=False, repr=False)

    # Live subscribers: each has a bounded queue of ReceivedSessionUpdate
    _subscribers: set[asyncio.Queue[ReceivedSessionUpdate]] = field(default_factory=set, init=False, repr=False)

    def publish(self, update: SessionUpdate, *, received_at: datetime) -> ReceivedSessionUpdate:
        if self._closed:
            raise StreamClosedError("cannot publish to closed AcpSessionUpdateStream")

        event = ReceivedSessionUpdate(
            sequence=self._next_sequence,
            received_at=received_at,
            update=update,
        )
        self._next_sequence += 1

        # Append to bounded history.
        self._events.append(event)

        # Fan out to subscribers; overflow → terminate that subscriber.
        dead: list[asyncio.Queue[ReceivedSessionUpdate]] = []
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)
            # Let subscriber code detect termination via an exception or sentinel.

        return event

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[AsyncIterator[ReceivedSessionUpdate]]:
        # Snapshot history at subscription time.
        snapshot = list(self._events)

        # If already closed, deliver snapshot only.
        live_queue: asyncio.Queue[ReceivedSessionUpdate] | None
        if self._closed:
            live_queue = None
        else:
            live_queue = asyncio.Queue[ReceivedSessionUpdate](maxsize=self.max_events)
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
                    ev = await live_queue.get()
                    yield ev
            finally:
                self._subscribers.discard(live_queue)

        try:
            yield _iterator()
        finally:
            # No additional action needed here; stream lifetime is managed by close().
            pass

    def close(self, error: BaseException | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_error = error
        # Current subscribers will drain any queued events and then stop when their
        # producer stops calling publish(). New subscribers will see only the
        # retained snapshot.
```

This is intentionally high-level pseudo-code with concrete method names, types, and behavior. When implementing, you should enforce:

- No silent loss of ACP updates.
- Explicit `SubscriberOverflowError` on subscriber queue overflow (you can revise `publish` to raise on overflow rather than dropping subscribers, depending on how strictly you want to enforce failure).
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

`NateNtmAcpProtocolClient` currently receives `session_update` callbacks from the ACP SDK and translates them into `AgentEvent`. Replace that with a typed callback that forwards real `SessionUpdate` values into the owning `BaseAcpClient`.

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
        # Do not construct AgentEvent here.
        received_at = self._clock()
        self._event_sink(self._agent_id, session_id, update, received_at)
```

**In the ACP client (`acp_client.py`), provide the sink implementation:**

```python
from nate_ntm.runtime.acp_update_stream import AcpSessionUpdateStream, ReceivedSessionUpdate
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
        session = self._sessions.get(agent_id)
        if session is None:
            # Unknown agent; log and drop.
            return
        if session.conversation_id != session_id:
            # Stale callback for a replaced session; log and drop.
            return

        session.update_stream.publish(update, received_at=received_at)
```

Then, when constructing `NateNtmAcpProtocolClient` in `acp_connection.py` or within `BaseAcpClient` startup code, pass the bound `_on_session_update` method as `event_sink`.


### 9.4 Replacing the subscription API

**File:** `src/nate_ntm/runtime/acp_client.py`

The legacy subscription APIs (`subscribe_events`, `iter_events`, `wait_for_event`, `_emit_event`, `_EVENT_QUEUE_MAXSIZE`, `_EVENT_STREAM_CLOSED`, `on_event`) must be replaced by a single typed subscription path built on `AcpSessionUpdateStream`.

Suggested implementation:

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
        session = self._sessions.get(agent_id)
        if session is None:
            raise AgentSessionNotActive(f"no active ACP session for agent {agent_id!r}")

        stream = session.update_stream
        async with stream.subscribe() as updates:
            yield updates
```

Update tests and later SwarmACPMux to consume this API instead of the old `subscribe_events`/`iter_events` functions.


### 9.5 Removing generic telemetry (where to edit)

The following modules currently participate in the generic `AgentEvent` telemetry system and will need updates or deletion:

- `src/nate_ntm/runtime/events.py`
  - Defines `AgentEvent`, `AgentEventSource`, `AgentEventStream`.
- `src/nate_ntm/runtime/state.py`
  - `AgentRuntimeState.event_stream` references `AgentEventStream`.
- `src/nate_ntm/runtime/agents.py`
  - `AgentSupervisor.append_agent_event`, `_append_runtime_event`, `record_unread_mail` produce non-ACP `AgentEvent`s.
  - `on_agent_event` callback.
- `src/nate_ntm/runtime/daemon.py`
  - `get_agent_detail` includes `events` derived from `AgentEventStream`.
  - `create`/`resume` wire `acp_client.on_event = agent_supervisor.append_agent_event`.
- `src/nate_ntm/runtime/acp_event_translation.py`
  - Converts ACP updates to `AgentEvent`.
- `src/nate_ntm/runtime/acp_client.py`
  - Legacy subscription machinery around `AgentEvent` and `on_event`.
- `src/nate_ntm/api/runtime_api.py`
  - `/events` WebSocket endpoint and subscription maps.
  - `publish_event` bridge.
- `src/nate_ntm/api/jsonrpc.py`
  - `build_events_notify_messages` and `events.notify` payloads.
- `src/nate_ntm/api/server.py`
  - JSON-RPC methods for `events.subscribe` / `events.unsubscribe`.
- `src/nate_ntm/runtime/runner.py`
  - Bridges `on_agent_event` into `app.state.publish_event`.

When implementing Phases 6 and 7, use a grep pass to ensure all usage is removed:

```bash
rg "AgentEvent" -n
rg "AgentEventStream" -n
rg "AgentEventSource" -n
rg "events.notify" -n
rg "/events" -n src/nate_ntm
rg "translate_acp_update" -n
rg "subscribe_events" -n
```


### 9.6 Tests to update or remove

This list is not exhaustive but covers the main suites that depend on the old telemetry/event model:

- `tests/unit/runtime/test_events.py`
  - Validates `AgentEvent` and `AgentEventStream` behavior.
  - Should be replaced with tests for `AcpSessionUpdateStream` (see 9.1) or removed.
- `tests/unit/runtime/test_acp_event_translation.py`
  - Validates `translate_acp_update`; delete once translation is removed.
- `tests/unit/runtime/test_agents.py`
  - Asserts on runtime and Agent Mail `AgentEvent`s.
  - Rewrite to assert on `RuntimeState` / `AgentRuntimeState` and mail state/logging instead.
- `tests/unit/runtime/test_scheduler.py`
  - Asserts `MailReceived` events; move to state-based assertions.
- `tests/integration/runtime_acp/test_runtime_daemon_acp_async_real_path_epic005.py`
  - Consumes `AgentEvent` payloads.
  - Update to subscribe via `subscribe_acp_updates` and assert on `ReceivedSessionUpdate.update`.
- `tests/integration/quickstart/test_runtime_ws_events_us3.py`
  - Exercises `/events` WebSocket streaming; delete or rewrite after removing event APIs.
- `tests/unit/api/test_jsonrpc.py`
  - Tests `build_events_notify_messages` and `events.notify`; delete those tests.
- `tests/unit/api/test_server.py`
  - Tests event-related JSON-RPC methods; update to reflect the absence of `events.*` methods.
- `tests/unit/runtime/test_runner.py`
  - Validates the runtime → WebSocket event bridge; delete or refocus on non-event wiring.

After each major phase (especially Phases 3–7), re-run at least:

```bash
pytest tests/unit/runtime tests/integration/runtime_acp
pytest tests/unit/api tests/integration/quickstart
```

and fix references to removed types and APIs until the suite passes with only the new typed session stream.


Specifically remove or simplify:

- `AgentSupervisor.on_agent_event`;
- `_get_or_create_event_stream`;
- `_append_runtime_event`;
- `append_agent_event`;
- `AgentRuntimeState.event_stream`;
- wiring from ACP clients into `AgentSupervisor.append_agent_event`.

Keep runtime truth in actual state:

- agent status;
- failure information;
- restart state;
- conversation or session identifiers;
- unread Agent Mail state;
- scheduler state.

Use existing logging for operational diagnostics.

Do not introduce a replacement `RuntimeEvent`, `TelemetryEvent`, or second generic stream.

------------------------------------------------------------------------

## Phase 8 — Remove generic event APIs

Update:

```
src/nate_ntm/api/server.py
src/nate_ntm/api/jsonrpc.py
src/nate_ntm/api/runtime_api.py
src/nate_ntm/runtime/runner.py
src/nate_ntm/runtime/daemon.py
```

Remove:

- `events.subscribe`;
- `events.unsubscribe`;
- `events.notify`;
- `/events` WebSocket behavior;
- subscription registries;
- event publication bridges;
- `publish_event`;
- `build_events_notify_messages`;
- event lists from `agent.get_detail`.

`agent.get_detail` should return agent state only.

Do not expose the ACP update stream through a replacement JSON API unless a concrete non-ACP consumer requires it. The mux consumes the typed stream internally, and the ACP SDK owns wire serialization.

Delete or rewrite tests that exist solely to validate the removed generic event API.

------------------------------------------------------------------------

## 5. Validation Strategy

Keep validation focused on a few end-to-end stream guarantees. Do not reproduce the deleted telemetry tests field by field.

### 5.1 Stream contract test

Add one focused test covering:

1. publish retained updates;
2. subscribe;
3. verify retained updates arrive in order;
4. publish live updates;
5. verify live updates follow retained history without gaps or duplicates;
6. close the stream;
7. verify the iterator terminates.

Use real representative ACP `SessionUpdate` models.

### 5.2 Replay/live race test

Add one concurrency test that repeatedly publishes while a subscriber attaches.

Assert that the subscriber observes a contiguous, ordered sequence with no duplicates at the replay/live boundary.

This is the most important concurrency test in the refactor.

### 5.3 Overflow and closure test

Add one test covering:

- a slow subscriber fills its bounded live queue;
- the subscriber terminates with `SubscriberOverflowError`;
- another subscriber continues receiving updates;
- publication after stream close raises `StreamClosedError`;
- subscribing after close replays the final retained history and terminates.

### 5.4 Mux integration test

Add one integration-level test covering:

- retained updates are forwarded first;
- live updates are forwarded afterward;
- the exact typed updates reach the downstream ACP path once;
- closing the source session terminates the mux attachment;
- the mux contains no independent replay history.

Existing tests should otherwise be updated only where behavior changed. Remove tests for deleted telemetry structures rather than recreating equivalent assertions against every new internal detail.

------------------------------------------------------------------------

## 6. Expected File Changes

Primary implementation files:

```
src/nate_ntm/runtime/acp_types.py
src/nate_ntm/runtime/acp_update_stream.py
src/nate_ntm/runtime/acp_client.py
src/nate_ntm/runtime/acp_protocol_client.py
src/nate_ntm/runtime/acp_connection.py
src/nate_ntm/runtime/agents.py
src/nate_ntm/runtime/state.py
src/nate_ntm/runtime/daemon.py
src/nate_ntm/runtime/runner.py
src/nate_ntm/api/server.py
src/nate_ntm/api/jsonrpc.py
src/nate_ntm/api/runtime_api.py
```

Expected deletions:

```
src/nate_ntm/runtime/events.py
src/nate_ntm/runtime/acp_event_translation.py
tests/unit/runtime/test_events.py
tests/unit/runtime/test_acp_event_translation.py
tests/integration/quickstart/test_runtime_ws_events_us3.py
```

Some listed tests may instead be reduced or repurposed if they also cover behavior that remains relevant.

Update other imports and tests found by searching for:

```
AgentEvent
AgentEventSource
AgentEventStream
translate_acp_update
subscribe_events
events.subscribe
events.notify
/events
on_agent_event
append_agent_event
```

Avoid preserving unused files or forwarding aliases solely to reduce the size of the diff.

------------------------------------------------------------------------

## 7. Documentation Updateas

After implementation and validation, update:

```
AGENT_EVENT_REFACTOR.md
AGENT_EVENT_REFACTOR_PLAN.md
specs/001-swarm-runtime-orchestrator/
specs/008-swarm-acp-mux/
```

Epic 001 updates should:

- remove the generic `AgentEvent` contract;
- remove event history from `agent.get_detail`;
- remove generic event subscription and notification APIs;
- describe lifecycle and Agent Mail information as state.

Epic 008 updates should:

- define the concrete session-owned typed stream;
- remove the separate telemetry path;
- remove `require_session_update`;
- show the mux subscribing directly to the current concrete session;
- state that mux attachments end when that session closes;
- state that the mux owns no replay history.

Update other specifications that reference the deleted runtime event API, but do not let documentation cleanup delay the implementation.

# Agent Event Refactor — Phased Checklist

## Phase 1 — Establish the typed session stream

**Goal:** Introduce the new ACP-native event model without changing existing runtime behavior.

- [x] Add a central import or precise union for the real ACP `SessionUpdate` type.
- [x] Add `ReceivedSessionUpdate` with:

  - session-local `sequence`;
  - `received_at`;
  - exact typed `update`.

- [ ] Add `AcpSessionUpdateStream` with:

  - bounded retained history;
  - internally assigned sequence numbers;
  - atomic replay-then-live subscriptions;
  - bounded live queues per subscriber;
  - explicit `SubscriberOverflowError`;
  - persistent close state.

- [x] Define deterministic behavior for:

  - publish after close;
  - subscribe after close;
  - subscriber cancellation;
  - repeated close calls.

- [ ] Add focused tests for:

  - replay followed by live updates;
  - replay/live attachment race;
  - overflow and closure behavior.

**Phase complete when:** the new stream independently guarantees ordered, gap-free delivery and never silently drops an ACP update.

------------------------------------------------------------------------

## Phase 2 — Make the stream session-owned

**Goal:** Give each concrete `AcpAgentSession` exactly one update stream.

- [x] Add `update_stream` to `AcpAgentSession`.
- [x] Create a fresh stream whenever a concrete ACP session is created.
- [x] Close the stream when its session:

  - stops;
  - fails;
  - is replaced.

- [x] Ensure old streams are not retained on logical-agent runtime state.
- [x] Define session attachment semantics:

  - subscriptions target one concrete session;
  - replacement closes the old subscription;
  - subscriptions do not automatically follow a replacement session.

**Phase complete when:** stream lifetime and ACP session lifetime are identical.

------------------------------------------------------------------------

## Phase 3 — Publish real ACP updates into the stream

**Goal:** Replace ACP-to-telemetry translation with direct typed publication.

- [x] Update `NateNtmAcpProtocolClient.session_update` to forward:

  - agent identity;
  - ACP session identity;
  - exact `SessionUpdate`;
  - receipt timestamp.

- [ ] Add one ACP-client handler that:

  - locates the current concrete session;
  - rejects or logs stale-session updates;
  - publishes into that session's stream.

- [x] Let the stream assign the canonical sequence number.
- [x] Remove sequencing, serialization, and generic event construction from the protocol callback.
- [x] Verify representative ACP update types remain intact through publication.

**Phase complete when:** every ACP callback follows one direct path into the current session's typed stream.

------------------------------------------------------------------------

## Phase 4 — Replace the subscription API

**Goal:** Expose one typed subscription path for ACP consumers.

- [x] Add `subscribe_acp_updates(agent_id)`.
- [x] Raise `AgentSessionNotActive` when no active session exists.
- [x] Bind each subscription to the concrete session found at attachment time.
- [x] Delegate replay, live delivery, overflow, and closure to the session stream.
- [ ] Migrate existing ACP consumers to the new API.
- [ ] Remove the old generic ACP subscription machinery:

  - subscriber mappings;
  - sentinels;
  - `_emit_event`;
  - `subscribe_events`;
  - `iter_events`;
  - `wait_for_event`;
  - `on_event`.

**Phase complete when:** there is exactly one ACP subscription implementation.

------------------------------------------------------------------------

## Phase 5 — Wire the mux

**Goal:** Make `SwarmACPMux` a thin consumer of the session-owned stream.

- [ ] Subscribe through `subscribe_acp_updates`.
- [ ] Forward `event.update` directly to the downstream ACP connection.
- [ ] Let the ACP SDK perform wire serialization.
- [ ] Remove any mux-owned replay buffer or event reconstruction.
- [ ] Surface subscriber overflow as an attachment failure.
- [ ] End the mux attachment when the source session closes.
- [ ] Add one integration test covering:

  - retained updates;
  - live updates;
  - exact-once forwarding;
  - source-session closure.

**Phase complete when:** the mux owns forwarding only, not history or event conversion.

------------------------------------------------------------------------

## Phase 6 — Remove generic telemetry events

**Goal:** Delete the obsolete event abstraction after all ACP consumers have migrated.

- [ ] Delete:

  - `AgentEvent`;
  - `AgentEventSource`;
  - `AgentEventStream`.

- [ ] Delete ACP translation and reconstruction code:

  - `translate_acp_update`;
  - `_model_to_payload`;
  - `_update_kind`;
  - payload-to-`SessionUpdate` conversion.

- [ ] Remove lifecycle, process, Agent Mail, and client code that creates generic events.
- [ ] Remove event-stream fields and callbacks from runtime and supervisor state.
- [ ] Replace affected assertions with checks against:

  - runtime state;
  - Agent Mail state;
  - relevant logs.

- [ ] Delete tests that only validate the removed telemetry envelope.

**Phase complete when:** no production code imports or constructs the old generic event types.

------------------------------------------------------------------------

## Phase 7 — Remove the generic event API

**Goal:** Remove external APIs that existed only to expose telemetry events.

- [ ] Remove:

  - `events.subscribe`;
  - `events.unsubscribe`;
  - `events.notify`;
  - `/events` WebSocket support.

- [ ] Remove subscription registries and event publication bridges.
- [ ] Remove event history from `agent.get_detail`.
- [ ] Keep `agent.get_detail` focused on current agent state.
- [ ] Remove API tests that only cover retired event streaming.
- [ ] Do not introduce a replacement JSON representation for ACP updates unless a concrete non-ACP consumer requires one.

**Phase complete when:** ACP events are exposed through ACP, while the control API exposes runtime state.

------------------------------------------------------------------------

## Phase 8 — Final cleanup and documentation

**Goal:** Make the repository describe and enforce one architecture.

- [ ] Search for and remove remaining references to:

  - `AgentEvent`;
  - `AgentEventSource`;
  - `AgentEventStream`;
  - `translate_acp_update`;
  - `subscribe_events`;
  - `events.notify`;
  - `/events`.

- [ ] Remove unused files, imports, aliases, and compatibility wrappers.
- [ ] Run the relevant runtime, ACP, mux, and API test suites.
- [ ] Update Epic 001 documentation to remove the generic event API.
- [ ] Update Epic 008 documentation to describe:

  - the session-owned typed stream;
  - explicit overflow failure;
  - session-scoped mux attachment;
  - mux ownership of no replay history.

- [ ] Update other specifications only where they still reference removed behavior.

**Phase complete when:** code, tests, and documentation all describe the same single typed ACP event path.

------------------------------------------------------------------------

# Final Acceptance Checklist

- [ ] The real ACP `SessionUpdate` type is used end to end.
- [ ] Each concrete ACP session owns exactly one update stream.
- [ ] Replay and live delivery have no gaps, duplicates, or reordering.
- [ ] Subscriber queues are bounded.
- [ ] Overflow terminates the affected subscriber explicitly.
- [ ] No ACP update is silently dropped.
- [ ] Closing a session closes its stream and subscribers.
- [ ] A mux attachment does not silently follow session replacement.
- [ ] The mux forwards exact typed updates and owns no replay history.
- [ ] The old generic event model is fully removed.
- [ ] Runtime lifecycle and Agent Mail behavior use state and logs.
- [ ] Generic event RPC and WebSocket APIs are removed.
- [ ] The focused stream and mux contract tests pass.
- [ ] Epic 001 and Epic 008 reflect the implemented design.
