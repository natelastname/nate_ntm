# Agent Event Refactor – Implementation Plan

This document turns `AGENT_EVENT_REFACTOR.md` into a concrete, repo-aware implementation plan.

The refactor is an **intentional breaking cleanup**:

- Remove the generic `AgentEvent` / `AgentEventStream` telemetry layer.
- Introduce a **session-owned, typed ACP update stream**.
- Make ACP `SessionUpdate` the single, end-to-end abstraction for ACP events.
- Remove non-ACP event producers and generic runtime event APIs.
- Represent runtime lifecycle and Agent Mail state via dedicated models and logs.
- Update the relevant Epic 001 and Epic 008 docs **after** implementation to describe the new design.

> Specs in `specs/001-*.md` and `specs/008-*.md` describe earlier design decisions. They do **not** block this refactor. Updating them is part of the migration.

---

## 1. Target End State (Repo-Level)

At the end of this refactor:

1. **Single typed ACP event stream per session**
   - Each concrete ACP session (represented by `AcpAgentSession`) owns one bounded, in-memory stream of **typed** ACP `SessionUpdate` events.
   - Every ACP `session/update` received for that session becomes exactly one `AgentSessionEvent` in that stream.

2. **Stream belongs to the concrete ACP session**
   - The stream is attached directly to `AcpAgentSession` (or a trivial helper it owns).
   - When the ACP session ends or is replaced (restart/new session):
     - the old stream is closed and all subscribers see closure;
     - a **new** stream is created with its own sequence space for the new session.
   - The logical agent exposes **only its current session’s** stream.

3. **Mux consumes the ACP stream, owns no history**
   - SwarmACPMux (once wired in) subscribes to the session-owned stream via the ACP client layer.
   - It receives replay-then-live `AgentSessionEvent` objects and forwards `event.update` (a `SessionUpdate`) to the external ACP connection.
   - The mux maintains **no separate replay or telemetry history**.

4. **Removed generic runtime event machinery**
   - `AgentEvent`, `AgentEventSource`, and `AgentEventStream` are deleted.
   - `translate_acp_update`, `_model_to_payload`, `_update_kind`, and any `SessionUpdate` reconstruction from generic payloads are deleted.
   - All non-ACP `AgentEvent` producers (runtime lifecycle, Agent Mail, process telemetry) are removed.
   - No replacement generic runtime event channel is introduced.

5. **Runtime lifecycle and Agent Mail represented via state and logs**
   - Lifecycle is represented via `RuntimeState` / `AgentRuntimeState` (`status`, `last_error`, etc.).
   - Agent Mail state is represented via the Agent Mail client and any explicit unread-mail markers in runtime state.
   - Tests that previously asserted on non-ACP `AgentEvent`s now assert on:
     - runtime state transitions;
     - mail-related state/behavior;
     - logging, where appropriate.

6. **Public API and JSON events simplified**
   - The runtime control API (`specs/001-*/contracts/runtime-api.md` and `src/nate_ntm/api/*`) no longer exposes the generic `AgentEvent` shape.
   - JSON-RPC methods and the `/events` WebSocket endpoint for streaming `AgentEvent` telemetry are removed.
   - No JSON representation is introduced for the new session-owned ACP stream **unless** a non-ACP API genuinely still needs to expose it. ACP serialization is left to the ACP SDK at the downstream connection.

7. **Specs updated after implementation**
   - Epic 001 and Epic 008 documents and related specs are updated **after** the code changes to describe the new end state.
   - The updated docs treat the session-owned `SessionUpdate` stream and mux behavior as the canonical design.

---

## 2. New Types and Modules

### 2.1 SessionUpdate alias

Introduce a central alias for ACP `SessionUpdate` types so the runtime depends on a single, internal abstraction rather than the SDK union directly.

**New file:** `src/nate_ntm/runtime/acp_types.py`

```python
from __future__ import annotations

from typing import Any

# TODO: Narrow this alias once the ACP SDK SessionUpdate union is firmly integrated
# and importable in a stable way.
SessionUpdate = Any
```

This may later be tightened to import the actual ACP union.

### 2.2 AgentSessionEvent and AgentSessionEventStream

Introduce a typed, session-owned ACP event model and stream.

**New file:** `src/nate_ntm/runtime/acp_update_stream.py` (name can be adjusted if desired).

```python
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator, Deque, Iterable, List, Optional, Set

from .acp_types import SessionUpdate


@dataclass(frozen=True, slots=True)
class AgentSessionEvent:
    """Single ACP session update event for one concrete session.

    - sequence: 1-based, monotonically increasing within a single ACP session.
    - received_at: timestamp when the runtime observed this update.
    - update: the exact typed SessionUpdate instance from the ACP SDK.
    """

    sequence: int
    received_at: datetime
    update: SessionUpdate


_CLOSE_SENTINEL: object = object()


@dataclass(slots=True)
class AgentSessionEventStream:
    """Bounded, replay-capable stream of AgentSessionEvent for one ACP session.

    Responsibilities:
    - Retain a bounded history of recent AgentSessionEvent values.
    - Provide per-subscriber queues with drop-oldest behavior on overflow.
    - On subscription: replay retained history, then deliver live events.
    - On close: deliver a closure sentinel to all subscribers.
    """

    max_events: int = 200

    _events: Deque[AgentSessionEvent] = field(default_factory=deque, init=False, repr=False)
    _subscribers: Set[asyncio.Queue[object]] = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_events <= 0:
            raise ValueError("max_events must be positive")

    def _append_to_history(self, event: AgentSessionEvent) -> None:
        self._events.append(event)
        overflow = len(self._events) - self.max_events
        if overflow > 0:
            for _ in range(overflow):
                self._events.popleft()

    def publish(self, event: AgentSessionEvent) -> None:
        """Append `event` and fan it out to all subscribers.

        Drop-oldest semantics are applied per subscriber on overflow.
        """

        self._append_to_history(event)

        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    _ = queue.get_nowait()
                except asyncio.QueueEmpty:
                    _ = None
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    # At this point we log in the caller / owner if needed.
                    continue

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[AsyncIterator[AgentSessionEvent]]:
        """Subscribe to this stream.

        Yields an async iterator that:
        - first replays the retained history as of subscription time;
        - then yields live events as they are published;
        - terminates when the stream is closed.
        """

        queue: asyncio.Queue[object] = asyncio.Queue(maxsize=self.max_events)
        self._subscribers.add(queue)

        # Snapshot history at subscription time; do not replay events published
        # concurrently while we enqueue the snapshot.
        snapshot: List[AgentSessionEvent] = list(self._events)
        for ev in snapshot:
            try:
                queue.put_nowait(ev)
            except asyncio.QueueFull:
                try:
                    _ = queue.get_nowait()
                except asyncio.QueueEmpty:
                    _ = None
                try:
                    queue.put_nowait(ev)
                except asyncio.QueueFull:
                    # We accept losing oldest history under sustained backpressure.
                    continue

        async def _iterator() -> AsyncIterator[AgentSessionEvent]:
            try:
                while True:
                    item = await queue.get()
                    if item is _CLOSE_SENTINEL:
                        break
                    yield item  # type: ignore[misc]
            finally:
                self._subscribers.discard(queue)

        try:
            yield _iterator()
        finally:
            # On context exit, ensure this subscriber sees closure.
            try:
                queue.put_nowait(_CLOSE_SENTINEL)
            except asyncio.QueueFull:
                try:
                    _ = queue.get_nowait()
                except asyncio.QueueEmpty:
                    _ = None
                try:
                    queue.put_nowait(_CLOSE_SENTINEL)
                except asyncio.QueueFull:
                    # Give up; iterator will eventually time out or be GCed.
                    pass

    def close(self) -> None:
        """Close the stream and terminate all current subscribers."""

        queues = list(self._subscribers)
        self._subscribers.clear()
        for queue in queues:
            try:
                queue.put_nowait(_CLOSE_SENTINEL)
            except asyncio.QueueFull:
                try:
                    _ = queue.get_nowait()
                except asyncio.QueueEmpty:
                    _ = None
                try:
                    queue.put_nowait(_CLOSE_SENTINEL)
                except asyncio.QueueFull:
                    pass
```

> Exact implementation details can be refined during coding; the shape and semantics above are the intended target.

---

## 3. Phased Implementation Plan

### Phase 0 – Introduce new types (no behavior changes yet)

**Goals:**
- Add `SessionUpdate` alias and `AgentSessionEvent` / `AgentSessionEventStream` to the codebase.
- Do **not** modify existing `AgentEvent`/`AgentEventStream` or APIs yet.

**Tasks:**

- [ ] Add `src/nate_ntm/runtime/acp_types.py` with `SessionUpdate` alias.
- [ ] Add `src/nate_ntm/runtime/acp_update_stream.py` with `AgentSessionEvent` and `AgentSessionEventStream`.
- [ ] Ensure unit tests and mypy (if present) still run without referencing the new types.


### Phase 1 – Attach the stream to AcpAgentSession and ACP callbacks

**Goals:**
- Make each concrete ACP session own a session-scoped event stream.
- Populate that stream directly from ACP `session/update` callbacks.

**Changes in `src/nate_ntm/runtime/acp_client.py`:**

- [ ] Extend `AcpAgentSession` with:
  - [ ] `event_stream: AgentSessionEventStream | None` field.
  - [ ] `sequence: int = 0` field for per-session monotonic sequencing.
- [ ] Initialize `event_stream = AgentSessionEventStream()` when creating a new `AcpAgentSession`.
- [ ] On session shutdown/replacement, call `event_stream.close()` on the old session before discarding it.

**Changes in `src/nate_ntm/runtime/acp_protocol_client.py`:**

- [ ] Replace the existing `EventSink = Callable[[AgentEvent], None]` with a typed SessionUpdate sink:
  - [ ] Define `SessionUpdateSink = Callable[[str, str, SessionUpdate, datetime], None]` (or similar) and use it as the `event_sink` type.
- [ ] Update `NateNtmAcpProtocolClient.__init__` to accept the new sink.
- [ ] Update `session_update` to:
  - [ ] Increment an internal sequence counter (still useful for diagnostics/logging if desired).
  - [ ] Call `self._event_sink(agent_id, session_id, update, received_at)` without constructing `AgentEvent`.

**Changes in `src/nate_ntm/runtime/acp_client.py` (BaseAcpClient):**

- [ ] Implement a private handler method, e.g. `_handle_session_update(agent_id, session_id, update, received_at)` that:
  - [ ] Locates the current `AcpAgentSession` for `agent_id`.
  - [ ] Validates that `session_id` matches the session’s canonical identifier (drop or log if stale/mismatched).
  - [ ] Lazily creates `session.event_stream` if missing.
  - [ ] Increments `session.sequence` and publishes an `AgentSessionEvent(sequence, received_at, update)` to `session.event_stream`.
- [ ] Wire `_handle_session_update` into the ACP connection setup so that `NateNtmAcpProtocolClient` uses it as `event_sink`.


### Phase 2 – Expose subscription APIs over the session-owned stream

**Goals:**
- Provide a public, typed API on the ACP client to subscribe to ACP updates.
- Use the new `AgentSessionEventStream` as the implementation.

**Changes in `src/nate_ntm/runtime/acp_client.py`:**

- [ ] Remove the legacy event subscription machinery:
  - [ ] `_EVENT_QUEUE_MAXSIZE`, `_EVENT_STREAM_CLOSED`.
  - [ ] `_event_subscribers` mapping and related helpers.
  - [ ] `_emit_event`.
  - [ ] `subscribe_events`, `iter_events`, and `wait_for_event` that operate on `AgentEvent`.
  - [ ] The `on_event: Callable[[AgentEvent], None] | None` callback attribute used for telemetry.
- [ ] Introduce a new API:

  ```python
  @asynccontextmanager
  async def subscribe_acp_updates(
      self,
      agent_id: str,
  ) -> AsyncIterator[AsyncIterator[AgentSessionEvent]]:
      ...
  ```

  that:

  - [ ] Looks up the current `AcpAgentSession` for `agent_id`.
  - [ ] If no active session/stream exists, yields an iterator that terminates immediately.
  - [ ] Otherwise, delegates to `session.event_stream.subscribe()`.

- [ ] Optionally add a thin `iter_acp_updates(agent_id: str) -> AsyncIterator[AgentSessionEvent]` convenience wrapper.
- [ ] Update any call sites (tests, future mux code) to use `subscribe_acp_updates` instead of `subscribe_events`.


### Phase 3 – Remove AgentEvent / AgentEventStream and non-ACP producers

**Goals:**
- Remove the generic telemetry model entirely.
- Switch runtime and tests to state-based or SessionUpdate-based assertions.

#### 3.1 Delete or repurpose `runtime/events.py`

**File:** `src/nate_ntm/runtime/events.py`

- [ ] Remove `AgentEventSource` enum.
- [ ] Remove `AgentEvent` dataclass.
- [ ] Remove `AgentEventStream` class.
- [ ] Either:
  - [ ] Delete the file and update imports, **or**
  - [ ] Repurpose it as a thin re-export of `AgentSessionEvent` / `AgentSessionEventStream` if a stable import path is beneficial.

**Imports to clean up:**

- [ ] `src/nate_ntm/api/runtime_api.py` (AgentEvent imports).
- [ ] `src/nate_ntm/api/server.py`.
- [ ] `src/nate_ntm/api/models.py`.
- [ ] `src/nate_ntm/api/jsonrpc.py`.
- [ ] `src/nate_ntm/api/runtime_client.py` (docstrings, if any).
- [ ] `src/nate_ntm/runtime/acp_protocol_client.py`.
- [ ] `src/nate_ntm/runtime/acp_connection.py`.
- [ ] `src/nate_ntm/runtime/acp_client.py`.
- [ ] `src/nate_ntm/runtime/state.py` (forward ref to `AgentEventStream`).
- [ ] `src/nate_ntm/runtime/runner.py`.
- [ ] `src/nate_ntm/runtime/agents.py`.
- [ ] All tests under `tests/unit` and `tests/integration` that reference `AgentEvent` / `AgentEventStream` / `AgentEventSource`.


#### 3.2 Remove non-ACP event producers in AgentSupervisor

**File:** `src/nate_ntm/runtime/agents.py`

- [ ] Remove imports of `AgentEvent`, `AgentEventSource`, and `AgentEventStream`.
- [ ] Remove the `on_agent_event: Callable[[AgentEvent], None] | None` field.
- [ ] Remove `_get_or_create_event_stream` helper.
- [ ] Remove `_append_runtime_event`.
- [ ] Remove `append_agent_event` (ACP and Agent Mail-sourced telemetry appender).
- [ ] In `ensure_agent_runtime_state`, stop attaching `AgentEventStream` to `AgentRuntimeState`.
- [ ] Update lifecycle helpers to rely solely on state and logging:
  - [ ] `mark_agent_failed` updates `AgentRuntimeState.status` and `last_error` and logs; does **not** append events.
  - [ ] `restart_agent` updates status and placeholders and logs; does **not** append events.
  - [ ] `record_unread_mail` optionally updates state or logs; does **not** create `MailReceived` events.

**Test updates:**

- [ ] `tests/unit/runtime/test_agents.py` – replace all assertions about `AgentEventStream` and lifetime events with assertions on `RuntimeState` / `AgentRuntimeState` and logging.
- [ ] `tests/unit/runtime/test_scheduler.py` – replace `MailReceived` event assertions with state/log assertions (e.g., unread mail flags in `AgentMailClient` and scheduler behavior).


#### 3.3 Remove AgentEvent usage from RuntimeDaemon and control API

**Files:**

- `src/nate_ntm/runtime/state.py`
- `src/nate_ntm/runtime/daemon.py`
- `src/nate_ntm/api/server.py`
- `src/nate_ntm/api/jsonrpc.py`
- `src/nate_ntm/api/runtime_api.py`
- `src/nate_ntm/runtime/runner.py`

**State and daemon:**

- [ ] In `AgentRuntimeState` (in `runtime/state.py`), remove `event_stream: Optional["AgentEventStream"]` field.
- [ ] In `RuntimeDaemon.get_agent_detail`:
  - [ ] Stop reading from any `event_stream`.
  - [ ] Remove the `events` list from the result; return only the `agent` payload.
  - [ ] Update unit tests that call `get_agent_detail` to no longer expect an `events` array.
- [ ] In `RuntimeDaemon.create` and `RuntimeDaemon.resume`:
  - [ ] Remove wiring of `acp_client.on_event = agent_supervisor.append_agent_event`.

**API server and JSON-RPC:**

- [ ] In `api/server.py`:
  - [ ] Remove subscription registry (`_subscriptions`, `_next_subscription_id`).
  - [ ] Remove `subscribe_events`, `unsubscribe_events`.
  - [ ] Remove `build_agent_event_notifications`.
  - [ ] Ensure the server now only exposes:
    - `get_runtime_status`,
    - `get_swarm_overview`,
    - `shutdown_runtime`,
    - `get_agent_detail` (agent payload only).
- [ ] In `api/jsonrpc.py`:
  - [ ] Remove `build_events_notify_messages`.
  - [ ] Remove JSON-RPC method handlers for `events.subscribe`, `events.unsubscribe`, and `events.notify`.
- [ ] In `api/runtime_api.py`:
  - [ ] Remove all `/events` WebSocket logic and handshake.
  - [ ] Remove `app.state.subscription_clients` and `app.state.client_subscriptions`.
  - [ ] Remove `_attach_subscriptions` / `_detach_client` helpers.
  - [ ] Remove the `publish_event` coroutine and its attachment to `app.state.publish_event`.
- [ ] In `runtime/runner.py`:
  - [ ] Remove wiring of `supervisor.on_agent_event` to `app.state.publish_event`.

**Test updates:**

- [ ] `tests/integration/quickstart/test_runtime_ws_events_us3.py` – remove or rewrite; the WebSocket event streaming API is being retired.
- [ ] `tests/unit/api/test_server.py` – remove tests for event subscriptions and notifications; adjust `agent.get_detail` expectations.
- [ ] `tests/unit/api/test_jsonrpc.py` – remove `build_events_notify_messages` tests and `events.*` JSON-RPC tests.
- [ ] `tests/unit/runtime/test_runner.py` – remove checks related to the WebSocket event bridge and `build_events_notify_messages`.


#### 3.4 Remove ACP→AgentEvent translation

**File:** `src/nate_ntm/runtime/acp_event_translation.py`

- [ ] Delete this file entirely, including:
  - [ ] `_model_to_payload`.
  - [ ] `_update_kind`.
  - [ ] `translate_acp_update`.
- [ ] Remove imports of `translate_acp_update` from `acp_protocol_client.py` and any other modules.
- [ ] Delete `tests/unit/runtime/test_acp_event_translation.py`.


#### 3.5 Update ACP-related tests to use typed SessionUpdate stream

**File:** `tests/integration/runtime_acp/test_runtime_daemon_acp_async_real_path_epic005.py`

- [ ] Replace imports of `AgentEvent` with `AgentSessionEvent` where needed.
- [ ] Replace helper `_extract_text_payloads(events: list[AgentEvent])` that digs into `event.payload["update"]` with logic that inspects `event.update` (the typed `SessionUpdate` object):
  - [ ] Use actual ACP model attributes (e.g. `content`, `text` fields) instead of JSON payloads.
- [ ] Replace uses of `NateOhaAcpClient.subscribe_events` with `subscribe_acp_updates`:

  ```python
  async with acp_client.subscribe_acp_updates(agent_id) as events:
      async for event in events:
          # event.update is the SessionUpdate
  ```

- [ ] Update any helper equivalent to `next_matching_event` to operate on `AgentSessionEvent` (or `SessionUpdate` via `event.update`).
- [ ] Update any other integration/unit tests under `tests/unit/runtime/test_acp_connection.py`, `test_acp_client_subscriptions.py`, etc., to:
  - [ ] Stop asserting on `AgentEvent.source`, `type`, and `payload`.
  - [ ] Assert directly on typed `SessionUpdate` structures where relevant.


### Phase 4 – Macro-Level Tests for the New Stream and Mux

**Goals:**
- Ensure the new session-owned stream behaves correctly (history, live updates, closure).
- Ensure ACP client subscription semantics are correct.
- Once implemented, ensure SwarmACPMux behaves as specified.

#### 4.1 AgentSessionEventStream behavior

**New unit tests (e.g. `tests/unit/runtime/test_acp_update_stream.py`):**

- [ ] **History then live**: publish N events, then subscribe and assert that:
  - [ ] the subscriber sees those N events in order;
  - [ ] then sees newly published events.
- [ ] **Typed preservation**: publish representative `SessionUpdate` instances and assert that `event.update` retains type and fields exactly.
- [ ] **Independent streams**: create two `AgentSessionEventStream` instances, publish to each, and assert no cross-talk.
- [ ] **Close semantics**: after calling `stream.close()`, subscribers terminate (no further events).

#### 4.2 BaseAcpClient subscribe_acp_updates

**Tests (existing or new):**

- [ ] For an agent with a live session:
  - [ ] Call `subscribe_acp_updates(agent_id)`.
  - [ ] Assert the subscription yields retained history, then live events.
- [ ] For an agent with no active session:
  - [ ] Call `subscribe_acp_updates(agent_id)` and assert the iterator terminates immediately.
- [ ] For two agents with concurrent sessions:
  - [ ] Assert each `subscribe_acp_updates` stream only contains that agent’s events.

#### 4.3 SwarmACPMux (when wired)

Once SwarmACPMux is implemented and uses the new APIs (per Feature 008):

- [ ] Add tests that:
  - [ ] **Replay+live**: when attaching a mux, pre-populated retained events are forwarded to the external ACP connection, followed by live updates.
  - [ ] **Exactly once**: events are not duplicated across attach/detach cycles.
  - [ ] **Session replacement**: when an ACP session is replaced, the mux stops reading from the old session’s stream and uses the new one, replaying only the new session’s history.


### Phase 5 – Documentation and Spec Updates (Epic 001 & 008)

**Goals:**
- Align Epics 001 and 008 with the new implementation.
- Record that `AgentEvent` telemetry and runtime event streaming APIs have been removed.

#### 5.1 Update AGENT_EVENT_REFACTOR.md and this plan

- [ ] Update `AGENT_EVENT_REFACTOR.md` to:
  - [ ] Note that the refactor has been implemented.
  - [ ] Briefly describe the new `AgentSessionEvent` / `AgentSessionEventStream` design.
- [ ] Update `AGENT_EVENT_REFACTOR_PLAN.md` (this file) to mark all completed checklist items.

#### 5.2 Update Epic 001 – Runtime control API

**File:** `specs/001-swarm-runtime-orchestrator/contracts/runtime-api.md`

- [ ] Remove the `AgentEvent` type from Common Types.
- [ ] Update `agent.get_detail` to:
  - [ ] Return only the `agent` object, without an `events` array.
  - [ ] Note that per-agent event histories are not part of the control API in this design.
- [ ] Remove or clearly mark as obsolete the `events.subscribe`, `events.notify`, and `events.unsubscribe` sections.
- [ ] Where necessary, add notes that ACP-level event history is now accessible only via ACP integration (not via the runtime control API).

#### 5.3 Update Epic 008 – Swarm ACP mux

**Files:**

- `specs/008-swarm-acp-mux/data-model.md`
- `specs/008-swarm-acp-mux/spec.md`
- `specs/008-swarm-acp-mux/research.md`
- `specs/008-swarm-acp-mux/plan.md`

- [ ] In `data-model.md`:
  - [ ] Replace the `AcpUpdateStream` concept with `AgentSessionEventStream` (or equivalent) as the per-session, replay-capable stream of `SessionUpdate` events.
  - [ ] Remove references to `AgentEvent` as an ACP-agnostic telemetry layer.
- [ ] In `spec.md` and `research.md`:
  - [ ] Remove the decision to keep `AgentEvent` ACP-agnostic.
  - [ ] Document that ACP events are represented as `AgentSessionEvent(SessionUpdate)` throughout the mux pipeline.
  - [ ] Remove the `require_session_update(event: AgentEvent) -> SessionUpdate` boundary and any reconstruction logic.
  - [ ] Update diagrams to show:

    ```
    ACP SDK → NateNtmAcpProtocolClient.session_update
           → BaseAcpClient._handle_session_update
           → AcpAgentSession.event_stream (AgentSessionEventStream)
           → SwarmACPMux (subscribe_acp_updates)
           → external ACP connection (SessionUpdate forwarding)
    ```

- [ ] In `plan.md`:
  - [ ] Mark any tasks that referenced `AgentEvent` telemetry or `require_session_update` as superseded.
  - [ ] Ensure the plan for SwarmACPMux uses `subscribe_acp_updates` and `AgentSessionEventStream`.

#### 5.4 Other specs referencing AgentEvent

- [ ] `specs/002-nate-oha-acp-adapter/spec.md` and `plan.md`:
  - [ ] Remove references to `on_event: Callable[[AgentEvent], None]`.
  - [ ] Replace with usage of `subscribe_acp_updates` / `AgentSessionEventStream` as needed.
- [ ] `specs/003-textual-runtime-console/tasks.md` and others:
  - [ ] Replace references to runtime `AgentEvent` streaming with state-based observation or ACP-facing views, or explicitly mark those references as obsolete.

---

## 4. High-Level Progress Checklist

For quick tracking across phases, use this consolidated checklist:

- [ ] Phase 0 – New types added (`acp_types.py`, `acp_update_stream.py`).
- [ ] Phase 1 – `AcpAgentSession` owns `AgentSessionEventStream` and ACP callbacks populate it.
- [ ] Phase 2 – `BaseAcpClient.subscribe_acp_updates` and related helpers replace `subscribe_events`.
- [ ] Phase 3 – `AgentEvent` / `AgentEventStream` / non-ACP event producers removed; runtime/API/tests migrated to state & typed streams.
- [ ] Phase 4 – Macro-level tests for `AgentSessionEventStream`, `subscribe_acp_updates`, and SwarmACPMux added/passing.
- [ ] Phase 5 – Epic 001 & 008 and related specs updated to describe the new design.
