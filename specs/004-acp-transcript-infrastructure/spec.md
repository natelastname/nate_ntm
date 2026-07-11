# Feature Specification: ACP Transcript Infrastructure

**Feature Branch**: `004-acp-transcript-infrastructure`

**Created**: 2026-07-07

**Status**: Draft

**Input**: Replaces the earlier Feature 004 attachment API scope. Goal: introduce protocol-level ACP frame capture and bounded per-agent transcript history that future features (such as an attachment API) can build on.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Inspect recent ACP conversation for a single agent (Priority: P1)

Runtime operators and developers need to inspect the recent ACP JSON-RPC conversation for a specific agent so they can understand what requests, responses, and notifications have flowed without relying on external logs or modifying the agent.

**Why this priority**: Understanding the actual protocol-level traffic is essential for debugging and for future interactive attachment features. Without a local transcript, it is difficult to explain agent behaviour, reproduce bugs, or validate that clients and adapters are speaking the same protocol.

**Independent Test**: Start a runtime with at least one ACP-backed agent, generate a small sequence of ACP requests and responses for that agent, and then query the transcript history. Confirm that the returned frames appear in the order they were observed, include both directions of traffic, and preserve the original JSON-RPC payloads.

**Acceptance Scenarios**:

1. **Given** a running runtime and agent A with recent ACP traffic, **When** the transcript history for agent A is requested, **Then** the caller receives a bounded, ordered sequence of ACP frames that includes both requests sent to A and responses/notifications received from A.
2. **Given** an agent with no recorded ACP traffic, **When** the transcript history is requested, **Then** the caller is informed that the history is empty and the runtime remains usable for future traffic and transcript capture.

---

### User Story 2 - Keep transcript memory usage bounded and per-agent (Priority: P1)

Operators responsible for running the swarm runtime need confidence that ACP transcript capture will not cause unbounded memory growth or cross-contaminate histories between agents.

**Why this priority**: ACP traffic can be frequent and verbose. Without a bounded, per-agent transcript buffer, transcript capture could degrade runtime performance or make it impossible to reason about which frames belong to which agent.

**Independent Test**: For two agents A and B, generate more ACP frames than the configured history size for each agent. Confirm that only the most recent frames are retained for each agent, that older frames are dropped, and that querying the transcript for A never returns frames belonging to B (and vice versa).

**Acceptance Scenarios**:

1. **Given** a configured maximum transcript length **N** for agent A, **When** more than **N** ACP frames are captured for A, **Then** only the most recent **N** frames are retained and the ordering of those frames matches the order in which they were observed.
2. **Given** agents A and B with interleaved ACP traffic, **When** transcript histories are queried for each agent, **Then** each history contains only frames for its respective agent and never includes frames from the other.

---

### User Story 3 - Preserve existing monitoring/event behaviour (Priority: P2)

Existing users of the runtime’s monitoring and status APIs need ACP transcript capture to be added without changing the semantics of the existing high-level event streams or control API.

**Why this priority**: The runtime is already used for monitoring via high-level events and snapshots. Introducing a new transcript facility must not alter those behaviours, so existing dashboards, CLIs, and tests continue to work unchanged.

**Independent Test**: Run the existing runtime test suite and representative monitoring workflows with transcript capture enabled. Confirm that previously defined Agent event streams, JSON-RPC responses, and WebSocket event notifications are unchanged in shape and behaviour.

**Acceptance Scenarios**:

1. **Given** a runtime with transcript capture enabled, **When** existing APIs such as `agent.get_detail`, `swarm.get_overview`, and the events WebSocket are exercised, **Then** their response shapes and semantics remain unchanged compared to a runtime without transcript capture.
2. **Given** existing monitoring-focused tests for agent event streams, **When** transcript capture is implemented, **Then** all such tests continue to pass without modification.

### Edge Cases

- Agents that generate no ACP traffic (empty transcripts).
- Very rapid ACP traffic that exceeds the transcript buffer size.
- Multiple agents with interleaved ACP traffic.
- Requests without explicit IDs (notifications) and responses that rely on out-of-band correlation.
- Transcript clearing/dropping while ACP traffic continues.
- Runtime shutdown and restart; transcripts are in-memory only and are discarded between runs.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The runtime MUST introduce a dedicated ACP transcript model separate from existing high-level monitoring and status event streams. The new transcript MUST NOT repurpose or change the semantics of the existing event stream facility used for runtime monitoring.
- **FR-002**: For each ACP-backed agent, the runtime MUST maintain a bounded, in-memory transcript history of protocol-level ACP frames observed for that agent. When the history reaches its configured capacity, the oldest frames MUST be dropped as new frames are added.
- **FR-003**: Each captured transcript frame MUST record at least: (a) the associated agent identifier, (b) a timestamp indicating when the frame was observed, (c) the direction of the frame (for example, client-to-agent vs. agent-to-client), (d) the raw JSON-RPC object for the frame, and (e) any request or correlation identifier that is available.
- **FR-004**: The runtime MUST capture ACP JSON-RPC frames at the adapter or relay boundary, such that all protocol traffic between the runtime and ACP peers that is relevant to an agent’s control conversation can be recorded in the transcript without requiring changes to the agents themselves.
- **FR-005**: The transcript facility MUST provide a runtime-facing API that allows callers to: (a) append a new frame to an agent’s transcript, (b) retrieve the most recent frames for a given agent (with a caller-specified or default limit), and (c) clear or drop transcript history for a given agent (or all agents) when needed.
- **FR-006**: The transcript retrieval API MUST return frames in the correct logical order as observed by the runtime. When a limit is specified, it MUST return the most recent frames up to that limit while preserving their relative ordering.
- **FR-007**: Transcript histories MUST remain isolated per agent. Operations on the transcript for a given agent (append, retrieve, clear) MUST NOT affect or expose frames for any other agent.
- **FR-008**: ACP transcript storage for this feature MUST be in-memory only and MUST NOT introduce durable transcript persistence (for example, on disk or in an external datastore). Future features may extend this behaviour, but persistence is out of scope here.
- **FR-009**: Adding ACP transcript capture MUST NOT change the externally observable behaviour of existing runtime control and monitoring interfaces, including but not limited to: JSON-RPC method shapes, response payloads, and the monitoring-focused event stream.
- **FR-010**: The transcript infrastructure MUST be suitable for reuse by follow-on features, including but not limited to a dedicated ACP attachment API that can replay recent ACP traffic and relay live bidirectional traffic for a single agent.

### Key Entities *(include if feature involves data)*

- **ACP Transcript Frame**: A single protocol-level ACP JSON-RPC frame associated with a specific agent, including direction, timestamp, and the raw JSON-RPC object.
- **ACP Transcript Stream**: The bounded, ordered, in-memory collection of transcript frames for a single agent.
- **Transcript API**: The internal runtime-facing operations that allow code within the runtime to append frames, query recent history, and clear/drop histories.
- **Adapter/Relay Boundary**: The conceptual integration point where ACP JSON-RPC traffic enters or leaves the runtime (for example, ACP client adapters or relays); this is where frames are observed and captured.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Unit tests demonstrate that transcript frames for a given agent are appended in order and that retrieval returns the most recent frames with their original ordering preserved.
- **SC-002**: Unit tests demonstrate that each agent’s transcript history is bounded: when more than the configured maximum number of frames are appended, only the most recent frames are retained and older frames are discarded.
- **SC-003**: Unit tests demonstrate that transcript histories are isolated per agent: appending frames for multiple agents and then querying each history returns only frames for the requested agent.
- **SC-004**: Unit tests demonstrate that transcript frames retain their raw JSON-RPC payloads without lossy transformation so that future features can inspect or replay them as needed.
- **SC-005**: The existing high-level monitoring event stream behaviour (including event shapes and counts in representative scenarios) remains unchanged when transcript capture is enabled, as validated by the existing test suite and any additional regression tests.

## Assumptions

- ACP traffic remains JSON-RPC-shaped, and it is acceptable for the transcript facility to store frames as JSON-RPC objects without interpreting or normalising method-specific payloads.
- In-memory, bounded transcripts are sufficient for the initial debugging and attachment scenarios; durable transcript storage, if required, will be introduced in a separate feature.
- The runtime continues to target Linux-like environments consistent with the project’s constitution; platform-specific differences outside that environment are out of scope for this feature.
- User-facing attachment and interactive UI capabilities (including any dedicated WebSocket attach endpoint) will be designed and implemented as separate features that build on top of this transcript infrastructure (for example, Feature 005: ACP Agent Attachment API).
