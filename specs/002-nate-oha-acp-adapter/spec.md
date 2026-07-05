# Feature Specification: nate_OHA ACP Production Adapter

**Feature Branch**: `[002-nate-oha-acp-adapter]`

**Created**: 2026-07-05

**Status**: Draft

**Input**: User description: "Read CONOP_1.md and generate a specification." Derived from `CONOP_1.md` (Concept of Operations).

## User Scenarios & Testing *(mandatory)*

<!--
  IMPORTANT: User stories should be PRIORITIZED as user journeys ordered by importance.
  Each user story/journey must be INDEPENDENTLY TESTABLE - meaning if you implement just ONE of them,
  you should still have a viable MVP (Minimum Viable Product) that delivers value.

  Assign priorities (P1, P2, P3, etc.) to each story, where P1 is the most critical.
  Think of each story as a standalone slice of functionality that can be:
  - Developed independently
  - Tested independently
  - Deployed independently
  - Demonstrated to users independently
-->

### User Story 1 - Run a swarm on nate_OHA (Priority: P1)

An operator wants to start a nate_ntm swarm where every managed agent is backed by a supervised nate_OHA ACP instance, without manually wiring or supervising individual agent runtimes.

**Why this priority**: This is the core value of the adapter: it turns nate_OHA into the production ACP runtime for nate_ntm while preserving the existing swarm architecture and operator experience. Without reliable swarm startup on nate_OHA, the rest of the integration does not matter.

**Independent Test**: From a project configured to use the production NateOhaAcpClient adapter, an operator can start a swarm and see that all agents are running on nate_OHA, exposed through existing runtime APIs, with no code or UX changes required in clients.

**Acceptance Scenarios**:

1. **Given** a project with agents configured to use the production NateOhaAcpClient, **when** the operator starts a swarm, **then** the runtime launches a nate_OHA ACP process for each managed agent and reports a healthy swarm state through the runtime API.
2. **Given** a running swarm using NateOhaAcpClient, **when** the scheduler detects new eligible work for agents from Agent Mail, **then** nate_OHA processes execute turns for those agents and the swarm continues to appear healthy in runtime status.

---

### User Story 2 - Preserve agent identity and conversation continuity (Priority: P2)

An operator wants to be able to shut down and later resume a swarm that uses nate_OHA, without losing agent identities, Agent Mail linkage, or continuity of the underlying OpenHands conversations that power agent execution.

**Why this priority**: Long-running swarms must survive runtime restarts. If agent identities or conversations are reset when switching to nate_OHA, existing coordination, mailboxes, and history become unusable.

**Independent Test**: After cleanly shutting down and later resuming a swarm that uses NateOhaAcpClient, every agent resumes with the same Agent Mail identity and the same OpenHands conversation identifier, and new work continues from the previous coordination state.

**Acceptance Scenarios**:

1. **Given** a running swarm whose agents are using nate_OHA, **when** the operator performs a controlled shutdown and later resumes the same swarm, **then** each agent is relaunched on nate_OHA with the same Agent Mail identity and the same OpenHands conversation identifier it used before shutdown.
2. **Given** unread Agent Mail messages at the time of shutdown, **when** the swarm is resumed, **then** those messages remain available and eligible agents are scheduled for new turns on nate_OHA without duplication of previously completed work.

---

### User Story 3 - Observe nate_OHA-backed agents through existing runtime APIs (Priority: P3)

An operator or client application wants to observe and debug nate_OHA-backed agents using the same runtime APIs and event streams already defined for the swarm orchestrator, without talking directly to nate_OHA.

**Why this priority**: Operators and tools should not need to learn a new API or connect directly to nate_OHA in order to understand what agents are doing. The adapter must preserve the existing nate_ntm observability model.

**Independent Test**: From a client that only talks to nate_ntm, an operator can inspect nate_OHA-backed agents, see recent events, and subscribe to live updates, with no knowledge of nate_OHA internals.

**Acceptance Scenarios**:

1. **Given** a running swarm using the NateOhaAcpClient adapter, **when** a client inspects an agent via the runtime API, **then** the client sees agent status and recent events that reflect what nate_OHA is doing for that agent.
2. **Given** an open agent inspection view for a nate_OHA-backed agent, **when** new events occur in nate_OHA (such as turn completions, tool calls, or errors), **then** those events are surfaced through AgentEventStream and runtime subscriptions with acceptable latency.

---

### Edge Cases

- What happens if the nate_OHA executable is not available on the PATH or fails basic self-checks when the runtime attempts to start an agent?
- How does the adapter behave if per-agent Agent Mail identities or credentials are missing, invalid, or revoked when a nate_OHA instance is launched?
- What happens when a nate_OHA process crashes repeatedly on startup or mid-turn for a given agent while the swarm is running?
- How does the system behave if the runtime is shut down while nate_OHA processes are mid-turn, or if shutdown timeouts are exceeded?
- What happens when the runtime resumes a swarm but the on-disk metadata for one or more nate_OHA-backed agents is incomplete, corrupted, or refers to an incompatible nate_OHA configuration?
- How does the adapter handle backpressure or overload if many agents become active at once and nate_OHA processes contend for local resources (CPU, memory, file descriptors)?

## Requirements *(mandatory)*

<!--
  ACTION REQUIRED: The content in this section represents placeholders.
  Fill them out with the right functional requirements.
-->

### Functional Requirements

- **FR-001**: The runtime MUST provide a production NateOhaAcpClient adapter that implements the shared BaseAcpClient abstraction. NateOhaAcpClient becomes the canonical production implementation of BaseAcpClient.
- **FR-002**: For every managed agent configured to use the production adapter, the runtime MUST launch, supervise, and terminate a dedicated nate_OHA ACP process for that agent and associate it with that agent's runtime metadata.
- **FR-003**: The adapter MUST launch nate_OHA using the process contract defined in `NATE_OHA_GUIDE.md`, including Agent Mail project, agent identity, token or credential reference, working directory, and, where applicable, the persisted OpenHands conversation identifier.
- **FR-004**: For each nate_OHA-backed agent, the adapter MUST apply this contract consistently so that nate_OHA can reconnect to the correct Agent Mail mailbox and underlying OpenHands conversation without relying on undocumented flags or environment variables.
- **FR-005**: When the runtime shuts down a swarm and later resumes it, the adapter MUST recreate nate_OHA processes such that each agent reuses the same Agent Mail identity and the same persisted OpenHands conversation identifier, so that the underlying OpenHands session resumes rather than creating a new conversation.
- **FR-006**: The adapter MUST supervise the lifecycle of each nate_OHA process, including detecting startup failures, abnormal exits, and hangs, and MUST surface these conditions to the runtime as explicit events for policy-driven restart and failure handling.
- **FR-007**: The adapter MUST treat a single canonical event source from nate_OHA (for example, the ACP event stream) as authoritative for agent-level events, and MUST expose those events (including turn completions, errors, and other relevant signals) into the existing runtime event pipeline so they appear through `AgentEventStream`, `agent.get_detail`, and runtime event subscription APIs in a form consistent with other ACP adapters.
- **FR-008**: The runtime MUST allow operators or configuration to select ACP adapters in a way that is unambiguous between development and production: FakeAcpClient is used for fake/dev/test mode, and NateOhaAcpClient is used for production mode. As part of this feature, the experimental OpenHandsAcpClient MUST be removed or retired from production use and MUST NOT be exposed as a selectable production adapter.
- **FR-009**: The runtime MUST expose operational metadata for agents managed by NateOhaAcpClient (such as current nate_OHA process status, last restart time, and recent failures) through its existing inspection and monitoring APIs, using signals surfaced by the adapter.
- **FR-010**: If clean integration requires additional capabilities from nate_OHA beyond what it currently exposes, the specification MUST define those capabilities at the interface level (for example, additional flags, configuration options, or event types) so that they can be implemented on the nate_OHA side without changing nate_ntm's overall runtime model.
- **FR-011**: The adapter MUST define and document the complete process launch contract for nate_OHA, including at minimum: the executable name, command-line arguments, required environment variables, working directory expectations, startup readiness detection, shutdown procedure, timeout behavior, and restart behavior.

- **FR-012**: The runtime MUST interact with managed nate_OHA instances exclusively through NateOhaAcpClient. NateOhaAcpClient owns the complete lifecycle of each managed nate_OHA subprocess, including creation, supervision, and termination.
- **FR-013**: NateOhaAcpClient MUST verify that the installed nate_OHA executable satisfies the minimum supported interface or version (for example via `nate_OHA --version` or another documented self-check command) before attempting to launch managed agents, and MUST fail with a clear diagnostic if the version is incompatible. This verification is part of the nate_OHA process launch contract.


### Interface Contract: BaseAcpClient

BaseAcpClient is no longer treated as a thin conversation/turn ID helper. For this feature, it is explicitly the runtime-facing contract for ACP-backed agent execution.

**Required operations (per-implementation contract)**

Any BaseAcpClient implementation (including FakeAcpClient and NateOhaAcpClient) MUST provide the following methods:

- `ensure_conversation(agent_id: str) -> str`
  - Ensure an ACP conversation exists for `agent_id` and return its opaque identifier.
- `start_agent(agent_id: str, *, metadata: AgentMetadata) -> None`
  - Launch or attach to the ACP runtime instance backing `agent_id`, using the agent's persisted metadata and the swarm configuration.
- `start_turn(agent_id: str, prompt: str | None = None) -> str`
  - Initiate a new unit of ACP work (a "turn") for `agent_id` and return a turn/run identifier.
- `stop_agent(agent_id: str, *, timeout: float) -> None`
  - Request a graceful stop of the ACP runtime backing `agent_id` and enforce a bounded timeout, escalating according to policy on timeout.
- `get_status(agent_id: str) -> AcpAgentStatus`
  - Report the current ACP/runtime status for `agent_id` in a small, structured form that can be mapped onto `AgentRuntimeState` and exposed through the runtime APIs.

**Event delivery model**

- BaseAcpClient implementations MUST support an event callback of the form `on_event: Callable[[AgentEvent], None] | None`.
- NateOhaAcpClient MUST forward ACP and runtime events for nate_OHA-backed agents through this callback.
- The runtime (via `AgentSupervisor`) is responsible for routing these events into `AgentEventStream` and the existing WebSocket/JSON-RPC pipeline; ACP adapters do not talk directly to transport layers.

**Lifecycle ownership boundary**

- BaseAcpClient implementations own ACP runtime lifecycle for agents they manage (process launch, readiness checks, shutdown, and status reporting).
- AgentSupervisor owns in-memory runtime state and event routing, but does not spawn or kill ACP processes directly.
- RuntimeDaemon and scheduler invoke the ACP adapter via the BaseAcpClient interface; they do not reach around it to manipulate nate_OHA processes.

NateOhaAcpClient MUST NOT be hidden behind a sidecar-specific interface; it is the concrete BaseAcpClient implementation responsible for nate_OHA process lifecycle, conversation setup, turn execution, status reporting, and event emission.


### Key Entities *(include if feature involves data)*

- **BaseAcpClient (ACP Adapter Abstraction)**: The shared abstraction implemented by all ACP clients. It defines the contract between the swarm runtime and any ACP adapter. Both FakeAcpClient and NateOhaAcpClient implement BaseAcpClient.
- **NateOhaAcpClient (Production ACP Adapter)**: The production ACP adapter implementation used by nate_ntm to launch, supervise, and communicate with nate_OHA instances on behalf of managed agents. NateOhaAcpClient owns the complete lifecycle of each managed nate_OHA subprocess, and the runtime interacts with nate_OHA exclusively through this adapter.
- **FakeAcpClient (Fake/Dev/Test Adapter)**: The fake ACP adapter implementation used for development and tests. It remains available for non-production use and continues to implement BaseAcpClient.
- **NateOhaAgentRuntimeRecord (Persisted Agent Runtime Metadata)**: A persisted per-agent record (backed by `AgentMetadata` in `metadata_store`) containing, at minimum, the Agent Mail identity reference, persisted OpenHands conversation identifier, last known process status, restart counters or policy state, and any compatibility/version information required to safely launch nate_OHA for that agent.
- **nate_OHA Instance**: A single nate_OHA ACP process running on behalf of one managed agent. It connects to Agent Mail, executes turns, and emits agent-level events over the ACP connection.
- **Agent Mail Identity**: The persisted identity and credentials associated with a managed agent in Agent Mail, used by nate_OHA to read and write coordination messages for that agent.
- **Swarm Runtime (nate_ntm)**: The long-lived process that manages a swarm of agents, selects the ACP adapter, supervises per-agent processes, and exposes the runtime API and event streams to clients.
- **Runtime Event Pipeline / AgentEventStream**: The event-delivery mechanism through which nate_ntm surfaces agent lifecycle changes, turn results, and errors from ACP adapters (including NateOhaAcpClient) to operators and clients.

## Success Criteria *(mandatory)*

<!--
  ACTION REQUIRED: Define measurable success criteria.
  These must be technology-agnostic and measurable.
-->

### Measurable Outcomes

- **SC-001**: From a standing start with valid configuration, at least 95% of swarm startups that use the NateOhaAcpClient adapter successfully launch nate_OHA processes for all configured agents and report a healthy swarm state via the runtime API within 15 seconds under normal load.
- **SC-002**: After stopping and later resuming a swarm that uses NateOhaAcpClient, 100% of managed agents reuse their prior Agent Mail identities and persisted OpenHands conversation identifiers, and no duplicate or orphaned conversations are created by default.
- **SC-003**: For nate_OHA-backed agents, at least 95% of agent events (including turn completions and errors) are visible to clients through `AgentEventStream` and runtime subscriptions with end-to-end latency under 1 second under normal load.
- **SC-004**: In controlled fault-injection tests where nate_OHA processes are terminated or fail mid-turn, the runtime detects the failures and applies its restart or failure-handling policy, returning affected agents to a supervised state (or marking them failed with clear metadata) in at least 95% of cases.

## Assumptions

- Operators have access to a working `nate_OHA` executable on the same machine as the nate_ntm runtime, and it is on the system PATH or otherwise discoverable by the adapter.
- Agent Mail is available and correctly configured for the project, and per-agent identities and credentials are created and persisted by the time the production adapter is enabled for those agents.
- The existing swarm runtime architecture, event pipeline, and runtime APIs from the "Swarm Runtime Orchestrator" feature remain in place; this feature adds a new production ACP adapter rather than redesigning the runtime.
- Fake and test adapters used for development and testing remain available (in particular FakeAcpClient); the previous experimental OpenHandsAcpClient is retired as part of this feature, and NateOhaAcpClient is the only selectable production ACP adapter under the BaseAcpClient abstraction in the default configuration.
- Detailed conversation history and long-term coordination state continue to be provided by OpenHands and Agent Mail respectively; the adapter and runtime persist only the metadata required to reconnect nate_OHA-backed agents.
