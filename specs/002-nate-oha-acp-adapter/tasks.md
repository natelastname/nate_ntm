---
description: "Implementation tasks for Feature 002: nate_OHA ACP Production Adapter (NateOhaAcpClient)"
---

# Tasks: nate_OHA ACP Production Adapter (Feature 002)

**Input**: Design documents from `/specs/002-nate-oha-acp-adapter/`

**Prerequisites**: `plan.md` (required), `spec.md` (required for user stories), `research.md`, `data-model.md`, `contracts/nate_oha_process_launch.md`, `quickstart.md`, `NATE_OHA_GUIDE.md`

**Tests**: This feature includes targeted unit, integration, and (where explicitly gated) end-to-end tests around the NateOhaAcpClient adapter, nate_OHA process lifecycle, and runtime observability.

**Organization**: Tasks are grouped by user story (US1–US3) after a small set of foundational tasks. Each task description includes concrete file paths.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (`US1`, `US2`, `US3`)
- All descriptions **MUST** include exact file paths.

## Path Conventions

- Single project layout: `src/`, `tests/` at repository root.
- Runtime and adapter code live under `src/nate_ntm/runtime/` and `src/nate_ntm/config/`.
- Tests live under `tests/` with `unit/` and `integration/` subpackages.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Confirm that the existing runtime, adapter, and test scaffolding from Feature 001 is sufficient for this feature.

For this feature, no new project scaffolding or package layout changes are required. The existing `src/nate_ntm/runtime/`, `src/nate_ntm/api/`, and `tests/` structure from Feature 001 is reused as-is.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Establish the expanded `BaseAcpClient` contract and lock down the nate_OHA process launch contract before introducing production behavior.

**⚠️ CRITICAL**: Complete these tasks before changing runtime behavior for ACP adapters.

- [x] T200 Reconcile nate_OHA process launch contract with `NATE_OHA_GUIDE.md`.
  - Cross-check `specs/002-nate-oha-acp-adapter/contracts/nate_oha_process_launch.md` and `specs/002-nate-oha-acp-adapter/data-model.md` against the current `NATE_OHA_GUIDE.md` and the actual `nate_OHA` CLI behavior (including executable name, subcommands, arguments, required environment variables, working directory, startup readiness, shutdown, and version/self-check semantics). Update the contracts and data-model docs so they are fully consistent with the guide. This task must be completed before any runtime code changes that depend on the launch contract.

- [x] T201 [P] [US1] Define `AcpAgentStatus` in the ACP adapter module.
  - In `src/nate_ntm/runtime/acp_client.py`, introduce an `AcpAgentStatus` dataclass (or equivalent small structured type) that captures adapter-level status for a single agent (for example: lifecycle state, last exit code, last error summary, restart count / flags). Ensure it is importable by other runtime modules and will be suitable for mapping into `AgentRuntimeState` and the runtime API payloads.

- [x] T202 [P] [US1] Expand `BaseAcpClient` to the full interface contract.
  - In `src/nate_ntm/runtime/acp_client.py`, expand `BaseAcpClient` from the thin conversation/turn helper into the runtime-facing ACP adapter contract described in `specs/002-nate-oha-acp-adapter/spec.md`:
    - Add abstract methods:
      - `start_agent(agent_id: str, *, metadata: AgentMetadata) -> None`.
      - `start_turn(agent_id: str, prompt: str | None = None) -> str` (allowing an optional prompt parameter while keeping existing call sites compatible).
      - `stop_agent(agent_id: str, *, timeout: float) -> None`.
      - `get_status(agent_id: str) -> AcpAgentStatus`.
    - Add an `on_event: Callable[[AgentEvent], None] | None` callback or equivalent hook on the adapter instance.
    - Update the class docstring to match the "Interface Contract: BaseAcpClient" section in `spec.md`, clarifying lifecycle ownership and event delivery responsibilities.
    - Import `AgentMetadata` from `src/nate_ntm/runtime/metadata_store.py` and `AgentEvent` from `src/nate_ntm/runtime/events.py` as needed.

- [x] T203 [P] [US1] Update `FakeAcpClient` to implement the expanded contract.
  - In `src/nate_ntm/runtime/acp_client.py`, update `FakeAcpClient` so it fully implements the new `BaseAcpClient` API while remaining in-memory and side-effect free:
    - Provide no-op or simple in-memory implementations of `start_agent` and `stop_agent` that track a minimal `NateOhaProcessRecord`-like status per agent (for example, a simple dictionary of `agent_id` → status enum/value).
    - Keep the existing `ensure_conversation` and `start_turn` semantics, updating `start_turn` to accept the optional `prompt` parameter and to invoke `on_event` (when configured) with a synthetic `AgentEvent` representing a completed fake turn.
    - Implement `get_status` to return an `AcpAgentStatus` derived from the in-memory state, suitable for unit tests.
  - Update `tests/unit/runtime/test_acp_client.py` to assert the new methods and callback behavior for `FakeAcpClient`.

- [ ] T204 [US1] Confirm runtime/adapter boundaries reflect the new contract.
  - Review `src/nate_ntm/runtime/daemon.py`, `src/nate_ntm/runtime/scheduler.py`, and `src/nate_ntm/runtime/agents.py` to ensure they treat `BaseAcpClient` as the owner of ACP runtime lifecycle (process launch, readiness, shutdown, status reporting), while `AgentSupervisor` remains focused on in-memory runtime state and event routing. Make any **minimal** adjustments needed to comments, type hints, or attribute names so that this division of responsibility is clear and consistent with `spec.md`.

---

## Phase 3: User Story 1 – Run a swarm on nate_OHA (Priority: P1)

**Goal**: Allow an operator to start a nate_ntm swarm where every managed agent is backed by a supervised nate_OHA ACP instance, without manual per-agent wiring.

**Independent Test**: From a project configured to use the production `NateOhaAcpClient` adapter, an operator can start a swarm and see that all agents are running on nate_OHA, exposed through existing runtime APIs, with no client-side API or UX changes required.

### Implementation for User Story 1

- [x] T210 [US1] Introduce `NateOhaProcessRecord` and `NateOhaAcpClient` skeleton.
  - In `src/nate_ntm/runtime/acp_client.py`, add an internal `NateOhaProcessRecord` (or similarly named) dataclass that matches the fields and invariants in `specs/002-nate-oha-acp-adapter/data-model.md` section 2.1 (e.g. `agent_id`, `pid`, `status`, `last_start_time`, `last_exit_code`, `last_error`, `restart_count`).
  - Introduce a new `NateOhaAcpClient` class that subclasses `BaseAcpClient`, owns a mapping of `agent_id` → `NateOhaProcessRecord`, and is documented (in the module docstring and class docstring) as the canonical production implementation of `BaseAcpClient` for the nate_ntm runtime.

- [x] T211 [US1] Implement nate_OHA version/compatibility self-check.
  - In `NateOhaAcpClient` (`src/nate_ntm/runtime/acp_client.py`), implement a version/compatibility check (for example, a private `_check_version()` helper) that runs the documented self-check command from `NATE_OHA_GUIDE.md` (such as `nate_OHA --version` or `nate_OHA acp --version`) and parses its output to ensure the installed nate_OHA meets the minimum supported version/interface.
  - Invoke this check before launching any nate_OHA subprocesses, and raise `AcpClientError` with a clear diagnostic if the version is incompatible, satisfying FR-013 and the compatibility requirements in `contracts/nate_oha_process_launch.md`.

- [x] T218a [P] [US1] Write initial failing unit tests for `NateOhaAcpClient` launch/status/stop.
  - In `tests/unit/runtime/test_acp_client.py`, add tests that describe the expected behavior for `NateOhaAcpClient.start_agent`, `NateOhaAcpClient.get_status`, and `NateOhaAcpClient.stop_agent` based on the nate_OHA process launch contract. These tests should be written before implementing T212–T214 and may initially fail until the implementation is complete.

- [x] T212 [US1] Implement `start_agent` and the nate_OHA process launch contract.
  - Implement `NateOhaAcpClient.start_agent` in `src/nate_ntm/runtime/acp_client.py` to launch a dedicated `nate_OHA` ACP subprocess per agent using `subprocess.Popen` and the process launch contract in `specs/002-nate-oha-acp-adapter/contracts/nate_oha_process_launch.md`:
    - Resolve the executable and base arguments, ensuring that `--enable-agent-mail` is included when launching nate_OHA with Agent Mail enabled (for example: `nate_OHA acp --enable-agent-mail ...`), consistent with `NATE_OHA_GUIDE.md`.
    - Derive the working directory from `SwarmMetadata.project_path` and/or `AgentMetadata.launch_config`.
    - Populate the required `AGENT_MAIL_*` environment variables (`AGENT_MAIL_PROJECT`, `AGENT_MAIL_AGENT`, `AGENT_MAIL_TOKEN`, `AGENT_MAIL_UPSTREAM_URL`) and any `NATE_NTM_*` correlation variables from `SwarmMetadata` and `AgentMetadata`.
    - Initialize or update the associated `NateOhaProcessRecord` (status "starting", `pid`, timestamps, etc.).
    - Emit a `nate_oha_process_started` (or equivalent) `AgentEvent` via `on_event` when the process is successfully spawned.

- [ ] T213 [US1] Implement startup readiness and failure detection for nate_OHA.
  - Extend `NateOhaAcpClient` (`src/nate_ntm/runtime/acp_client.py`) with a bounded startup readiness check (as per `contracts/nate_oha_process_launch.md` section 4):
    - Within a configurable timeout, verify that the nate_OHA ACP endpoint is healthy and correctly configured for the agent’s Agent Mail identity and conversation.
    - On success, transition the `NateOhaProcessRecord.status` to "running", return normally from `start_agent`, update `AcpAgentStatus`, and emit a `nate_oha_process_ready` event via `on_event`.
    - On failure or timeout, update the process record (status "failed", `last_exit_code`, `last_error`), raise `AcpClientError`, and emit a `nate_oha_process_start_failed` event for downstream policy handling (FR-006).

- [x] T214 [US1] Implement `stop_agent` and `get_status` for nate_OHA processes.
  - In `NateOhaAcpClient` (`src/nate_ntm/runtime/acp_client.py`), implement:
    - `stop_agent` to send a graceful termination signal to the nate_OHA process for the given agent, wait up to a configured timeout, then escalate to a hard kill on timeout, updating `NateOhaProcessRecord` and emitting appropriate events (`nate_oha_process_exited`, `nate_oha_process_crashed`) as described in `contracts/nate_oha_process_launch.md` section 5.
    - `get_status` to return an `AcpAgentStatus` derived from the current `NateOhaProcessRecord` for the agent (including lifecycle, exit codes, and last error), suitable for mapping into runtime API responses.

- [x] T215 [US1] Wire `NateOhaAcpClient` into adapter selection.
  - In `src/nate_ntm/runtime/adapters.py`, update the ACP adapter selection so that:
    - The `AdapterKind.FAKE` branch continues to construct `FakeAcpClient(config=config)`.
    - The `AdapterKind.REAL` branch now constructs `NateOhaAcpClient(config=config)` instead of `OpenHandsAcpClient`.
  - Update the module and class docstrings to state explicitly that `NateOhaAcpClient` is the canonical production implementation of `BaseAcpClient`, while `FakeAcpClient` remains the dev/test implementation.

- [ ] T216 [US1] Retire `OpenHandsAcpClient` as a production option.
  - In `src/nate_ntm/runtime/acp_client.py` and `src/nate_ntm/runtime/adapters.py`:
    - Remove `OpenHandsAcpClient` from the `create_runtime_adapters` ACP selection logic so it can no longer be selected as the production adapter via `AdapterKind.REAL`.
    - Update the `OpenHandsAcpClient` docstring to mark it as legacy/experimental only (if it is retained for compatibility testing) and adjust `__all__` exports as appropriate.
  - In `tests/integration/runtime_acp/test_openhands_acp_client_integration_t102.py`, update the module docstring and skip conditions so that these tests are clearly optional/legacy (for example, by strengthening the skip reason) and do not affect the default production path, which should use `NateOhaAcpClient`.

- [x] T217 [US1] Ensure runtime creation uses nate_OHA-compatible metadata.
  - In `RuntimeDaemon.create` (`src/nate_ntm/runtime/daemon.py`), ensure that when `AdapterKind.REAL` is selected for ACP:
    - The initial agents created via the `agent_count` parameter use `NateOhaAcpClient.ensure_conversation` (or the appropriate nate_OHA mechanism) to derive a stable conversation identifier.
    - The resulting `conversation_id` and Agent Mail identity fields are written into `AgentMetadata` and persisted via `MetadataStore` in a way that is compatible with nate_OHA’s reconnection and resume behavior (per `data-model.md` and `contracts/nate_oha_process_launch.md`).

- [ ] T218b [P] [US1] Expand unit tests for `NateOhaAcpClient` edge cases.
  - In `tests/unit/runtime/test_acp_client.py`, extend the initial tests from T218a to cover additional edge cases using monkeypatched `subprocess` and any internal helpers:
    - Version/compatibility checks and error reporting when nate_OHA is missing or incompatible.
    - Correct construction of the `nate_OHA acp ...` command and environment (including `AGENT_MAIL_*` and `NATE_NTM_*` variables).
    - Startup readiness and failure paths (success, timeout, non-zero exit) updating `NateOhaProcessRecord` and `AcpAgentStatus` as expected.
    - `stop_agent` behavior and status transitions.

- [ ] T219 [P] [US1] Add gated integration smoke tests for real nate_OHA.
  - Under `tests/integration/runtime_acp/`, add a new module (for example, `test_nate_oha_acp_client_integration_002.py`) that exercises `NateOhaAcpClient` against a real nate_OHA installation when an explicit environment flag is set (e.g. `NATE_OHA_INTEGRATION=1`).
  - Include light smoke tests that verify at least:
    - `ensure_conversation` is idempotent for a given `agent_id`.
    - A simple `start_agent` + `stop_agent` roundtrip succeeds without leaking processes.
  - Ensure these tests are skipped by default in CI environments that do not have nate_OHA available.

---

## Phase 4: User Story 2 – Preserve agent identity and conversation continuity (Priority: P2)

**Goal**: Allow operators to shut down and later resume a swarm that uses nate_OHA without losing Agent Mail identities or underlying OpenHands conversation context.

**Independent Test**: After a clean shutdown and later resume of a swarm using `NateOhaAcpClient`, every agent is relaunched on nate_OHA with the same Agent Mail identity and the same OpenHands conversation identifier, and new work continues from the previous coordination state.

### Implementation for User Story 2

- [x] T220 [US2] Persist and reuse conversation IDs through `NateOhaAcpClient`.
  - In `NateOhaAcpClient` (`src/nate_ntm/runtime/acp_client.py`), implement `ensure_conversation` (and any related helpers) so that for nate_OHA-backed agents:
    - If `AgentMetadata.conversation_id` is already set, the adapter reuses it and configures nate_OHA/OpenHands to reconnect to the existing conversation rather than creating a new one by default.
    - If `conversation_id` is empty on first launch, `ensure_conversation` MUST return and persist a conversation identifier that is either:
      - allocated deterministically from runtime metadata (for example, based on swarm and agent identifiers), or
      - obtained from nate_OHA/OpenHands during the first successful ACP initialization (after the process has been launched and any required ACP handshake has completed).
    - Once a non-empty conversation identifier is available, it MUST be written back into `AgentMetadata.conversation_id` via `MetadataStore.save_agent_metadata` so that subsequent launches and resumes reuse the same ID.
  - This behavior must satisfy FR-005 and the invariants in `specs/002-nate-oha-acp-adapter/data-model.md` (section 3.2).

- [x] T221 [US2] Enforce nate_OHA conversation continuity on resume.
  - In `RuntimeDaemon.resume` (`src/nate_ntm/runtime/daemon.py`), extend the existing resume-time validation so that when `acp_client` is a `NateOhaAcpClient` and `AgentMetadata.conversation_id` is non-empty for an agent:
    - Calling `acp_client.ensure_conversation(agent_id)` yields the same identifier.
    - Any mismatch between the adapter-derived ID and the persisted `conversation_id` is logged and raised as a `RuntimeStartupError`, mirroring the existing error structure for ACP mismatches.
  - This preserves conversation continuity guarantees for nate_OHA-backed agents (FR-005, FR-012).

- [ ] T222 [P] [US2] Extend resume integration tests for nate_OHA-backed swarms.
  - In `tests/integration/quickstart/test_resume_swarm_us2.py` (and, if helpful, `tests/integration/quickstart/test_resume_error_paths_us2.py`), add or extend scenarios that run the runtime with `AdapterKind.REAL` for ACP using `NateOhaAcpClient` (with nate_OHA interactions stubbed or gated as needed). Validate that:
    - After shutdown and resume, each agent’s `agent_mail_identity` and `conversation_id` are unchanged.
    - Resume-time validation rejects mismatched identities or conversation IDs with clear errors.

- [ ] T223 [P] [US2] Persist last-known nate_OHA status into `AgentMetadata`.
  - Update `RuntimeDaemon` and/or `RuntimeScheduler` (`src/nate_ntm/runtime/daemon.py`, `src/nate_ntm/runtime/scheduler.py`) so that for nate_OHA-backed agents, significant lifecycle changes reported via `AcpAgentStatus` (for example, transitions to "Failed" or repeated crashes) are reflected in `AgentMetadata.last_known_status` and persisted via `MetadataStore.save_agent_metadata`.
  - Ensure `RuntimeDaemon.get_agent_detail` continues to provide a meaningful status for agents even when no live `AgentRuntimeState` exists (for example, immediately after a crash or before the scheduler has started), aligning with US2 acceptance scenarios.

---

## Phase 5: User Story 3 – Observe nate_OHA-backed agents through existing runtime APIs (Priority: P3)

**Goal**: Allow operators and client applications to observe and debug nate_OHA-backed agents using the existing runtime APIs and event streams, without talking directly to nate_OHA.

**Independent Test**: From a client that only talks to nate_ntm, an operator can inspect nate_OHA-backed agents, see recent events, and subscribe to live updates, with no knowledge of nate_OHA internals.

### Implementation for User Story 3

- [x] T230 [US3] Wire `BaseAcpClient.on_event` into `AgentSupervisor` and the WebSocket pipeline.
  - In `RuntimeDaemon.create` and `RuntimeDaemon.resume` (`src/nate_ntm/runtime/daemon.py`), after constructing the `RuntimeScheduler` and its `AgentSupervisor`, wire the ACP adapter’s event callback so that:
    - `BaseAcpClient.on_event` (for both `FakeAcpClient` and `NateOhaAcpClient`) forwards `AgentEvent` instances into `AgentSupervisor`’s per-agent `AgentEventStream`.
    - The existing bridge in `src/nate_ntm/runtime/runner.py` (which assigns `supervisor.on_agent_event` to publish events over the WebSocket JSON-RPC server) continues to receive these events, so that `events.subscribe` / `events.notify` work for nate_OHA-backed agents without additional transport-specific logic in the adapters.
  - Ensure this wiring is in place by the time T212–T214 are implemented so that process-started and readiness events from `NateOhaAcpClient` are not silently dropped.

- [x] T231 [US3] Map nate_OHA process and ACP events into `AgentEvent`.
  - In `NateOhaAcpClient` (`src/nate_ntm/runtime/acp_client.py`), implement event mapping logic that converts:
    - Process-level lifecycle events (for example, `nate_oha_process_started`, `nate_oha_process_ready`, `nate_oha_process_start_failed`, `nate_oha_process_exited`, `nate_oha_process_crashed`).
    - ACP/agent-level events from nate_OHA’s ACP event stream (turn completions, tool calls, relevant errors).
    into `AgentEvent` instances with appropriate `type` and `payload` fields.
  - Dispatch these events via the adapter’s `on_event` callback, taking care to exclude secrets (such as `AGENT_MAIL_TOKEN`) from payloads, consistent with `contracts/nate_oha_process_launch.md` and the runtime’s logging guidelines.

- [ ] T232 [P] [US3] Extend WebSocket event streaming tests for nate_OHA-backed agents.
  - In `tests/integration/quickstart/test_runtime_ws_events_us3.py`, extend the existing US3 scenarios to cover a swarm configured with `AdapterKind.REAL` for ACP using `NateOhaAcpClient` (with nate_OHA interactions stubbed or gated as appropriate). Validate that:
    - Agent inspection via `agent.get_detail` shows nate_OHA-related status and recent events.
    - Live event subscriptions via `events.subscribe` / `events.notify` receive nate_OHA process and ACP events with acceptable latency (SC-003).

- [x] T233 [P] [US3] Add unit tests for event callback wiring.
  - Add unit tests in `tests/unit/runtime/test_events.py` and/or `tests/unit/runtime/test_acp_client.py` to verify that:
    - When `BaseAcpClient.on_event` is set, both `FakeAcpClient` and `NateOhaAcpClient` invoke the callback with well-formed `AgentEvent` instances.
    - `RuntimeControlContext.create_runtime_control_context` in `src/nate_ntm/runtime/runner.py` continues to bridge `AgentSupervisor.on_agent_event` into `JsonRpcWebSocketServer.publish_event`, ensuring end-to-end propagation from adapters to WebSocket clients.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Align documentation, cross-feature references, and manual validation with the implemented behavior.

- [ ] T240 [P] Update quickstart and top-level docs for NateOhaAcpClient.
  - Review and update `specs/002-nate-oha-acp-adapter/quickstart.md` to match the implemented CLI flags, adapter selection defaults, and observed behavior of `NateOhaAcpClient` (including create → inspect → shutdown → resume flows).
  - Where appropriate, update `README.md` and `AGENTS_MK2.md` to reference nate_OHA as the production ACP runtime for nate_ntm and to point to this feature’s spec, plan, and tasks for operators who need deeper details.

- [ ] T241 [P] Align Feature 001 tasks with the new production adapter.
  - In `specs/001-swarm-runtime-orchestrator/tasks.md`, update the Phase 6 production integration tasks (particularly T102) to indicate that the canonical production ACP adapter is now `NateOhaAcpClient` implemented via Feature 002, and that `OpenHandsAcpClient` is no longer the default production option. Keep historical references for traceability, but make the current adapter choice unambiguous.

- [ ] T242 [P] Run end-to-end validation and capture findings.
  - Execute the full quickstart scenarios from `specs/002-nate-oha-acp-adapter/quickstart.md` against a real nate_OHA + Agent Mail setup where possible (including swarm create, inspect, shutdown, and resume using `NateOhaAcpClient`).
  - Capture any deviations, edge cases, or follow-up work items in `specs/002-nate-oha-acp-adapter/research.md` and/or `specs/002-nate-oha-acp-adapter/plan.md`, updating acceptance notes and assumptions as needed.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No new work; relies on Feature 001 scaffolding.
- **Foundational (Phase 2)**: Depends on Setup; T200–T204 must be completed before implementing US1 tasks.
- **User Story 1 (Phase 3 – P1)**: Depends on Phase 2; delivers the MVP of nate_OHA-backed swarm startup on top of the existing runtime.
- **User Story 2 (Phase 4 – P2)**: Depends on US1; builds on initial swarm creation and adapter wiring to enforce identity and conversation continuity.
- **User Story 3 (Phase 5 – P3)**: Depends on US1; can proceed in parallel with US2 once nate_OHA-backed swarms can start reliably.
- **Polish (Phase 6)**: Depends on all desired user story phases being complete.

### Within Each User Story

- Complete foundational adapter contract work (Phase 2) before implementing any `[US1]`, `[US2]`, or `[US3]` tasks.
- For each story:
  - Implement core runtime/adapter behavior before adding or updating integration tests, except where tasks are explicitly marked as "write tests first" (for example, T218a).
  - Ensure the relevant tests fail before completing implementation tasks.
  - Validate the story’s independent test from `specs/002-nate-oha-acp-adapter/spec.md` and `quickstart.md` before moving on.

### Parallel Opportunities

- All tasks marked `[P]` can be worked on in parallel once their phase prerequisites are satisfied.
- Within Phase 2, T201–T203 can proceed in parallel once T200 has locked down the nate_OHA process contract.
- After Phase 3 is stable, US2 tasks (T220–T223) and US3 tasks (T230–T233) can proceed concurrently by different contributors.
- Polish tasks (T240–T243) can largely run in parallel once all user story phases are complete.
