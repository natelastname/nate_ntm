# Data Model: SwarmACPMux and Typed ACP Session Streaming (Epic 009)

This document describes the in-memory data structures and relationships introduced or constrained by SwarmACPMux, building on Epic 008's typed ACP session streaming layer.

It focuses on **connection-local mux state** and its interaction with per-agent `AcpSessionUpdateStream` instances.

---

## 1. Core Entities

### 1.1 ReceivedSessionUpdate and SessionUpdate

Epic 008 defines the typed ACP session update model. SwarmACPMux depends on (but does not implement) these types.

Conceptually:

```python
@dataclass(slots=True)
class SessionUpdate:
    ...  # ACP SDK-defined fields for a single protocol update

@dataclass(slots=True)
class ReceivedSessionUpdate:
    update: SessionUpdate
    sequence: int
    received_at: datetime
```

- `SessionUpdate` is the opaque, protocol-level update that must be forwarded unchanged to external ACP clients.
- `ReceivedSessionUpdate` wraps a `SessionUpdate` with runtime-only metadata used for ordering, logging, and diagnostics.

SwarmACPMux:

- consumes `AsyncIterator[ReceivedSessionUpdate]` instances from `subscribe_acp_updates(agent_id)`;
- forwards only the underlying `SessionUpdate` via `ExternalACPConnection.session_update(session_id, update)`.

---

### 1.2 SwarmAgentClient

`SwarmAgentClient` is a narrow protocol capturing the agent operations used by the mux.

```python
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Protocol

class SwarmAgentClient(Protocol):
    def subscribe_acp_updates(
        self,
        agent_id: str,
    ) -> AbstractAsyncContextManager[AsyncIterator[ReceivedSessionUpdate]]:
        """Yield retained updates followed by live updates."""

    async def prompt(
        self,
        agent_id: str,
        prompt: str,
    ) -> str | None: ...

    async def interrupt(self, agent_id: str) -> None: ...
```

- Implemented by `NateOhaAcpClient` / `AcpAgentSession`.
- Owns per-agent `AcpSessionUpdateStream` instances and their replay/overflow semantics.

---

### 1.3 ExternalACPConnection

`ExternalACPConnection` abstracts the Swarm ACP server adapter's access to the external ACP transport.

```python
class ExternalACPConnection(Protocol):
    async def session_update(
        self,
        *,
        session_id: str,
        update: SessionUpdate,
    ) -> None: ...
```

- Implemented by the Swarm ACP server adapter.
- Writes typed `SessionUpdate` objects to the external ACP session identified by `session_id`.

---

### 1.4 SwarmACPMux

`SwarmACPMux` is connection-scoped state for a single external ACP session.

```python
from dataclasses import dataclass, field
import asyncio

@dataclass(slots=True)
class SwarmACPMux:
    daemon: RuntimeDaemon
    agent_client: SwarmAgentClient
    external_connection: ExternalACPConnection
    external_session_id: str

    attached_agent_id: str | None = None

    _attachment: _Attachment | None = None
    _lifecycle_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
    )
    _failure: asyncio.Future[None] = field(
        init=False,
        repr=False,
    )
    _closed: bool = field(
        default=False,
        init=False,
        repr=False,
    )
```

- `daemon: RuntimeDaemon`
  - Authoritative for swarm and agent metadata, including durable swarm membership and agent detail views.
- `agent_client: SwarmAgentClient`
  - Access to per-agent typed ACP update streams and prompt/interrupt operations.
- `external_connection: ExternalACPConnection`
  - Output channel for forwarding `SessionUpdate` objects to the external ACP session.
- `external_session_id: str`
  - Identifier for the external ACP session.
- `attached_agent_id: str | None`
  - The agent currently attached to this external session, if any.
- `_attachment: _Attachment | None`
  - Internal record of the current concrete ACP subscription and its forwarding task.
- `_lifecycle_lock: asyncio.Lock`
  - Serializes lifecycle transitions that mutate mux state.
- `_failure: asyncio.Future[None]`
  - Represents the first fatal forwarding failure, observed via `wait_failed()`.
- `_closed: bool`
  - Marks the mux as closed; subsequent operations raise `SwarmACPMuxClosedError`.

---

### 1.5 _Attachment

`_Attachment` retains the concrete ACP subscription and iterator for the currently attached agent.

```python
@dataclass(slots=True)
class _Attachment:
    agent_id: str
    subscription: AbstractAsyncContextManager[
        AsyncIterator[ReceivedSessionUpdate]
    ]
    updates: AsyncIterator[ReceivedSessionUpdate]
    forwarding_enabled: asyncio.Event
    task: asyncio.Task[None] | None = None
```

- `agent_id`
  - The agent to which this external session is currently attached.
- `subscription`
  - The entered subscription context returned by `subscribe_acp_updates(agent_id)`.
- `updates`
  - The async iterator yielded from the subscription; produces `ReceivedSessionUpdate` values.
- `forwarding_enabled`
  - An `asyncio.Event` gate; `_run_forwarding()` waits on this before reading from `updates`.
- `task`
  - The background forwarding task that reads from `updates` and calls `external_connection.session_update(...)`.

Identity of `_Attachment` objects (or their associated tokens, see below) is used to avoid stale activations and cleanups.

---

### 1.6 PreparedAttachment

`PreparedAttachment` is a small, immutable handle returned by `prepare_attach()`.

```python
@dataclass(frozen=True, slots=True)
class PreparedAttachment:
    agent_id: str
    token: object
```

- `agent_id`
  - The agent prepared for attachment.
- `token`
  - An opaque identity marker for the underlying `_Attachment`.

`activate_attachment(prepared)` verifies both that the mux is still open and that `prepared.token` matches the current `_Attachment`. This prevents stale acknowledgments from activating obsolete attachments.

---

## 2. Runtime Views Consumed by the Mux

### 2.1 Swarm Status View

The mux reuses the existing swarm overview view from spec 001.

Conceptually, `RuntimeDaemon.get_swarm_status()` returns a structure like:

```jsonc
{
  "swarm_id": "default",
  "project_path": "/abs/path/to/project",
  "runtime_status": "RuntimeStatus",
  "agent_counts": { /* counts by AgentStatus */ },
  "agents": [ /* AgentSummary[] */ ]
}
```

`SwarmACPMux.get_swarm_status()` wraps this with connection-local attachment state:

```jsonc
{
  "attached_agent_id": "agent-1" | null,
  "swarm": <get_swarm_status result>
}
```

---

### 2.2 Agent Detail View

Similarly, `RuntimeDaemon.get_agent_detail(agent_id, max_events)` provides detailed information for a single agent.

Conceptually:

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
  "events": [ /* AgentEvent[] */ ]
}
```

`SwarmACPMux.get_agent_detail(agent_id, max_events)` adds whether this mux is currently attached to that agent:

```jsonc
{
  "attached": true | false,
  "agent": <get_agent_detail result>.agent,
  "events": <get_agent_detail result>.events
}
```

---

## 3. Relationships and Invariants

Key relationships:

- One `SwarmACPMux` instance per external ACP session.
- At most one `_Attachment` per mux at any time.
- An `_Attachment` always refers to exactly one concrete agent ACP session (one subscription/iterator pair).
- `PreparedAttachment` provides a stable handle for activating the current `_Attachment` after the adapter sends the `_attach` acknowledgment.

Invariants (from the spec):

1. A mux is attached to at most one agent.
2. A successful attachment refers to one concrete agent ACP session and does not automatically follow replacements.
3. The internal subscription exists before attachment success is acknowledged.
4. New-agent replay begins only after the attachment acknowledgment is sent.
5. The old forwarding task has stopped before a new attachment is acknowledged.
6. Per-agent ordering is preserved exactly as yielded by Epic 008.
7. Detaching the mux does not stop the agent or affect independent subscribers.
8. No second ACP buffer or subscription system is introduced by the mux.
