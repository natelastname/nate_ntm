# Data Model: SwarmACPMux and Agent Event Streams (Feature 008)

This document describes the core data structures and relationships introduced or constrained by SwarmACPMux, aligned with the existing runtime orchestrator (spec 001).

The focus is on **in-memory state** for connection-scoped muxes and replay-capable agent event streams; persistent swarm and agent state remains owned by `RuntimeDaemon` / `SwarmState` and documented in spec 001.

## 1. Core In-Memory Entities

### 1.1 AgentEvent (runtime-level)

`AgentEvent` is the normalized event model used throughout the runtime. It remains ACP-agnostic and JSON-serializable.

Conceptual shape (matching spec 001 `runtime-api.md`):

```python
@dataclass(slots=True)
class AgentEvent:
    event_id: str
    timestamp: datetime
    agent_id: str
    source: Literal["ACP", "AgentMail", "Runtime", "Client"]
    type: str
    payload: Mapping[str, object]
```

Notes:

- `payload` is a summarized, structured view of the event; it need not contain the full raw ACP or Agent Mail payload.
- ACP-specific models (e.g., `SessionUpdate`) are **not** stored directly in `AgentEvent`.
- The ACP integration code is responsible for converting between ACP SDK types and this normalized representation.

### 1.2 ACP Update Stream (transport-level)

Each agent exposes an **ACP update stream** that carries exact protocol updates (e.g., `SessionUpdate` objects) for that agent. This stream is owned by the ACP client layer (e.g., `NateOhaAcpClient`).

Conceptual structure:

```python
class AcpUpdateStream:
    def publish(self, update: SessionUpdate) -> None: ...

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[AsyncIterator[SessionUpdate]]:
        """Yield retained history of updates, then live updates, then a closure sentinel."""
        ...
```

Implementation properties:

- **Retained history**
  - A bounded deque of recent `SessionUpdate` objects per agent, ordered oldest→newest.
  - When full, oldest updates are dropped.

- **Per-subscriber queues**
  - Each subscriber gets its own bounded queue.
  - If a subscriber is too slow, the queue drops oldest updates to make room for new ones (drop-oldest policy).
  - This matches the existing `NateOhaAcpClient` semantics for live subscribers.

- **Replay-then-live semantics**
  - On subscription:
    - capture a replay boundary and enqueue retained updates up to that boundary;
    - then deliver live updates through the same queue;
    - finally yield a closure sentinel when the stream ends.

- **Durability**
  - Streams are in-memory only; they are rebuilt empty on runtime restart.
  - Retained history is best-effort context for ACP clients (including SwarmACPMux), not a persistence mechanism.

`SwarmACPMux` subscribes to this ACP update stream for the currently attached agent and forwards the resulting `SessionUpdate` objects to the external ACP connection. It does **not** reconstruct protocol messages from `AgentEvent` telemetry.

The separate `AgentEvent` history used by the runtime control API and dashboards remains as defined in spec 001 and is not reused as a transport bus here.

### 1.3 SwarmACPMux

`SwarmACPMux` is connection-scoped state for a single external ACP session.

Conceptual dataclass (simplified from the spec):

```python
@dataclass(slots=True)
class SwarmACPMux:
    daemon: RuntimeDaemon
    agent_client: SwarmAgentClient
    external_connection: ExternalACPConnection
    external_session_id: str

    attached_agent_id: str | None = None
    _forwarding_task: asyncio.Task[None] | None = None
    _closed: bool = False
```

Fields:

- `daemon: RuntimeDaemon`
  - Runtime daemon instance; authoritative for swarm metadata and agent detail.
- `agent_client: SwarmAgentClient`
  - Narrow interface for per-agent operations (`subscribe_events`, `prompt`, `interrupt`).
- `external_connection: ExternalACPConnection`
  - Adapter-owned abstraction for writing ACP messages to the external session.
- `external_session_id: str`
  - Identifier for the external ACP session.
- `attached_agent_id: str | None`
  - Currently attached agent ID, if any.
- `_forwarding_task: asyncio.Task | None`
  - Background task consuming a subscription to the attached agentŌĆÖs events and forwarding them to the external connection.
- `_closed: bool`
  - Marks the mux as closed; further operations raise `SwarmACPMuxClosedError`.

Notes:

- There is **no explicit subscription field**; the subscription context is owned by `_forwarding_task` via `agent_client.subscribe_events()`.
- Detach/shutdown is implemented by cancelling and awaiting `_forwarding_task`.

### 1.4 SwarmAgentClient and ExternalACPConnection

The mux depends on two protocols, defined in the spec:

```python
class SwarmAgentClient(Protocol):
    def subscribe_acp_updates(
        self,
        agent_id: str,
    ) -> AbstractAsyncContextManager[AsyncIterator[SessionUpdate]]: ...

    async def prompt(self, agent_id: str, prompt: str) -> str | None: ...

    async def interrupt(self, agent_id: str) -> None: ...


class ExternalACPConnection(Protocol):
    async def session_update(
        self,
        *,
        session_id: str,
        update: SessionUpdate,
    ) -> None: ...
```

- `SwarmAgentClient` can be implemented by `NateOhaAcpClient`.
- `ExternalACPConnection` is implemented by the Swarm ACP server adapter, which holds the concrete ACP connection object.

## 2. Swarm and Agent Views for the Mux

SwarmACPMux does not introduce new persisted views. It consumes the existing runtime introspection views defined in spec 001.

### 2.1 Swarm Status View

Source: `swarm.get_overview` (
`specs/001-swarm-runtime-orchestrator/contracts/runtime-api.md`).

Shape (summarized):

```jsonc
{
  "swarm_id": "default",
  "project_path": "/abs/path/to/project",
  "runtime_status": "RuntimeStatus",
  "agent_counts": { /* counts by AgentStatus */ },
  "agents": [AgentSummary, ...]
}
```

`SwarmACPMux.get_swarm_status()` wraps this as:

```jsonc
{
  "attached_agent_id": "agent-1" | null,
  "swarm": <swarm.get_overview result>
}
```

### 2.2 Agent Detail View

Source: `agent.get_detail` (same contract file).

Shape (summarized):

```jsonc
{
  "agent": {
    "agent_id": "nav-1",
    "display_name": "Navigator 1",
    "status": "AgentStatus",
    "agent_mail_identity": "...",
    "conversation_id": "...",
    "last_error": null
  },
  "events": [AgentEvent, ...]
}
```

`SwarmACPMux.get_agent_detail(agent_id, max_events)` wraps this as:

```jsonc
{
  "attached": true | false,
  "agent": <agent.get_detail result>.agent,
  "events": <agent.get_detail result>.events  // bounded by max_events
}
```

## 3. State Transitions

### 3.1 Attachment Lifecycle per Mux

For a single `SwarmACPMux` instance:

1. **Initial state (after construction)**

   ```python
   attached_agent_id = None
   _forwarding_task = None
   _closed = False
   ```

2. **Attach (`attach(agent_id)`)**

   - Ensure mux is open (`_ensure_open`).
   - Validate `agent_id` via durable swarm membership (`_require_known_agent`).
   - If already attached to the same agent with a live `_forwarding_task`, return.
   - Otherwise:
     - `await detach()` to stop any prior forwarding.
     - Set `attached_agent_id = agent_id`.
     - Start `_forwarding_task = asyncio.create_task(_forward_agent_events(agent_id), ...)`.

3. **Forwarding loop (`_forward_agent_updates`)**

   - Use `agent_client.subscribe_acp_updates(agent_id)` to obtain an async iterator of `SessionUpdate`.
   - For each update, call `_forward_external_update(update)`:
     - Optionally apply adapter-specific translation, but do not round-trip through `AgentEvent`.
     - Send via `external_connection.session_update(session_id=external_session_id, update=update)` (or a transformed equivalent).
   - On normal completion:
     - If this task is still the current `_forwarding_task` for `attached_agent_id`, clear `attached_agent_id` and `_forwarding_task`.

4. **Detach (`detach()`)**

   - Capture `task = _forwarding_task`.
   - Set `_forwarding_task = None` and `attached_agent_id = None`.
   - If `task is None`, return (idempotent detach).
   - Cancel `task` and await it, swallowing `asyncio.CancelledError`.
   - The underlying agent continues running; only mux attachment is removed.

5. **Close (`close()`)**

   - If `_closed` is already `True`, return.
   - Set `_closed = True`.
   - `await detach()`.
   - Subsequent operations that require an open mux raise `SwarmACPMuxClosedError`.

### 3.2 ACP Update Stream Retention and Replay

For each per-agent `AcpUpdateStream`:

- **Publish**: `publish(update)` appends to the retained deque of `SessionUpdate` objects (dropping oldest if full) and enqueues to each subscriber's bounded queue (dropping oldest per subscriber as needed).
- **Subscribe**:
  - A subscriber calls `subscribe()` and receives an async iterator.
  - The iterator yields:
    1. retained updates up to a capture boundary;
    2. then all future updates until stream closure;
    3. then a closure sentinel and terminates.

On runtime restart, all in-memory ACP update streams and muxes are discarded; external clients are expected to reconnect and reattach as needed. The runtime's separate `AgentEvent` history for observability is rebuilt as agents resume and produce new events.

## 4. Relationships Summary

- One `RuntimeDaemon` instance manages one swarm.
- Each swarm has many agents; each agent has:
  - one ACP update stream (`AcpUpdateStream`) for exact `SessionUpdate` transport; and
  - an `AgentEvent` history used for runtime observability (as in spec 001).
- Each ACP update stream may have many subscribers (including SwarmACPMux instances and other ACP-aware consumers).
- Each `SwarmACPMux` is associated with exactly one external ACP session and, at any given time, at most one attached agent.
- Swarm and agent views used by the mux (`get_swarm_status`, `get_agent_detail`) reuse the contracts defined in spec 001 and read from `AgentEvent` history, not from ACP transport streams.
