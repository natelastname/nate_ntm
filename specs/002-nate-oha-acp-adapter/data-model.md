# Data Model: nate_OHA ACP Production Adapter

This document describes the runtime- and persistence-level data affected by the `NateOhaAcpClient` feature. It refines how existing `SwarmMetadata` and `AgentMetadata` are used and introduces conceptual in-memory records for nate_OHA process supervision.

## 1. Swarm and Agent Metadata (Persistent)

The feature **reuses** the existing metadata definitions in `nate_ntm.runtime.metadata_store` and constrains their usage for nate_OHA–backed agents.

### 1.1 SwarmMetadata

Source: `nate_ntm.runtime.metadata_store.SwarmMetadata`

Relevant fields:

- `swarm_id: str`
  - Unique identifier for the swarm.
  - Used when deriving deterministic identifiers (for example, OpenHands threads in the legacy HTTP adapter).
- `project_path: Path`
  - Absolute path to the project directory.
  - Used by nate_OHA–related tooling to locate project-relative configuration and metadata.
- `agent_mail_project_id: str`
  - Identifier of the Agent Mail project that owns agent mailboxes for this swarm.
- `agents: Mapping[str, AgentMetadata]`
  - Mapping of `agent_id` → `AgentMetadata` (see below).
- `runtime_options: Mapping[str, Any]`
  - Free-form runtime configuration; may include adapter-selection flags at the swarm level.

**Validation rules (this feature):**

- For any swarm using `NateOhaAcpClient`, `agent_mail_project_id` **MUST** be non-empty.
- `agents` **MUST** contain all agents configured to use the production adapter; no nate_OHA–backed agent may run without a persisted `AgentMetadata` record.

### 1.2 AgentMetadata

Source: `nate_ntm.runtime.metadata_store.AgentMetadata`

Fields and semantics relevant to nate_OHA:

- `agent_id: str`
  - Primary key for an agent within a swarm.
- `display_name: str`
  - Human-friendly name; surfaced in logs and inspection APIs.
- `role: str | None`
  - Optional description of the agent’s role in the swarm.
- `agent_mail_identity: str`
  - **For nate_OHA–backed agents:**
    - The stable Agent Mail identity used by nate_OHA to read/write coordination messages.
    - **MUST** be non-empty once the agent has been created for production use.
- `agent_mail_credentials_ref: str`
  - Opaque reference to credentials or configuration used to authenticate to Agent Mail.
  - The adapter treats this as a reference (file path, secret key, etc.); interpretation is owned by the runtime and deployment environment.
- `conversation_id: str`
  - **For nate_OHA–backed agents:**
    - The persisted OpenHands conversation identifier that nate_OHA uses for the agent’s underlying work.
    - **MUST** be stable across runtime shutdown/resume (FR-005, SC-002).
    - May be empty only before the first successful conversation is established; once set, it **MUST NOT** be changed arbitrarily.
- `restart_policy: Mapping[str, Any]`
  - Policy controlling restart behavior when nate_OHA processes fail or exit.
  - Interpreted by the runtime/daemon; the adapter surfaces process outcomes.
- `last_known_status: str`
  - Snapshot status (e.g., `"Idle"`, `"Running"`, `"Failed"`).
  - Updated by the runtime in response to process and ACP events.
- `nate_oha_config: NateOhaConfig | None`
  - Fully resolved Nate OHA configuration for this agent. When present this is treated as the source of truth for launch-time behaviour (LLM model, prompts, Agent Mail settings, etc.).

**Validation rules (this feature):**

For any agent configured to use `NateOhaAcpClient` as its ACP adapter:

1. `agent_mail_identity` **MUST** be non-empty and correspond to a valid Agent Mail identity in the configured `agent_mail_project_id`.
2. `conversation_id`:
   - May be empty only before the first successful conversation is created.
   - Once established, **MUST** be reused on subsequent launches and swarm resumes.
3. `restart_policy` **SHOULD** define sensible defaults for maximum retries, backoff behavior, and permanent-failure marking; the adapter and daemon **MUST** respect this policy when deciding whether to restart a failing nate_OHA process.

## 2. In-memory Process Supervision Records

This section describes conceptual in-memory structures used by `NateOhaAcpClient` and the runtime daemon. Concrete class names and locations are left to the implementation, but the fields and relationships are normative.

### 2.1 NateOhaProcessRecord

Represents a single supervised nate_OHA subprocess attached to one agent.

- `agent_id: str`
- `pid: int | None`
- `status: Literal["starting", "running", "stopping", "terminated", "failed"]`
- `last_start_time: datetime | None`
- `last_exit_code: int | None`
- `last_error: str | None`
- `restart_count: int`

**Relationships and invariants:**

- Exactly one `NateOhaProcessRecord` per nate_OHA–backed agent at a time.
- `agent_id` **MUST** correspond to a key in `SwarmMetadata.agents`.
- When `status` is `"running"`, `pid` **MUST** be non-null and refer to a live OS process owned by the runtime.

### 2.2 NateOhaIdentityBinding (conceptual)

Logical binding between runtime metadata and the identity used by nate_OHA.

- `agent_id: str`
- `agent_mail_identity: str` (mirrors `AgentMetadata.agent_mail_identity`)
- `agent_mail_credentials_ref: str` (mirrors `AgentMetadata.agent_mail_credentials_ref`)
- `conversation_id: str` (mirrors `AgentMetadata.conversation_id`)

**Invariants (FR-005 / SC-002):**

- For any given `agent_id`, the tuple `(agent_mail_identity, conversation_id)` **MUST** remain constant across swarm shutdown and resume.
- If `conversation_id` is unset when an agent is first launched:
  - The adapter **MAY** request nate_OHA/OpenHands to create a new conversation.
  - Once created, `conversation_id` **MUST** be written back to `AgentMetadata` and persisted.

## 3. State Transitions

This feature relies on clear process and metadata state transitions.

### 3.1 nate_OHA Process State

A simplified state machine for a nate_OHA process managed by `NateOhaAcpClient`:

```text
       +-----------+       +-----------+       +-----------+
       | starting  | --->  | running   | --->  | stopping  |
       +-----------+       +-----------+       +-----------+
             |                   |                   |
             v                   v                   v
          +------+           +--------+          +----------+
          |failed|           |terminated|        |terminated|
          +------+           +--------+          +----------+
```

- **starting → running**: process has been spawned and passes basic health checks (per `NATE_OHA_GUIDE.md`).
- **starting → failed**: process fails to spawn or fails initial health checks; error surfaced via runtime events.
- **running → stopping**: runtime initiates a graceful shutdown (e.g., on swarm shutdown).
- **running → failed**: unexpected crash or repeated unresponsive behavior; restart policy is consulted.
- **stopping → terminated**: process exits cleanly or after timeout/forced kill.

### 3.2 AgentMetadata and Identity Lifecycle

For each nate_OHA–backed agent:

1. **Creation**
   - `AgentMetadata` created with:
     - `agent_mail_identity` set to a valid Agent Mail identity.
     - `conversation_id` empty.
   - The runtime configured with `RuntimeConfig.acp_adapter` (or global `adapter_mode`) resolving to `AdapterKind.REAL` so that `NateOhaAcpClient` is used for production.

2. **First Launch**
   - `NateOhaAcpClient` launches nate_OHA using CLI/env derived from `AgentMetadata` and `SwarmMetadata`.
   - If nate_OHA/OpenHands creates a new conversation, the resulting conversation ID is persisted back into `AgentMetadata.conversation_id`.

3. **Steady State**
   - On subsequent turns, `NateOhaAcpClient` ensures nate_OHA reconnects using the persisted `conversation_id` and `agent_mail_identity`.

4. **Shutdown and Resume**
   - On controlled shutdown, process state transitions to `stopping`/`terminated`; `AgentMetadata` and `SwarmMetadata` are flushed.
   - On resume, `NateOhaAcpClient` reads `AgentMetadata` and launches nate_OHA so that the same `agent_mail_identity` and `conversation_id` are used.
   - No new conversation is created unless explicitly configured via policy.

These transitions enforce the requirements in FR-002, FR-005, and FR-006 while keeping persistent state minimal and delegating long-term history and coordination to Agent Mail and OpenHands.
