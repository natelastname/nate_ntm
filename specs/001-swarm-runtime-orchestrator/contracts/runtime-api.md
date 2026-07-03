# Contract: nate_ntm Runtime Control API (MVP)

This document defines the **MVP contract** for the nate_ntm runtime control API.

The goal is to:

- Provide a stable, localhost-only interface for CLI/TUI/web clients.
- Reflect the architectural decision that **only the Runtime owns ACP connections**.
- Keep the contract small but sufficient to support the MVP user stories:
  - Start and monitor a swarm
  - Resume a previous swarm
  - Inspect a single agent in detail

The transport is assumed to be **JSON-RPC-style** over a local TCP socket, but the shapes below can be adapted to an equivalent request/response protocol if needed.

> NOTE: All examples below use JSON for clarity. Exact error codes and additional fields (e.g., pagination, filtering) can be refined during implementation without changing the core responsibilities.

---

## 1. General Conventions

- All methods are callable only from `localhost` in the MVP.
- All methods are **synchronous requests** that return a response payload or an error.
- Notifications/streaming are modeled via a subscription mechanism described below.
- Errors should include a machine-readable `code` and a human-readable `message`.

### 1.1 Common Types

```jsonc
// AgentStatus
"Starting" | "Idle" | "Running" | "Waiting" | "Failed"

// RuntimeStatus
"Starting" | "Running" | "ShuttingDown" | "Stopped" | "Failed"

// AgentSummary
{
  "agent_id": "string",
  "display_name": "string",
  "status": "AgentStatus",
  "has_unread_mail": true,
  "last_error": "string | null"
}

// AgentEvent
{
  "event_id": "string",
  "timestamp": "ISO-8601 datetime",
  "agent_id": "string",
  "source": "ACP" | "AgentMail" | "Runtime" | "Client",
  "type": "string",          // e.g., "TurnStarted", "TurnCompleted", "TurnFailed"
  "payload": { "...": "..." } // summarized details
}
```

---

## 2. Lifecycle Methods

### 2.1 `runtime.get_status`

Return high-level status of the Runtime and Swarm.

**Request:**

```json
{
  "jsonrpc": "2.0",
  "method": "runtime.get_status",
  "params": {},
  "id": 1
}
```

**Result:**

```json
{
  "status": "RuntimeStatus",
  "project_path": "/abs/path/to/project",
  "swarm_id": "default",
  "agent_counts": {
    "total": 10,
    "starting": 1,
    "idle": 6,
    "running": 2,
    "waiting": 0,
    "failed": 1
  }
}
```

### 2.2 `runtime.start_swarm`

Start a new swarm (or resume if metadata already exists) for the current project directory.

**Request:**

```json
{
  "jsonrpc": "2.0",
  "method": "runtime.start_swarm",
  "params": {
    "mode": "create" | "resume"
  },
  "id": 2
}
```

**Result:**

```json
{
  "swarm_id": "default",
  "status": "RuntimeStatus",
  "agent_counts": { /* same shape as runtime.get_status */ }
}
```

### 2.3 `runtime.shutdown`

Request a graceful shutdown of the Runtime and its swarm.

**Request:**

```json
{
  "jsonrpc": "2.0",
  "method": "runtime.shutdown",
  "params": {
    "timeout_seconds": 30
  },
  "id": 3
}
```

**Result:**

```json
{
  "accepted": true,
  "status": "ShuttingDown"
}
```

Errors should be returned if shutdown is already in progress or the runtime is not running.

---

## 3. Swarm and Agent Introspection

### 3.1 `swarm.get_overview`

Return a snapshot of swarm-level state and agent summaries.

**Request:**

```json
{
  "jsonrpc": "2.0",
  "method": "swarm.get_overview",
  "params": {},
  "id": 4
}
```

**Result:**

```json
{
  "swarm_id": "default",
  "project_path": "/abs/path/to/project",
  "runtime_status": "RuntimeStatus",
  "agent_counts": { /* as above */ },
  "agents": [
    {
      "agent_id": "nav-1",
      "display_name": "Navigator 1",
      "status": "Running",
      "has_unread_mail": true,
      "last_error": null
    }
    // ...
  ]
}
```

### 3.2 `agent.get_detail`

Return detailed information for a single agent, including recent events.

**Request:**

```json
{
  "jsonrpc": "2.0",
  "method": "agent.get_detail",
  "params": {
    "agent_id": "nav-1",
    "max_events": 100
  },
  "id": 5
}
```

**Result:**

```json
{
  "agent": {
    "agent_id": "nav-1",
    "display_name": "Navigator 1",
    "status": "Running",
    "agent_mail_identity": "...",
    "conversation_id": "...",
    "last_error": null
  },
  "events": [
    {
      "event_id": "e-123",
      "timestamp": "2026-07-03T12:34:56Z",
      "agent_id": "nav-1",
      "source": "ACP",
      "type": "TurnStarted",
      "payload": { "turn_id": "t-001" }
    }
    // ...
  ]
}
```

Errors should be returned if `agent_id` is unknown.

---

## 4. Event Streaming (Subscriptions)

For live dashboards and inspection views, clients need to receive events as they occur.

### 4.1 `events.subscribe`

Subscribe to runtime/agent events.

**Request:**

```json
{
  "jsonrpc": "2.0",
  "method": "events.subscribe",
  "params": {
    "agent_ids": ["nav-1", "impl-1"],
    "include_runtime": true
  },
  "id": 6
}
```

**Result (subscription handle):**

```json
{
  "subscription_id": "sub-001"
}
```

### 4.2 Event notifications

Events are delivered as server-initiated JSON-RPC notifications (or the closest equivalent in the chosen framework):

```json
{
  "jsonrpc": "2.0",
  "method": "events.notify",
  "params": {
    "subscription_id": "sub-001",
    "event": {
      "event_id": "e-124",
      "timestamp": "2026-07-03T12:35:01Z",
      "agent_id": "nav-1",
      "source": "ACP",
      "type": "TurnCompleted",
      "payload": { "turn_id": "t-001", "success": true }
    }
  }
}
```

### 4.3 `events.unsubscribe`

Terminate a subscription.

**Request:**

```json
{
  "jsonrpc": "2.0",
  "method": "events.unsubscribe",
  "params": {
    "subscription_id": "sub-001"
  },
  "id": 7
}
```

**Result:**

```json
{
  "unsubscribed": true
}
```

---

## 5. Error Handling (MVP)

Errors should follow a simple, consistent structure:

```json
{
  "jsonrpc": "2.0",
  "error": {
    "code": 1001,
    "message": "Agent not found",
    "data": {
      "agent_id": "unknown-agent"
    }
  },
  "id": 5
}
```

Recommended error code ranges (non-exhaustive):

- `1000–1099`: Invalid parameters or unknown identifiers.
- `1100–1199`: Runtime state conflicts (e.g., shutdown in progress, swarm not running).
- `1200–1299`: Integration errors (Agent Mail, ACP, subprocess failures) surfaced to clients.

---

## 6. Alignment with Spec and MVP Scope

This contract is intentionally minimal and focused on:

- Making it possible to **start, resume, and shut down** the swarm.
- Allowing clients to **observe swarm and agent status**.
- Enabling **detailed inspection and live event streaming** for individual agents.

Features explicitly **out of scope for the MVP** (but possible future extensions):

- Fine-grained work assignment or task management APIs.
- Remote (non-localhost) access and multi-user authentication/authorization.
- Administrative operations beyond this feature (e.g., global configuration management for multiple swarms).

Implementations should adhere to these shapes closely enough that clients can be written against this document, with room for additive fields as needed.
