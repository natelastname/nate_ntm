# Data Model: nate_ntm Swarm Runtime Orchestrator

This document extracts and structures the core entities, fields, relationships, and state transitions implied by the feature spec for the nate_ntm Swarm Runtime Orchestrator.

The goal is to clarify **what data the runtime owns**, how it is persisted, and how it changes over time, without over-constraining implementation details.

## 1. Overview

The runtime manages a single **Swarm** for a single project directory. It persists metadata needed to stop and later resume that swarm and maintains transient in-memory state for active agents and their recent events.

High-level categories:

- **Persisted metadata (project-local files)**
  - Swarm-level metadata
  - Per-agent metadata
- **Transient runtime state (in-memory)**
  - Runtime daemon and event loop state
  - Per-agent status and ACP connection state
  - Agent Event Streams (recent control-protocol events)

## 2. Persisted Entities

### 2.1 SwarmMetadata

Represents the persisted, project-local description of a swarm.

**Fields (suggested):**

- `swarm_id: string`
  - Logical identifier for the swarm within the project (e.g., `default`).
- `project_path: string`
  - Absolute path to the project directory associated with this swarm.
- `agent_mail_project_id: string`
  - Identifier of the corresponding coordination project in Agent Mail.
- `created_at: datetime`
  - Timestamp when the swarm was first created.
- `last_updated_at: datetime`
  - Timestamp of the last successful metadata write.
- `config_version: string`
  - Optional version tag for the swarm configuration schema.
- `agents: list[AgentMetadata]`
  - Embedded or referenced list of per-agent metadata records (see below).
- `runtime_options: object`
  - Configuration knobs for the runtime (e.g., polling intervals, restart limits) as captured at creation time.

**Invariants:**

- `project_path` MUST match the directory where the metadata store lives (for example, `.nate_ntm/` under that path).
- At most one `SwarmMetadata` record is active for a given `project_path` per runtime instance.

### 2.2 AgentMetadata

Represents persisted configuration and identity for a single agent within the swarm.

**Fields (suggested):**

- `agent_id: string`
  - Stable identifier for the agent within the swarm.
- `display_name: string`
  - Human-friendly name for UIs.
- `role: string`
  - Optional role or specialization label (e.g., `navigator`, `implementer`).
- `agent_mail_identity: string`
  - Identifier used by Agent Mail for this agent.
- `agent_mail_credentials_ref: string`
  - Reference to credentials (e.g., key name, secret path) rather than raw secrets.
- `conversation_id: string`
  - OpenHands conversation identifier associated with this agent's ongoing work.
- `restart_policy: object`
  - Limits and backoff settings for automatic restarts.
- `last_known_status: string`
  - Snapshot of the last persisted agent status (e.g., `Idle`, `Running`, `Failed`).
- `nate_oha_config: NateOhaConfig | null`
  - Fully resolved Nate OHA configuration for this agent. When present this is treated as the source of truth for launch-time behaviour (LLM model, prompts, Agent Mail feature flags, etc.).

**Invariants:**

- `agent_id` is unique within a given `SwarmMetadata`.
- `agent_mail_identity` and `conversation_id` MUST be reused on resume to satisfy FR-009.

### 2.3 Metadata Store Layout

The ConfigOverhaul persistence model standardises on a single, project-local
file under `.nate_ntm/`:

```text
.nate_ntm/
└── swarm.json   # Single SwarmState object graph (authoritative)
```

Older layouts that used `.nate_ntm/agents/` with per-agent JSON files are no
longer written by the runtime. Any such files are ignored when loading state;
`SwarmState`/`swarm.json` is the source of truth.

## 3. Transient Runtime State

### 3.1 RuntimeState

Represents in-memory state for the running Runtime daemon.

**Fields (conceptual):**

- `swarm_metadata: SwarmMetadata`
  - Loaded from project-local storage at startup or resume.
- `agents: dict[agent_id, AgentRuntimeState]`
  - Current runtime state keyed by agent identifier.
- `event_loop: RuntimeEventLoop`
  - Handle to the event-driven scheduler/event loop.
- `system_status: string`
  - Overall runtime health (e.g., `Starting`, `Running`, `ShuttingDown`, `Stopped`, `Failed`).
- `shutdown_requested: bool`
  - Indicates a graceful shutdown has been requested.

### 3.2 AgentRuntimeState

Represents the in-memory, ephemeral state of an agent instance.

**Fields (conceptual):**

- `agent_metadata: AgentMetadata`
  - Persisted configuration and identity for the agent.
- `status: string`
  - Current lifecycle state (e.g., `Starting`, `Idle`, `Running`, `Waiting`, `Failed`).
- `subprocess_handle: object`
  - Reference to the underlying agent subprocess (PID, handle, etc.).
- `acp_connection: ACPConnection`
  - Current control-protocol connection object (ephemeral; recreated on restart/resume).
- `current_turn_id: string | null`
  - Identifier of the currently active ACP turn, if any.
- `last_error: string | null`
  - Summary of the most recent error (if the agent is in `Failed` or degraded state).
- `event_stream: AgentEventStream`
  - Handle to the agent's in-memory event stream.

### 3.3 AgentEventStream

Represents a bounded, transient stream of recent control-protocol events for a single agent.

**Conceptual structure:**

- `events: list[AgentEvent]`
  - Ordered sequence of recent events (oldest to newest), capped by a configured maximum length or memory budget.
- `max_events: int`
  - Upper bound on the number of events retained.
- `cursor_state: object`
  - Optional state used by streaming clients to resume from the last seen event.

**Behavioral notes:**

- When the stream is full and a new event arrives, the oldest event is dropped.
- The stream is not a durability mechanism; it is safe to discard entirely between runtime restarts.

### 3.4 AgentEvent

Represents an individual event in an Agent Event Stream.

**Fields (suggested):**

- `event_id: string`
  - Unique identifier for the event within the stream.
- `timestamp: datetime`
  - Time at which the event occurred or was observed.
- `agent_id: string`
  - Agent associated with the event.
- `source: string`
  - Origin of the event (e.g., `ACP`, `AgentMail`, `Runtime`, `Client`).
- `type: string`
  - Event type (e.g., `TurnStarted`, `TurnCompleted`, `TurnFailed`, `ToolCall`, `MailReceived`, `MailAcknowledged`, `ProcessExited`).
- `payload: object`
  - Event-specific payload (summarized, not necessarily the full raw data).

## 4. State Transitions

### 4.1 Runtime Lifecycle States

Runtime-level states (informal):

- `Starting` 
- `Running`
- `ShuttingDown`
- `Stopped`
- `Failed`

**Typical transitions:**

- `Starting → Running` when the event loop is initialized, agents are launched or attached, and initial Agent Mail state has been loaded.
- `Running → ShuttingDown` when a shutdown request is received via the control API or system signal.
- `ShuttingDown → Stopped` when all agents have been gracefully terminated or forcibly killed within policy.
- `Running → Failed` when a fatal unrecoverable error occurs (e.g., metadata cannot be loaded, critical dependency unavailable) and the runtime cannot continue.

### 4.2 Agent Lifecycle States

Per the spec, agents have lifecycle states such as:

- `Starting`
- `Idle`
- `Running`
- `Waiting`
- `Failed`

**Example transitions:**

- `Starting → Idle` when the agent has been launched and is ready for work.
- `Idle → Running` when the scheduler initiates a new ACP turn for the agent.
- `Running → Waiting` when the agent is blocked on an external dependency (e.g., tool call, I/O) and cannot accept new turns.
- `Running/Waiting → Idle` when the current turn completes and the agent has no immediate follow-up work.
- `Any → Failed` when the agent subprocess crashes or a hard error occurs.
- `Failed → Starting` when the runtime applies a restart policy.

### 4.3 Swarm Resume Semantics

On resume, the runtime should:

1. Load `SwarmMetadata` and `AgentMetadata` records from the project-local store.
2. For each agent:
   - Reconstruct `AgentRuntimeState` from persisted metadata.
   - Recreate a new `ACPConnection` bound to the persisted `conversation_id`.
   - Reinitialize the `AgentEventStream` as empty.
3. Poll Agent Mail for unread messages and enqueue appropriate runtime events.
4. Transition the Runtime from `Starting` to `Running` once the initial state is established.

## 5. Relationships Summary

- One **Runtime** manages exactly one **Swarm** (per process).
- One **Swarm** has many **Agent Instances**.
- Each **Agent Instance** has one **AgentMetadata** record (persisted) and one **AgentRuntimeState** (transient, when running).
- Each **Agent Instance** is associated with exactly one **Agent Mail identity** and one **OpenHands conversation**.
- Each **Agent Instance** has one **Agent Event Stream** in memory while the runtime is running.
- The **Runtime Event Loop / Scheduler** observes events across all agents and external systems and updates their runtime state and event streams accordingly.

This data model should be treated as a guide rather than a rigid schema; implementations may adjust field names and layout as long as the semantics required by the spec (especially around resume behavior, state visibility, and event buffering) are preserved.
