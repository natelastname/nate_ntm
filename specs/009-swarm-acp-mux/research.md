# Research & Design Decisions: SwarmACPMux (Epic 009)

This document records the key design decisions for SwarmACPMux based on the Epic 009 feature spec and the existing runtime orchestrator contracts (spec 001). It is the Phase 0 output for `/speckit.plan`.

Each section follows the pattern:

- **Decision**
- **Rationale**
- **Alternatives considered**

---

## 1. Role and Boundaries of SwarmACPMux

**Decision**: Implement `SwarmACPMux` as a *connection-scoped* router over the typed ACP session streaming layer from Epic 008.

- One mux instance per external ACP session.
- Each mux is attached to **at most one agent at a time**, but may switch attachments over its lifetime.
- The mux owns only connection-local state:
  - `attached_agent_id`;
  - the current `_Attachment` record (retained subscription + forwarding task);
  - a `_lifecycle_lock` to serialize mutating operations;
  - a `_failure` future representing the first fatal forwarding error;
  - `_closed` flag.
- The mux **does not own**:
  - agent lifecycle or ACP connections to agents;
  - swarm metadata or persistent state;
  - ACP wire encoding/decoding;
  - per-agent replay buffers or subscriber queues.

**Rationale**:

- Keeps per-agent ACP sessions and typed `AcpSessionUpdateStream` behavior in the Epic 008 layer.
- Keeps swarm metadata and event history in `RuntimeDaemon` / swarm state, as defined in spec 001.
- Avoids creating a second mini-runtime; the mux is a narrow, connection-scoped coordinator.

**Alternatives considered**:

- **Heavier mux owning ACP connections and agent lifecycle**
  - Rejected: would duplicate responsibilities of `NateOhaAcpClient` and `RuntimeDaemon`, complicating failure handling and testing.
- **Global mux shared across sessions**
  - Rejected: breaks the one-mux-per-external-session invariant and makes it harder to reason about attachment state and error propagation for individual clients.

---

## 2. Typed ACP Update Path vs. Telemetry

**Decision**: SwarmACPMux consumes typed ACP updates exclusively via `subscribe_acp_updates()` and forwards the underlying `SessionUpdate` objects unchanged. `AgentEvent` remains an observability-only model.

- The canonical internal path is:

  ```text
  ACP SDK callback
      ↓
  NateOhaAcpClient / AcpAgentSession
      ↓
  AcpSessionUpdateStream (per agent)
      ↓
  subscribe_acp_updates(agent_id)
      ↓
  SwarmACPMux
      ↓
  ExternalACPConnection.session_update(session_id, update)
  ```

- `ReceivedSessionUpdate` metadata (sequence, timestamps) is used internally but is **not** forwarded over ACP; only the underlying `SessionUpdate` is.
- Any `AgentEvent` telemetry derived from ACP activity is emitted by the runtime daemon / observability pipeline, not by the mux.

**Rationale**:

- Ensures there is a single, typed ACP streaming abstraction (Epic 008) responsible for replay, ordering, overflow, and closure semantics.
- Keeps the runtime core (`AgentEvent`, status APIs) ACP-agnostic and JSON-serializable.
- Avoids introducing a second buffer or subscription system for ACP updates.

**Alternatives considered**:

- **Driving ACP transport from `AgentEvent` history**
  - Rejected: would require reconstructing protocol-level updates from lossy telemetry, and would conflate observability with transport.
- **Allowing the mux to publish its own `AgentEvent` records**
  - Rejected: better to keep telemetry responsibilities in the daemon and dedicated observability components; the mux stays focused on routing.

---

## 3. Attachment Transaction (prepare / acknowledge / activate)

**Decision**: Model attachment as a three-stage transaction:

1. `prepare_attach(agent_id)` establishes the concrete ACP subscription and records an `_Attachment`, but does **not** start forwarding.
2. The outer adapter sends the `_attach` acknowledgment over ACP.
3. `activate_attachment(prepared)` starts the forwarding task and releases its gate.

A convenience `attach(agent_id, acknowledge=...)` helper is allowed **only** if it preserves this ordering by calling `prepare_attach()`, then `acknowledge()`, then `activate_attachment()`.

**Rationale**:

- Makes "acknowledgment before replay" a structural guarantee rather than a scheduling accident.
- Ensures that if `subscribe_acp_updates()` fails (including `AgentSessionNotActive`), the adapter never sends an attach success acknowledgment.
- Prevents stale acknowledgments from activating outdated attachments via the `PreparedAttachment.token` identity check.

**Alternatives considered**:

- **Single-step `attach()` that both prepares and activates**
  - Rejected: does not give the adapter a clean point to send the acknowledgment before replayed events begin.
- **Starting forwarding before acknowledgment and relying on buffering at the adapter**
  - Rejected: harder to reason about ordering guarantees and error handling; the mux should not own additional buffering.

---

## 4. Concurrency & Identity: `_lifecycle_lock`, `_Attachment`, and Tokens

**Decision**: Serialize lifecycle transitions with `_lifecycle_lock` and use concrete `_Attachment` identity (plus a token) to avoid stale completion and activation.

- `prepare_attach()`, `activate_attachment()`, `detach()`, and `close()` acquire `_lifecycle_lock` before mutating shared state.
- `_Attachment` records:
  - `agent_id`;
  - the retained subscription context manager;
  - the `AsyncIterator[ReceivedSessionUpdate]`;
  - a `forwarding_enabled` event;
  - the forwarding `Task`.
- `PreparedAttachment` carries:
  - a token that is checked by `activate_attachment()` to ensure it still refers to the current `_Attachment`; and
  - a `newly_prepared` flag that records whether this request created a fresh `_Attachment` or reused an existing healthy one, so that `abort_attachment(prepared)` can roll back only truly new candidates while leaving idempotent same-agent attachments intact when acknowledgment fails.
- `_attachment_finished()` verifies identity before clearing attachment state when a forwarding task completes.

**Rationale**:

- Deterministic behavior when attachments, detaches, and close operations race.
- Prevents an old forwarding task from clearing a newer attachment.
- Ensures that only the current attachment can be activated and only once.

**Alternatives considered**:

- **Lock-free lifecycle with best-effort checks**
  - Rejected: too hard to reason about in the presence of concurrent attaches/detaches and task completion races.
- **Identity based solely on `agent_id`**
  - Rejected: does not distinguish between two different ACP sessions for the same agent or between old and new attachments.

---

## 5. Failure Observation and `wait_failed()`

**Decision**: Use a single `_failure` future to represent the first fatal forwarding error, and expose it via `wait_failed()`.

- `_run_forwarding()` calls `_report_failure(exc)` on unexpected exceptions from either:
  - consuming the ACP subscription iterator; or
  - writing to `ExternalACPConnection.session_update()`.
- `_report_failure()` completes `_failure` **exceptionally** exactly once.
- `wait_failed()` awaits `_failure`:
  - if a fatal forwarding failure occurs, it re-raises that exception;
  - if the mux is closed cleanly while `_failure` is still pending, `close()` cancels `_failure` and any pending `wait_failed()` calls are cancelled.
- Normal task cancellation (from `detach()` / `close()`), and clean ACP stream exhaustion are **not** treated as failures and do not complete `_failure`.

**Rationale**:

- Gives the outer connection handler a single, explicit point to observe forwarding failures.
- Fits naturally into structured concurrency patterns where one task waits on `mux.wait_failed()` while another serves inbound requests, and the adapter treats them as a first-completion race: whichever finishes first (normal completion or failure) cancels the other and triggers connection cleanup.

**Alternatives considered**:

- **Let detached background task exceptions bubble through `asyncio` logging only**
  - Rejected: too easy to miss failures; makes it harder to implement robust adapters.
- **Track per-attachment failures only**
  - Rejected: the external session semantics are simpler when there is exactly one failure terminal state for the mux.

---

## 6. Reserved Swarm-Control Operations

**Decision**: Treat `_attach`, `_detach`, `_swarm_status`, and `_agent_detail` as logical swarm-control operations handled at the adapter/mux boundary, not as special `SessionUpdate` names.

- The Swarm ACP server adapter:
  - decodes reserved controls from ACP requests/notifications;
  - calls the appropriate mux method (`prepare_attach`/`activate_attachment` or `attach`, `detach`, `get_swarm_status`, `get_agent_detail`);
  - translates results and domain errors back into protocol-level responses.
- The mux itself exposes Python methods only and is not tied to a specific ACP SDK wire representation.

**Rationale**:

- Keeps ACP protocol concerns localized in the adapter.
- Matches the fact that `SessionUpdate` models outbound updates, while reserved controls are inbound operations.
- Avoids constraining the design to a particular ACP extension encoding.

**Alternatives considered**:

- **Encoding reserved controls as special `SessionUpdate` names or variants**
  - Rejected: conflates inbound control with outbound update transport and would couple mux semantics to a particular SDK representation.

---

## 7. Error Model and External Codes

**Decision**: Use a small, explicit error hierarchy for mux-level domain failures and let the adapter map them to ACP-visible error codes.

Representative error classes from the spec:

- `SwarmACPMuxError` (base)
- `SwarmACPMuxClosedError`
- `UnknownAgentError`
- `NoAttachedAgentError`
- `StaleAttachmentError`
- `UnsupportedReservedUpdateError`

**Rationale**:

- Gives tests and adapter logic clear, domain-specific failure modes.
- Keeps protocol-level error encoding (strings, enums, error envelopes) in the adapter.
- Allows clean mapping to stable error codes like `MUX_NO_ATTACHED_AGENT`, `MUX_UNKNOWN_AGENT`, etc.

**Alternatives considered**:

- **Surfacing raw exceptions directly at the ACP layer**
  - Rejected: leaks internal details and makes protocol behavior unstable across refactors.
- **Using only generic `RuntimeError` subclasses without domain meaning**
  - Rejected: harder to test and reason about.
