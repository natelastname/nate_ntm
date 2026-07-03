# Feature Specification: nate_ntm Swarm Runtime Orchestrator

**Feature Branch**: `[001-swarm-runtime-orchestrator]`

**Created**: 2026-07-03

**Status**: Draft

**Input**: User description: "Read PROJECT_CONOP.md to find the preliminary plan for the project." Derived from `PROJECT_CONOP.md` (Concept of Operations).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Start and monitor a swarm (Priority: P1)

An operator wants to start a new swarm of coding agents for a local project and monitor high-level health and progress from a single place.

**Why this priority**: This is the core value of nate_ntm: turning a bare project directory into a supervised swarm of agents that can be observed and steered. Without reliable swarm startup and monitoring, none of the other capabilities matter.

**Independent Test**: From a clean environment with an accessible project directory and working external services, an operator can start a swarm and, within a short time, see accurate swarm and per-agent status including running/idle/failed counts and unread mailbox summaries.

**Acceptance Scenarios**:

1. **Given** a valid project directory and configured external services, **when** the operator starts a swarm, **then** nate_ntm creates or restores all required swarm metadata, launches the configured agents, and reports swarm-level status (including number of running, idle, and failed agents) via its runtime API.
2. **Given** a running swarm, **when** agents transition between lifecycle states (Starting, Running, Waiting, Idle, Failed), **then** those changes are reflected in the runtime API and visible to any attached UI client within an acceptable delay.

---

### User Story 2 - Resume a previous swarm (Priority: P2)

An operator wants to stop nate_ntm and later resume the same swarm without losing agent identities, conversations, or coordination context.

**Why this priority**: Swarms may run for many hours or days. Operators must be able to stop and later resume supervision without breaking ongoing work or inter-agent coordination.

**Independent Test**: After cleanly shutting down a swarm and the runtime, an operator can restart nate_ntm in "resume" mode for the same project, and the swarm reconstructs itself with the same Agent Mail identities and OpenHands conversations, picking up unread mail where it left off.

**Acceptance Scenarios**:

1. **Given** a previously running swarm with active agents and Agent Mail identities, **when** the operator resumes the swarm, **then** each agent is relaunched with the same Agent Mail identity and conversation identifier that it used before shutdown.
2. **Given** unread Agent Mail messages at the time of shutdown, **when** the swarm is resumed, **then** those messages are still present, the scheduler loads them, and eligible agents are scheduled for new turns based on that mailbox state.

---

### User Story 3 - Inspect a single agent in detail (Priority: P3)

An operator wants to drill into a specific agent to understand recent behavior, see live activity, and debug issues without attaching directly to the agent process.

**Why this priority**: When swarms misbehave or individual agents stall or fail, operators need quick visibility into what those agents are doing and how they are interacting with external systems, without compromising the architecture where only nate_ntm owns the control protocol.

**Independent Test**: From a running swarm, an operator can select an agent and view recent control-protocol events and live updates through a client that talks only to nate_ntm.

**Acceptance Scenarios**:

1. **Given** a running swarm with active agents, **when** an operator asks to inspect a particular agent, **then** nate_ntm provides a replay of that agent's recent control-protocol events and state transitions from its in-memory buffer.
2. **Given** an open agent inspection view, **when** new events occur for that agent (for example, new turns, tool calls, or errors), **then** nate_ntm streams those events to the client through its runtime API in near real time.

---

### Edge Cases

- What happens if an agent subprocess fails to start or crashes repeatedly while the swarm is running?
- How does the runtime behave if external services (OpenHands or Agent Mail) are temporarily unavailable during polling or turn execution?
- What happens when the operator requests shutdown while agents are mid-turn or while the scheduler is processing mailbox updates?
- How does the system behave when there are no unread mailbox messages for any agents (idle swarm) for an extended period?
- What happens if the stored swarm metadata is incomplete or partially corrupted when attempting to resume?

## Clarifications

### Session 2026-07-03

- Q: What is the runtime’s relationship between a running nate_ntm process and swarms/projects? 
  
  	→ A: Single-swarm runtime per process tied to one project directory.
- Q: For the MVP, what swarm size should the runtime be explicitly optimized and tested for, and how should this relate to long-term architectural limits?
  
  	→ A: The MVP may optimize for approximately 10–20 active agents per swarm, but the scheduler, runtime API, and data model must remain valid for substantially larger swarms, with performance and scalability improvements (for example batching, prioritization, and sharding) deferred to future work.
- Q: For the MVP, how exposed should the runtime control API be, and what is the default trust boundary for clients?
  
  	→ A: The runtime API is localhost-only by default and intended primarily for local clients, with remote access and authentication treated as an explicit future enhancement rather than part of the MVP.

- Q: Where and how should swarm metadata (for example agent identities, conversation IDs, launch configuration) be persisted between runs for the MVP?
  
  	→ A: Swarm metadata is stored in project-local files under a dedicated directory within or adjacent to the project directory; no external database is required for the MVP.



## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The runtime MUST allow an operator to create a swarm for a given project, initializing or reusing the corresponding coordination project in Agent Mail as needed.
- **FR-002**: The runtime MUST create or restore persistent swarm metadata that includes, for each agent, its Agent Mail identity, Agent Mail credentials (or reference to them), OpenHands conversation identifier, launch configuration, model selection, and task description.
- **FR-003**: The runtime MUST be the sole component that establishes and maintains control-protocol connections to managed agents while the swarm is active; user interfaces must never communicate directly with agent processes.
- **FR-004**: The runtime MUST launch the configured number and types of agent subprocesses, supervise their lifecycle (Starting, Idle, Running, Waiting, Failed), and apply restart policies for failed agents within defined limits.
- **FR-005**: The runtime MUST implement an event-driven scheduler that processes runtime events (including Agent Mail updates, ACP turn completions or failures, subprocess exits, user requests, timers, and shutdown signals) and, when appropriate, initiates new control-protocol turns for eligible agents.
- **FR-006**: The runtime MUST maintain a bounded, in-memory Agent Event Stream of recent control-protocol events for each agent, sufficient to support fast UI startup, live attach, and short-term debugging without becoming a primary durability store.
- **FR-007**: The runtime MUST expose a local, bidirectional control API that allows trusted clients (CLI, TUI, or web interfaces) to inspect swarm and agent state, subscribe to events, and request actions such as starting, stopping, resuming, or inspecting agents and swarms.
- **FR-008**: The runtime MUST support graceful shutdown of swarms and individual agents by attempting cancellation through the control protocol, allowing in-flight work to complete when possible, and escalating to forced termination only after a configurable timeout.
- **FR-009**: The runtime MUST support resuming an existing swarm so that all agents reuse the same Agent Mail identities and conversation identifiers, reload relevant swarm metadata, and continue from unread mailbox state without duplicating completed work.
- **FR-010**: The runtime MUST surface scheduler, agent, and mailbox events through its control API and per-agent event buffers so that operators can understand system behavior, failures, and restarts without needing direct access to internal logs.
- **FR-011**: A single nate_ntm runtime instance MUST manage exactly one swarm for a single project directory at a time; running multiple swarms concurrently requires running multiple nate_ntm processes.
- **FR-012**: The scheduler, runtime API, and data model MUST remain valid and correct for substantially larger swarms than the MVP target of approximately 10–20 active agents, with performance-focused optimizations (such as batching, prioritization, or sharding) deferred to future work.
- **FR-013**: By default, the runtime control API MUST bind only to loopback (localhost) interfaces so that it is reachable only from the local machine. Enabling remote access and authentication is a future, explicitly configured enhancement, not part of the MVP.
- **FR-014**: Swarm metadata (including agent identities, conversation identifiers, and launch configuration) MUST be persisted in project-local files under a dedicated directory within or adjacent to the project directory, and this local file store serves as the primary source of truth for starting and resuming the swarm in the MVP.

### Key Entities *(include if feature involves data)*

- **Runtime**: A single long-lived nate_ntm daemon process responsible for exactly one swarm associated with one project directory. The Runtime owns ACP connections, hosts the event-driven scheduler, and exposes the control API used by clients.
- **Swarm**: A named collection of managed agents, associated with a specific project directory, coordination project in Agent Mail, and runtime metadata used for startup, supervision, and resume.
- **Agent Instance**: A single managed agent within a swarm, characterized by its runtime metadata (including status, current turn, subprocess handle, and current ACP connection state), and associated Agent Mail identity and OpenHands conversation identifier.
- **Runtime Event Loop / Scheduler**: The runtime component that coordinates asynchronous events from Agent Mail, ACP, agent subprocesses, runtime API clients, timers, and shutdown signals, and decides when to initiate new control-protocol turns for agents.
- **Runtime API Client**: Any trusted interface (terminal dashboard, command-line utility, web UI, or editor integration) that connects to nate_ntm's control API to observe and steer swarm execution without talking directly to agents.
- **Agent Event Stream**: A bounded, transient, append-only in-memory stream of recent control-protocol events and key state transitions for each agent, used for fast startup, live inspection, and short-term debugging. Implementations may use a ring buffer, deque, or similar data structure.
- **OpenHands Conversation**: The durable conversation identifier used by OpenHands to associate turns and history for a given agent.
- **ACP Connection**: The ephemeral control-protocol connection between the Runtime and an agent process, always associated with an OpenHands conversation but recreated as needed (for example, on resume or restart).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: From a standing start with valid configuration, an operator can start a new swarm for a project and see accurate swarm-level status and per-agent lifecycle states via a client within 10 seconds for at least 95% of runs under normal load.
- **SC-002**: After stopping and later resuming a swarm using the runtime's resume capability, 100% of managed agents reuse their prior Agent Mail identities and conversation identifiers, and unread mailbox messages present at shutdown remain available for scheduling.
- **SC-003**: When an individual agent subprocess fails unexpectedly while the swarm is running (excluding failures caused by permanently unreachable external services), the runtime detects the failure and applies its restart policy, successfully returning the agent to a supervised state in at least 95% of such cases.
- **SC-004**: When an operator opens an agent inspection view via a client, recent control-protocol events for that agent are available for replay, and new events are streamed with end-to-end latency under 1 second for at least 95% of events under normal load.
- **SC-005**: In a scenario with at least 15 and up to 20 active agents in a single swarm, the runtime continues to meet SC-001 through SC-004 for at least 90% of runs under normal load.

## Assumptions

- Operators have access to the necessary external services (OpenHands agent server and Agent Mail) and any required credentials are configured outside of this runtime.
- The runtime runs on infrastructure with stable network connectivity to external services and sufficient resources to keep per-agent event buffers in memory for active swarms.
- Detailed, durable conversation history is provided by OpenHands, and durable coordination state is provided by Agent Mail; nate_ntm stores only the metadata required to reconnect and supervise swarms.
- User interfaces that connect to the runtime's control API are trusted clients responsible for authenticating their own users and enforcing any higher-level access control beyond what the runtime itself provides.
- For the MVP, all runtime API clients are expected to run on the same machine as the nate_ntm process; remote access and multi-user concerns are treated as future enhancements.
