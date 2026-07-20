# Contract: SwarmACPMux ACP Session Behavior (Epic 009)

This contract defines how SwarmACPMux and the Swarm ACP server adapter expose the nate_ntm swarm to external ACP clients at the **logical session** level.

It builds on:

- The Epic 009 SwarmACPMux spec (`specs/009-swarm-acp-mux/spec.md`).
- The typed ACP session streaming layer from Epic 008 (`SessionUpdate`, `ReceivedSessionUpdate`, `AcpSessionUpdateStream`).
- The runtime control API contracts from spec 001 (for swarm and agent views).

The focus here is on:

- Reserved swarm-control operations and their semantics.
- Attachment and forwarding behavior as seen by an external ACP client.
- Logical error codes and ordering guarantees.

Wire-level ACP encoding (how these operations appear as ACP extension methods or messages) is left to the ACP SDK and the Swarm ACP server adapter.

---

## 1. General Model

- One **SwarmACPMux** is created per external ACP session.
- Each mux is attached to **at most one agent at a time**.
- The Swarm ACP server adapter owns the concrete ACP transport and protocol:
  - it decodes ACP requests/notifications into logical operations;
  - it calls mux methods and runtime helpers;
  - it encodes results/errors back into ACP responses.

The mux consumes typed ACP updates through `subscribe_acp_updates(agent_id)` and forwards the underlying `SessionUpdate` objects via `ExternalACPConnection.session_update(session_id, update)`.

---

## 2. Error Codes (Logical)

The adapter maps mux/domain errors and validation failures to a small set of stable, string-valued error codes.

Suggested logical codes:

- `"MUX_NO_ATTACHED_AGENT"`
  - Client attempted an agent-directed operation (prompt, interrupt, etc.) with no attached agent.
- `"MUX_UNKNOWN_AGENT"`
  - Requested `agent_id` is not part of the durable swarm membership.
- `"MUX_INVALID_REQUEST"`
  - Malformed or missing fields in a reserved-operation request.
- `"MUX_STALE_ATTACHMENT"`
  - The client attempted to activate an attachment or otherwise rely on a `PreparedAttachment` token that no longer matches the current `_Attachment`.
- `"MUX_INTERNAL_ERROR"`
  - Unexpected failure in mux or adapter; details are logged server-side.

The actual on-the-wire error envelope (e.g., JSON-RPC error object vs. ACP-specific error type) is determined by the ACP SDK. This contract fixes only the **logical codes and meanings**.

---

## 3. Reserved Swarm-Control Operations

The following operations are **reserved swarm-level controls** that the adapter must recognize and route to SwarmACPMux and/or `RuntimeDaemon`. On the wire, they MAY be expressed as ACP extension methods or notifications; this contract describes them as logical requests and responses.

### 3.1 `_swarm_status`

Return swarm-level status and mux attachment information for the current external ACP session.

**Logical request payload:**

```jsonc
{
  "op": "_swarm_status",
  "payload": {}
}
```

**Mux behavior:**

- Call `mux.get_swarm_status()`.
- This in turn calls `daemon.get_swarm_status()` (or equivalent) and wraps the result with `attached_agent_id`.

**Logical response payload:**

```jsonc
{
  "attached_agent_id": "agent-1" | null,
  "swarm": {
    // Exactly the shape of swarm.get_overview result from spec 001
    "swarm_id": "default",
    "project_path": "/abs/path/to/project",
    "runtime_status": "RuntimeStatus",
    "agent_counts": { /* see spec 001 */ },
    "agents": [ /* AgentSummary[] as in spec 001 */ ]
  }
}
```

**Errors (logical):**

- `MUX_INTERNAL_ERROR` if retrieving swarm status fails unexpectedly.

---

### 3.2 `_agent_detail`

Return detailed information for a single agent, plus whether this mux is currently attached to that agent.

**Logical request payload:**

```jsonc
{
  "op": "_agent_detail",
  "payload": {
    "agent_id": "nav-1",
    "max_events": 100  // optional; default server value
  }
}
```

**Mux behavior:**

- Validate `agent_id` via durable swarm membership.
- Call `mux.get_agent_detail(agent_id, max_events=max_events)`.
- This reuses the runtime's `get_agent_detail` view and adds an `attached` flag.

**Logical response payload:**

```jsonc
{
  "attached": true | false,
  "agent": {
    // Exactly the shape of agent detail from spec 001
    "agent_id": "nav-1",
    "display_name": "Navigator 1",
    "status": "AgentStatus",
    "agent_mail_identity": "...",
    "conversation_id": "...",
    "last_error": null
  },
  "events": [
    // AgentEvent[] as defined in spec 001, bounded by max_events
  ]
}
```

**Errors (logical):**

- `MUX_UNKNOWN_AGENT` if `agent_id` is not a member of the durable swarm.
- `MUX_INVALID_REQUEST` if the payload is malformed.
- `MUX_INTERNAL_ERROR` for unexpected failures.

---

### 3.3 `_attach`

Attach this external ACP session to a specific agent and begin streaming that agent's typed ACP updates.

**Logical request payload:**

```jsonc
{
  "op": "_attach",
  "payload": {
    "agent_id": "nav-1"
  }
}
```

**Mux / adapter behavior:**

1. Ensure the mux is open.
2. Validate `agent_id` against durable swarm membership.
3. Call `prepared = await mux.prepare_attach(agent_id)`.
4. If this fails (including `AgentSessionNotActive`), return an error and do **not** send a success acknowledgment.
5. If it succeeds, send the `_attach` success acknowledgment **before** activating the attachment:

   ```python
   await external_connection.send_attach_acknowledgment(
       session_id=external_session_id,
       agent_id=agent_id,
   )
   await mux.activate_attachment(prepared)
   ```

6. `activate_attachment(prepared)` starts the forwarding task and releases the forwarding gate.

**Logical response payload (on success):**

```jsonc
{
  "attached_agent_id": "nav-1"
}
```

**Ordering guarantee:**

- The adapter MUST send the `_attach` success response **before** any retained or live updates for the new attachment are delivered to the client.
- No update from the new agent may appear on the external session before the `_attach` acknowledgment.
- When switching agents, no update from the old attachment may appear after the new-agent acknowledgment.

**Errors (logical):**

- `MUX_UNKNOWN_AGENT` if `agent_id` is not in durable swarm membership.
- `MUX_STALE_ATTACHMENT` if activation is attempted with a `PreparedAttachment` token that no longer matches the current `_Attachment`.
- `MUX_INVALID_REQUEST` if the payload is malformed.
- `MUX_INTERNAL_ERROR` if the attach flow fails unexpectedly.

---

### 3.4 `_detach`

Detach this external ACP session from its current agent.

**Logical request payload:**

```jsonc
{
  "op": "_detach",
  "payload": {}
}
```

**Mux behavior:**

- Call `await mux.detach()`.
- `detach()` is **idempotent**: detaching when there is no current attachment succeeds and has no effect.
- Detach removes only this mux's subscription; other subscribers to the same `AcpSessionUpdateStream` remain active.

**Logical response payload:**

```jsonc
{
  "detached": true
}
```

**Errors (logical):**

- `MUX_INTERNAL_ERROR` only if detach fails unexpectedly.

---

## 4. Ordinary Agent-Directed Operations

All non-reserved operations (ACP-level requests that are **not** `_swarm_status`, `_agent_detail`, `_attach`, or `_detach`) are treated as agent-directed operations.

### 4.1 Preconditions

- The mux must be **open** (not closed).
- `mux.attached_agent_id` must be non-null.

If either precondition is not met, the adapter returns an error with code `"MUX_NO_ATTACHED_AGENT"`.

### 4.2 Forwarding Behavior

- The adapter forwards the ACP request to the per-agent ACP client session corresponding to `mux.attached_agent_id`.
- The ACP client session produces typed `SessionUpdate` objects into the agent's `AcpSessionUpdateStream`.
- SwarmACPMux subscribes (via `subscribe_acp_updates(agent_id)`) and forwards each `SessionUpdate` to the external ACP session using `ExternalACPConnection.session_update(session_id, update)`.
- In parallel, the runtime may summarize each update into `AgentEvent` telemetry for status APIs and logs, but that telemetry history is **not** used to drive ACP transport.

Result: the client observes a **single ordered stream** of typed ACP updates for the attached agent, including retained history and all subsequent live updates, while the runtime maintains a separate observability stream.

---

## 5. Lifecycle & Shutdown

### 5.1 External Session Closure

When the external ACP session closes:

- The Swarm ACP server adapter:
  - signals the mux to `await mux.close()`;
  - tears down the concrete ACP transport.
- The mux:
  - marks itself `_closed = True`;
  - detaches from any attached agent (cancelling the forwarding task and exiting the subscription context);
  - resolves or cancels internal waiters (including `wait_failed()`);
  - frees connection-local resources.

### 5.2 Runtime Shutdown

When the runtime shuts down:

- SwarmACPMux instances are eventually closed.
- The adapter should:
  - return a clear error or close all ACP connections;
  - avoid leaving dangling mux instances.

---

## 6. Alignment with Specs 001 and 008

- SwarmACPMux reuses the **same swarm and agent views** as the runtime control API (spec 001) for `_swarm_status` and `_agent_detail`.
- Epic 008 provides:
  - a single, typed `AcpSessionUpdateStream` per agent;
  - `ReceivedSessionUpdate` wrappers and `subscribe_acp_updates()`;
  - replay, ordering, overflow, and closure semantics.
- Epic 009 adds:
  - a connection-scoped mux per external ACP session;
  - logical reserved operations handled at the ACP boundary;
  - replay-then-live typed update delivery into ACP-facing streams, with explicit acknowledgment-before-replay guarantees.
