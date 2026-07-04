---
description: "Implementation tasks for Feature 001: nate_ntm Swarm Runtime Orchestrator"
---

# Tasks: nate_ntm Swarm Runtime Orchestrator

**Input**: Design documents from `/specs/001-swarm-runtime-orchestrator/`

**Prerequisites**: `plan.md` (required), `spec.md` (required for user stories), `research.md`, `data-model.md`, `contracts/runtime-api.md`, `quickstart.md`

**Tests**: This feature includes targeted unit, integration, and contract tests where they are explicitly called out below.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (`US1`, `US2`, `US3`)
- All descriptions MUST include exact file paths.

## Path Conventions

- Single project layout: `src/`, `tests/` at repository root.
- Runtime and API code live under `src/nate_ntm/`.
- Tests live under `tests/` with `unit/` and `integration/` subpackages.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Establish baseline runtime/API packages, test layout, and metadata directory conventions.

- [x] T001 Create package skeletons in `src/nate_ntm/runtime/__init__.py`, `src/nate_ntm/api/__init__.py`, and `src/nate_ntm/config/__init__.py` to match the implementation plan structure.
- [x] T002 [P] Create runtime and API test package directories (`tests/unit/runtime/`, `tests/unit/api/`, `tests/integration/runtime_mail/`, `tests/integration/runtime_acp/`, `tests/integration/quickstart/`) with `__init__.py` files as needed.
- [x] T003 [P] Add the runtime metadata directory `.nate_ntm/` to `.gitignore` so swarm metadata generated under a project directory is not committed by default.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core runtime infrastructure that MUST be complete before implementing any user story.

**⚠️ CRITICAL**: No user story work should begin until this phase is complete.

- [x] T004 Implement `SwarmMetadata` and `AgentMetadata` persistence (load/save and basic validation) in `src/nate_ntm/runtime/metadata_store.py` using the layout and invariants from `specs/001-swarm-runtime-orchestrator/data-model.md`.
- [x] T038 Implement atomic metadata write semantics in `src/nate_ntm/runtime/metadata_store.py` for all `SwarmMetadata` and `AgentMetadata` persistence operations: write to a temporary file, flush/fsync if practical, then rename into place so that `.nate_ntm/` contents are never left in a partially written state after crashes or interruptions.

- [x] T005 [P] Implement a `RuntimeConfig` model and loader in `src/nate_ntm/config/runtime_config.py` to resolve the project path, `.nate_ntm/` directory, and control API port from CLI options and environment.
- [x] T006 Implement `RuntimeState` and `AgentRuntimeState` data structures in `src/nate_ntm/runtime/state.py` reflecting the runtime and agent lifecycle states from `data-model.md` and `specs/001-swarm-runtime-orchestrator/spec.md`.
- [x] T007 [P] Implement `AgentEvent` and `AgentEventStream` abstractions in `src/nate_ntm/runtime/events.py` consistent with the `AgentEvent` type defined in `specs/001-swarm-runtime-orchestrator/contracts/runtime-api.md`.
- [x] T008 Implement a `RuntimeDaemon` entrypoint class in `src/nate_ntm/runtime/daemon.py` that wires together `RuntimeConfig`, `SwarmMetadata`, `RuntimeState`, and the scheduler, with start and graceful shutdown methods but stubbed integrations.
- [x] T037 Define and implement explicit `create` vs `resume` startup semantics in `src/nate_ntm/runtime/daemon.py` and `src/nate_ntm/cli.py`, including CLI flags (for example, `--mode create|resume` and optional `--force`/`--reuse`) and ensuring that when `.nate_ntm/` metadata already exists and `--mode create` is requested without an explicit override, startup fails safely with a clear error instead of silently reusing or overwriting metadata.

- [x] T009 Add or migrate to a Typer-based CLI in `src/nate_ntm/cli.py` that exposes `runtime` and `api` command groups (for example, `nate-ntm runtime start` and `nate-ntm api call`), while keeping the existing `cli()` entrypoint function and adding an `api call` subcommand that uses a JSON-RPC/WebSocket client helper in `src/nate_ntm/api/client.py` to invoke the runtime control API. (Partial: `runtime start` implemented and tested; `api` subcommands and client stub are deferred.)
- [x] T010 [P] Update `pyproject.toml` `[project.scripts]` so that a `nate-ntm` console script is available and routed to `nate_ntm.cli:cli`, matching the commands used in `specs/001-swarm-runtime-orchestrator/quickstart.md`.
- [x] T011 [P] Add a WebSocket JSON-RPC server skeleton in `src/nate_ntm/api/server.py` that can accept localhost connections, parse JSON-RPC requests, and dispatch to placeholder handlers for all documented methods. (Skeleton class stubbed for later JSON-RPC/WebSocket wiring; no networking yet.)
- [x] T012 [P] Add basic unit tests for the metadata store load/save roundtrip and `.nate_ntm/` layout in `tests/unit/runtime/test_metadata_store.py` to enforce FR-014 semantics.

---

## Phase 3: User Story 1 

### Start and monitor a swarm (Priority: P1) 

**Goal**: Allow an operator to start a new swarm for a project and monitor high-level swarm and per-agent status from a single place.

**Independent Test**: From a clean environment with an accessible project directory and working external services, an operator can start a swarm and, within a short time, see accurate swarm and per-agent status including running/idle/failed counts and unread mailbox summaries via the runtime API.

### Implementation for User Story 1

- [x] T013 [US1] Implement swarm creation and metadata initialization for `mode="create"` in `src/nate_ntm/runtime/daemon.py`, creating `.nate_ntm/` contents and initial `SwarmMetadata`/`AgentMetadata` records as required by FR-001, FR-002, and FR-014.
- [x] T014 [P] [US1] Define an Agent Mail coordination adapter interface in `src/nate_ntm/runtime/agent_mail_client.py` and implement a fake/dev-mode client that simulates projects, identities, and unread mail without contacting a real Agent Mail service; use it from `src/nate_ntm/runtime/daemon.py` when a swarm is created to allocate and persist the Agent Mail project ID and per-agent identities (FR-001 and key entities in `spec.md`).
- [x] T015 [P] [US1] Define an ACP client adapter interface in `src/nate_ntm/runtime/acp_client.py` and implement a fake/dev-mode client that simulates OpenHands-style control-protocol conversations and turn lifecycle without contacting a real server; use it from `src/nate_ntm/runtime/daemon.py` when a swarm is created to allocate and persist an ACP `conversation_id` for new agents (FR-003 and FR-004).
- [ ] T016 [US1] Implement agent subprocess launch and lifecycle supervision in `src/nate_ntm/runtime/agents.py`, updating `AgentRuntimeState.status` (Starting, Idle, Running, Waiting, Failed) and invoking restart hooks per FR-004 and FR-011.
- [ ] T017 [US1] Implement scheduler logic in `src/nate_ntm/runtime/scheduler.py` for processing startup, subprocess, ACP, and Agent Mail events so that swarm-level and per-agent status remain accurate in `RuntimeState`.
- [x] T018 [US1] Implement the `runtime.get_status` handler in `src/nate_ntm/api/server.py` and its runtime-facing implementation in `src/nate_ntm/runtime/daemon.py` to return `RuntimeStatus` and aggregate agent counts as specified in `specs/001-swarm-runtime-orchestrator/contracts/runtime-api.md`.
- [x] T019 [US1] Implement the `swarm.get_overview` handler in `src/nate_ntm/api/server.py` and support function in `src/nate_ntm/runtime/daemon.py` to return per-agent summaries (ID, display name, status, `has_unread_mail`, `last_error`) consistent with the contract.
- [x] T020 [P] [US1] Add integration tests for swarm startup and status reporting in `tests/integration/quickstart/test_start_and_status_us1.py`, covering SC-001 and the US1 acceptance scenarios.
- [x] T021 [P] [US1] Add a CLI integration test in `tests/integration/quickstart/test_runtime_cli_us1.py` that runs `nate-ntm runtime start --project <tmp_project>` and verifies `runtime.get_status` returns `Running` with correct agent counts.


**US1 MVP status note**: The US1 quickstart flow (create swarm with fake
adapters, observe status/overview via the runtime API, and shut down
cleanly) is considered complete and validated by tasks T013, T014, T015
and T018, T019, T020, T021. Tasks T016 and T017 are partially satisfied
by the current dev-mode `AgentSupervisor` / `RuntimeScheduler`
implementations (no real subprocesses or external services) and remain
open for future expansion; they are not blockers for treating US1 as
complete for this orchestrator MVP.

---

## Phase 4: User Story 2 

### Resume a previous swarm (Priority: P2)

**Goal**: Allow an operator to stop the runtime and later resume the same swarm without losing agent identities, conversations, or coordination context.

**Independent Test**: After cleanly shutting down a swarm and the runtime, an operator can restart nate_ntm in `resume` mode for the same project and the swarm reconstructs itself with the same Agent Mail identities and OpenHands conversations, picking up unread mail where it left off.

### Implementation for User Story 2

- [X] T022 [US2] Implement the swarm resume path in `src/nate_ntm/runtime/daemon.py` to support `mode="resume"`, loading `SwarmMetadata` and `AgentMetadata` from `.nate_ntm/` and validating invariants from `specs/001-swarm-runtime-orchestrator/data-model.md`.
- [X] T023 [US2] Implement runtime logic in `src/nate_ntm/runtime/daemon.py` and `src/nate_ntm/runtime/agent_mail_client.py` / `src/nate_ntm/runtime/acp_client.py` to rebind Agent Mail identities and ACP conversations for all agents on resume, reusing `agent_mail_identity` and `conversation_id` in accordance with FR-009.
- [X] T024 [US2] Extend the scheduler in `src/nate_ntm/runtime/scheduler.py` to poll Agent Mail for unread messages at startup and enqueue events to schedule eligible agents on resume, satisfying FR-005 and US2 acceptance scenario 2.
- [X] T025 [P] [US2] Add integration tests for shutdown and resume behavior in `tests/integration/quickstart/test_resume_swarm_us2.py`, validating SC-002 and ensuring `.nate_ntm/` metadata is reused correctly.
- [X] T026 [P] [US2] Add tests for corrupted or incomplete metadata in `tests/integration/runtime_mail/test_resume_error_paths_us2.py` to ensure the runtime fails fast or degrades gracefully when `.nate_ntm/` contents are invalid (edge case section of `specs/001-swarm-runtime-orchestrator/spec.md`).

---

## Phase 5: User Story 3 

### Inspect a single agent in detail (Priority: P3)

**Goal**: Allow an operator to drill into a specific agent to view recent behavior and live activity without attaching directly to the agent process.

**Independent Test**: From a running swarm, an operator can select an agent and view recent control-protocol events plus live updates through a client that talks only to nate_ntm.

### Implementation for User Story 3

- [X] T027 [US3] Wire Agent Event Stream updates into `src/nate_ntm/runtime/scheduler.py` so that runtime- and adapter-originated events for each agent are appended to `AgentEventStream` buffers with bounded size, as defined in `specs/001-swarm-runtime-orchestrator/data-model.md`.
- [X] T028 [US3] Implement the `agent.get_detail` handler in `src/nate_ntm/api/server.py` and its runtime-side query in `src/nate_ntm/runtime/daemon.py` to return agent metadata and recent `AgentEvent` records per `specs/001-swarm-runtime-orchestrator/contracts/runtime-api.md`.
- [X] T029 [US3] Implement `events.subscribe`, the server-side subscription registry, and `events.notify` behavior in `src/nate_ntm/api/server.py` and `src/nate_ntm/api/jsonrpc_ws.py` using JSON-RPC-style notifications over the localhost WebSocket as defined in `specs/001-swarm-runtime-orchestrator/contracts/runtime-api.md`.
- [X] T030 [US3] Extend the runtime event pipeline in `src/nate_ntm/runtime/daemon.py`, `src/nate_ntm/runtime/events.py`, and `src/nate_ntm/runtime/runner.py` to publish new `AgentEvent` instances to active subscriptions for the correct agents.
- [X] T031 [P] [US3] Add integration tests for agent inspection and event streaming in `tests/integration/quickstart/test_runtime_ws_events_us3.py`, validating SC-004 at a basic level and the US3 acceptance scenarios.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Refinements that affect multiple user stories and overall operability.

- [ ] T032 [P] Align `specs/001-swarm-runtime-orchestrator/checklists/requirements.md` with the finalized spec, plan, runtime API contract, and this tasks.md so that each FR and SC has clear checklist coverage.
- [ ] T033 [P] Update `specs/001-swarm-runtime-orchestrator/quickstart.md` and `README.md` to reflect the implemented CLI commands, runtime API behavior, and any deviations discovered during implementation.
- [ ] T034 Implement structured logging and error-reporting conventions in `src/nate_ntm/runtime/daemon.py` and `src/nate_ntm/runtime/scheduler.py` (including log levels, error summaries, and correlation IDs where appropriate).
- [ ] T035 [P] Run the full quickstart validation scenarios and add any new follow-up items or clarifications to `specs/001-swarm-runtime-orchestrator/research.md` and `specs/001-swarm-runtime-orchestrator/plan_feedback.md`.
- [ ] T036 [P] Update `AGENTS_MK2.md` and, if appropriate, `AGENTS.md` to reference the nate_ntm Swarm Runtime Orchestrator feature, its plan (`specs/001-swarm-runtime-orchestrator/plan.md`), and this tasks file for future agent workflows.


## Future Features (Post-MVP)

- [ ] T100 [P] Support configurable runtime adapter implementations for ACP and Agent Mail,
  including configuration fields in `src/nate_ntm/config/runtime_config.py` and CLI wiring
  on `nate-ntm runtime start` (for example, a general `--adapter-mode` flag or
  per-adapter options), while defaulting to the existing fake/dev-mode adapters so
  that all US1–US3 tests continue to run without external services.
- [ ] T101 [P] Implement a production Agent Mail adapter in
  `src/nate_ntm/runtime/agent_mail_client.py` (for example, `McpAgentMailClient`) that
  satisfies the `BaseAgentMailClient` interface using real Agent Mail APIs, and add
  focused integration tests under `tests/integration/runtime_mail/` that are gated or
  marked to avoid running in offline CI environments.
- [ ] T102 [P] Implement a production OpenHands-compatible ACP adapter in
  `src/nate_ntm/runtime/acp_client.py` (for example, `OpenHandsAcpClient`) that
  satisfies the `BaseAcpClient` interface using real control-protocol APIs, and add
  focused integration tests under `tests/integration/runtime_acp/` that are gated or
  marked to avoid running in offline CI environments.


---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies – can start immediately.
- **Foundational (Phase 2)**: Depends on Setup (Phase 1) completion – BLOCKS all user stories.
- **User Story 1 (Phase 3 – P1)**: Depends on Foundational (Phase 2); delivers MVP end-to-end (SC-001).
- **User Story 2 (Phase 4 – P2)**: Depends on User Story 1 (Phase 3) because resume builds on initial swarm creation and status reporting.
- **User Story 3 (Phase 5 – P3)**: Depends on User Story 1 (Phase 3) for a running swarm and basic runtime/API wiring; can proceed in parallel with User Story 2 after Phase 3 is stable.
- **Polish (Phase 6)**: Depends on all desired user story phases being complete.

### User Story Dependencies

- **User Story 1 (P1)**: Baseline for all other stories (swarm startup, metadata, status).
- **User Story 2 (P2)**: Builds on US1’s metadata and runtime lifecycle model; must not change US1 behavior.
- **User Story 3 (P3)**: Builds on US1’s runtime and API infrastructure but should remain independently testable using a running swarm.

### Within Each User Story

- Complete foundational runtime wiring (Phase 2) before implementing any `[US1]`, `[US2]`, or `[US3]` tasks.
- For each story:
  - Implement core runtime behavior before adding integration tests.
  - Ensure tests for that story fail before completing the corresponding implementation tasks.
  - Validate the story’s independent test from `specs/001-swarm-runtime-orchestrator/spec.md` and `specs/001-swarm-runtime-orchestrator/quickstart.md` before moving on.

### Parallel Opportunities

- All tasks marked `[P]` can be worked on in parallel once their phase prerequisites are satisfied.
- Within Phase 2, tasks T005, T007, T010, T011, and T012 can proceed in parallel with T004 and T006 using provisional interfaces.
- After Phase 3 (US1) is stable:
  - US2 tasks (T022–T026) and US3 tasks (T027–T031) can proceed concurrently by different contributors.
- Polish tasks (T032–T036) can largely run in parallel once all user story phases are complete.

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (T001–T003).
2. Complete Phase 2: Foundational (T004–T012).
3. Complete Phase 3: User Story 1 (T013–T021).
4. **STOP and VALIDATE**: Run the US1 quickstart scenario and SC-001 checks (startup + status).
5. Decide whether to proceed to US2/US3 based on findings.

### Incremental Delivery

1. Setup + Foundational → Runtime skeleton and API server are in place.
2. Add User Story 1 → Test independently → treat as MVP.
3. Add User Story 2 → Test resume flows (SC-002) → document behavior.
4. Add User Story 3 → Test inspection/streaming (SC-004) → ensure performance at 15–20 agents (SC-005).

### Parallel Team Strategy

- One contributor can focus on runtime internals (Phase 2 + US1 tasks in `src/nate_ntm/runtime/`).
- Another can take on the API/WebSocket layer and tests (Phase 2 + US1/US3 tasks in `src/nate_ntm/api/` and `tests/integration/`).
- A third can own integration surfaces (`src/nate_ntm/runtime/agent_mail_client.py`, `src/nate_ntm/runtime/acp_client.py`) and resume behavior (US2).

---

## Notes

- `[P]` tasks = different files and no direct dependencies on incomplete work.
- `[US1]`, `[US2]`, and `[US3]` labels map tasks to specific user stories for traceability.
- Each user story should be independently completable and testable against its acceptance scenarios and success criteria.
- Avoid vague tasks or tasks that touch too many files at once; prefer small, verifiable increments.
- For this feature, CLI/API-based quickstart flows are the MVP validation surfaces; an interactive terminal dashboard/TUI is intentionally deferred to a future feature built on this runtime.
