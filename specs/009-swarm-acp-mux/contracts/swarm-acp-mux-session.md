# Contract: SwarmACPMux ACP Session Behavior (Feature 008)

This contract defines how SwarmACPMux and the Swarm ACP server adapter expose the nate_ntm swarm to external ACP clients at the **logical operation** level.

It builds on:

- the runtime control API contracts in
  `specs/001-swarm-runtime-orchestrator/contracts/runtime-api.md` (for swarm and agent views);
- the SwarmACPMux class and methods defined in `specs/008-swarm-acp-mux/spec.md`.

The goal is to specify:

- the logical swarm-control operations (`_swarm_status`, `_agent_detail`, `_attach`, `_detach`);
- how those map to mux methods and runtime behavior;
- error codes and ordering guarantees;
- without over-committing to a specific ACP SDK representation (e.g., `SessionUpdate` vs. method objects).

---

## 1. General Model

- One **SwarmACPMux** is created per external ACP session.
- Each mux is attached to **at most one agent at a time**.
- The Swarm ACP server adapter owns the concrete ACP connection and protocol:
  - it decodes ACP requests/notifications into **logical operations**;
  - it calls mux methods and runtime helpers;
  - it encodes results/errors back into ACP responses.

### 1.1 Error Codes (Logical)

The adapter maps mux/domain errors to a small set of stable, string-valued error codes:

- `"MUX_NO_ATTACHED_AGENT"`
  - Client attempted an agent-directed operation (prompt, interrupt, etc.) with no attached agent.
- `"MUX_UNKNOWN_AGENT"`
  - Requested `agent_id` is not part of the durable swarm membership.
- `"MUX_INVALID_REQUEST"`
  - Malformed or missing fields in a reserved-operation request.
- `"MUX_INTERNAL_ERROR"`
  - Unexpected failure in mux or adapter; details logged server-side.

The on-the-wire error encoding (e.g., JSON-RPC error object vs. ACP-specific error envelope) is determined by the ACP SDK; this contract fixes only the **logical codes and meanings**.

---

## 2. Reserved Swarm-Control Operations

The following operations are **reserved swarm-level controls** that the adapter must recognize and route to SwarmACPMux and/or RuntimeDaemon. On the wire, they MAY be expressed as ACP extension methods or notifications; this contract describes them in terms of logical requests and responses.

### 2.1 `_swarm_status`

Return swarm-level status and mux attachment information for the current session.

**Logical request:**

```jsonc
{
  "op": "_swarm_status",
  "payload": {}
}
```

**Mux behavior:**

- Call `mux.get_swarm_status()`.
- This in turn calls `daemon.get_swarm_status()` (or equivalent) and wraps the result.

**Logical response payload:**

```jsonc
{
  "attached_agent_id": "agent-1" | null,
  "swarm": {
    // Exactly the shape of swarm.get_overview result
    "swarm_id": "default",
    "project_path": "/abs/path/to/project",
    "runtime_status": "RuntimeStatus",
    "agent_counts": { /* see spec 001 */ },
    "agents": [ /* AgentSummary[] as in spec 001 */ ]
  }
}
```

Errors (logical):

- `MUX_INTERNAL_ERROR` if `get_swarm_status` fails.

### 2.2 `_agent_detail`

Return detailed information for a single agent, plus whether this mux is currently attached to that agent.

**Logical request:**

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

- Validate `agent_id` via mux.
- Call `mux.get_agent_detail(agent_id, max_events=max_events)`.
- This reuses the runtime’s `agent.get_detail` view and adds an `attached` flag.

**Logical response payload:**

```jsonc
{
  "attached": true | false,
  "agent": {
    // Exactly the shape of agent.get_detail.agent from spec 001
    "agent_id": "nav-1",
    "display_name": "Navigator 1",
    "status": "AgentStatus",
    "agent_mail_identity": "...",
    "conversation_id": "...",
    "last_error": null
  },
  "events": [
    // AgentEvent[] as defined in spec 001, bounded by max_events
    {
      "event_id": "e-123",
      "timestamp": "ISO-8601",
      "agent_id": "nav-1",
      "source": "ACP",
      "type": "TurnStarted",
      "payload": { "turn_id": "t-001" }
    }
  ]
}
```

Errors (logical):

- `MUX_UNKNOWN_AGENT` if `agent_id` is not a member of the durable swarm.
- `MUX_INVALID_REQUEST` if the payload is malformed.
- `MUX_INTERNAL_ERROR` for unexpected failures.

### 2.3 `_attach`

Attach this external ACP session to a specific agent and begin streaming that agent’s events.

**Logical request:**

```jsonc
{
  "op": "_attach",
  "payload": {
    "agent_id": "nav-1"
  }
}
```

**Mux behavior:**

- Ensure mux is open.
- Validate `agent_id` via durable swarm membership.
- If already attached to `agent_id` with a live forwarding task, treat as a no-op and return success.
- Otherwise:
  - `await mux.detach()`;
  - `await mux.attach(agent_id)`;
  - the mux starts a new `_forward_agent_events(agent_id)` task internally.

**Logical response payload:**

```jsonc
{
  "attached_agent_id": "nav-1"
}
```

**Ordering guarantee:**

- The adapter MUST send the `_attach` success response **before** any replayed events for the new attachment are delivered to the client.
- Events delivered after this acknowledgment belong to the new attachment.

Errors (logical):

- `MUX_UNKNOWN_AGENT` if `agent_id` is not in durable `SwarmState.agents`.
- `MUX_INVALID_REQUEST` if the payload is malformed.
- `MUX_INTERNAL_ERROR` if the attach flow fails unexpectedly.

### 2.4 `_detach`

Detach this external ACP session from its current agent.

**Logical request:**

```jsonc
{
  "op": "_detach",
  "payload": {}
}
```

**Mux behavior:**

- Call `await mux.detach()`.
- This is **idempotent**: detaching when there is no attachment succeeds and has no effect.

**Logical response payload:**

```jsonc
{
  "detached": true
}
```

Errors (logical):

- `MUX_INTERNAL_ERROR` only if detach fails unexpectedly.

---

## 3. Ordinary Agent-Directed Operations

All other operations (ACP-level requests that are **not** `_swarm_status`, `_agent_detail`, `_attach`, or `_detach`) are treated as agent-directed operations.

### 3.1 Preconditions

- The mux must be **open** (not closed).
- `mux.attached_agent_id` must be non-null.

If either precondition is not met, the adapter returns:

```jsonc
{
  "error": {
    "code": "MUX_NO_ATTACHED_AGENT",
    "message": "No agent is attached to this session. Use _attach first.",
    "data": {}
  }
}
```

### 3.2 Forwarding Behavior

- The adapter forwards the ACP request to the per-agent ACP client session corresponding to `mux.attached_agent_id`.
- The ACP client session produces protocol-level updates (e.g., `SessionUpdate` objects) into the agent’s ACP update stream.
- SwarmACPMux subscribes to that ACP update stream for the attached agent and forwards each `SessionUpdate` (or a lightly transformed equivalent) to the external ACP session.
- In parallel, the runtime may summarize each `SessionUpdate` into an `AgentEvent` and append it to the agent’s `AgentEvent` history for observability, but that telemetry history is **not** used to drive ACP transport.

Result: the client observes a **single ordered stream** of protocol updates for the attached agent, including replayed history and all subsequent live updates, while the runtime maintains a separate, possibly lossy telemetry history for status and diagnostics.

---

## 4. Lifecycle & Shutdown

### 4.1 External Session Closure

When the external ACP session closes:

- The Swarm ACP server adapter:
  - signals the mux to `await mux.close()`;
  - tears down the concrete ACP transport.
- The mux:
  - marks itself `_closed = True`;
  - detaches from any attached agent (cancelling the forwarding task);
  - frees connection-local resources.

### 4.2 Runtime Shutdown

When the runtime shuts down:

- SwarmACPMux instances are eventually closed.
- The adapter should:
  - return a clear error or close all ACP connections;
  - avoid leaving dangling mux instances.

---

## 5. Alignment with Specs 001 and 008

- SwarmACPMux uses the **same swarm and agent views** as the runtime control API (spec 001) for `_swarm_status` and `_agent_detail`.
- Feature 008 adds:
  - a connection-scoped mux per external ACP session;
  - logical reserved operations handled at the ACP boundary;
  - replay-capable, per-agent event delivery into ACP-facing streams.

This contract is intentionally ACP-SDK-agnostic at the wire level; it focuses on the logical operations, payloads, and ordering guarantees needed to write tests and clients that behave correctly across runtime and ACP integration changes.
