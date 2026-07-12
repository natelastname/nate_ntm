---

description: "Task list for Epic 005: Nate OHA runtime integration"
---

# Tasks: Nate OHA Runtime Integration (Epic 005)

**Input**: Design documents from `specs/005-nate-oha-migration/`

**Prerequisites**: `plan.md` (required), `spec.md` (required for user stories), optional `research.md`, `data-model.md`, and related appendices/contracts

**Tests**: The epic spec calls out explicit validation and integration behavior. Each user story below includes test tasks that SHOULD be implemented (or updated) alongside code.

**Organization**: Tasks are grouped by user story (P1–P5) so that each story can be implemented and validated as an independent increment.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no direct dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

## Path Conventions

- Single project layout:
  - Runtime and adapters: `src/nate_ntm/runtime/`
  - Configuration: `src/nate_ntm/config/`
  - CLI: `src/nate_ntm/cli.py`
  - API/control surface: `src/nate_ntm/api/`
  - Unit tests: `tests/unit/`
  - Integration tests: `tests/integration/`
  - E2E tests: `tests/e2e/`

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Confirm design context and capture the current ACP/Agent Mail behavior for Epic 005.

- [ ] T001 Read `specs/005-nate-oha-migration/spec.md`, `plan.md`, and appendices A–C plus `NATE_OHA_GUIDE.md`, and create `specs/005-nate-oha-migration/research.md` capturing key goals (Nate OHA–centric runtime, ACP-owned conversation IDs, no fake ACP/mail) and open questions.
- [ ] T002 Survey existing runtime integration code and tests and append findings to `specs/005-nate-oha-migration/research.md`, covering:
  - ACP adapters in `src/nate_ntm/runtime/acp_client.py` (`FakeAcpClient`, `OpenHandsAcpClient`, `NateOhaAcpClient`)
  - Agent Mail adapters in `src/nate_ntm/runtime/agent_mail_client.py`
  - Adapter wiring in `src/nate_ntm/runtime/adapters.py`
  - Current tests in `tests/unit/runtime/test_acp_client.py` and `tests/integration/runtime_acp/`.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Shared design artifacts that subsequent user stories rely on.

- [ ] T003 Create or update `specs/005-nate-oha-migration/data-model.md` to describe the runtime data model for this epic, including:
  - Swarm metadata and per-agent metadata fields relevant to ACP and Agent Mail (`src/nate_ntm/runtime/metadata_store.py`).
  - `RuntimeConfig` fields that affect ACP and Nate OHA launches (`src/nate_ntm/config/runtime_config.py`).
  - Adapter abstractions (`BaseAcpClient`, `NateOhaAcpClient`, Agent Mail clients) in `src/nate_ntm/runtime/acp_client.py` and `src/nate_ntm/runtime/agent_mail_client.py`.
  - How ACP-owned conversation identifiers and Agent Mail configuration are persisted and reused across create/resume flows.

**Checkpoint**: Foundation ready – user story implementation can now begin.

---

## Phase 3: User Story 1 - Launch and supervise Nate OHA agents through ACP (Priority: P1) 🎯 MVP

**Goal**: `nate_ntm` launches and supervises `nate-oha acp` subprocesses via the ACP SDK so that swarm agents use the current configuration-driven Nate OHA runtime rather than the obsolete HTTP-oriented ACP design.

**Independent Test (from spec)**: Start a swarm with one or more agents and verify that each agent is launched through `nate-oha acp`, establishes an ACP session, exposes its ACP events to `nate_ntm`, and can be shut down cleanly.

### Tests for User Story 1

- [ ] T010 [P] [US1] Extend unit tests in `tests/unit/runtime/test_acp_client.py` to cover NateOhaAcpClient ACP session management and event emission, including:
  - Successful ACP connection establishment (using stubbed ACP SDK objects).
  - Emission of `AgentEvent` instances for key ACP lifecycle/turn events via `on_event`.
  - Robust handling of process startup failures and early exits.
- [ ] T011 [P] [US1] Add or update integration tests in `tests/integration/runtime_acp/` (e.g., `test_nate_oha_acp_runtime_us1.py`) that start a `RuntimeDaemon` using `NateOhaAcpClient` and verify:
  - Each agent is launched via `nate-oha acp`.
  - An ACP session is established per agent.
  - ACP events are visible via the runtime’s event/inspection interfaces.
  - Shutdown requests lead to graceful process termination, with escalation to forced kill only when necessary.

### Implementation for User Story 1

- [ ] T012 [US1] Implement ACP SDK session management in `src/nate_ntm/runtime/acp_client.py` for `NateOhaAcpClient`, creating per-agent ACP client sessions bound to each `nate-oha acp` subprocess and wiring their callbacks into the adapter’s `on_event`.
- [ ] T013 [US1] Implement translation from ACP SDK events to `AgentEvent` instances in `src/nate_ntm/runtime/acp_client.py` (and/or helper functions in `src/nate_ntm/runtime/events.py`), ensuring event `source=AgentEventSource.ACP`, stable type names, and JSON-serializable payloads that match `contracts/runtime-api.md` expectations.
- [ ] T014 [US1] Ensure ACP events from `NateOhaAcpClient` flow through the existing runtime pipeline by verifying that `RuntimeDaemon` → `AgentSupervisor` → `RuntimeScheduler` wiring (`src/nate_ntm/runtime/daemon.py`, `src/nate_ntm/runtime/agents.py`, `src/nate_ntm/runtime/scheduler.py`) appends ACP events to per-agent streams and exposes them via the control API (`src/nate_ntm/api/runtime_api.py`, `tests/integration/quickstart/`).
- [ ] T015 [US1] Harden `NateOhaAcpClient.start_agent` and `stop_agent` in `src/nate_ntm/runtime/acp_client.py` to fully satisfy FR-002 (startup checks, graceful shutdown, forced termination on timeout, accurate `AcpAgentStatus`), updating existing unit tests in `tests/unit/runtime/test_acp_client.py` as needed.

**Checkpoint**: User Story 1 is fully functional and testable independently using the Nate OHA ACP adapter.

---

## Phase 4: User Story 2 - Create and resume persistent agent conversations (Priority: P2)

**Goal**: Conversation identifiers for agents are allocated by ACP (`session/new`), persisted by `nate_ntm`, and reused on resume via `--resume`, with no locally synthesized conversation IDs.

**Independent Test (from spec)**: Create a swarm, capture the conversation identifier returned by ACP `session/new`, stop the swarm, resume it using the persisted identifier, and verify that the resumed agent continues the same conversation.

### Tests for User Story 2

- [ ] T020 [P] [US2] Add unit tests in `tests/unit/runtime/test_acp_client.py` that stub ACP SDK `session/new` responses and verify that `NateOhaAcpClient.ensure_conversation`:
  - Calls `session/new` only when no persisted `conversation_id` exists for an agent.
  - Persists the ACP-provided conversation identifier via `MetadataStore` (`src/nate_ntm/runtime/metadata_store.py`).
  - Reuses the persisted identifier on subsequent calls and across processes.
- [ ] T021 [P] [US2] Add integration tests in `tests/integration/runtime_acp/test_nate_oha_conversations_p2.py` that exercise create → shutdown → resume for a swarm and assert:
  - Conversation identifiers observed on first run come from ACP (or a stubbed ACP SDK).
  - Resume launches `nate-oha acp` with the same conversation identifiers via `--resume`.
  - No new conversations are created for agents that already have persisted IDs.

### Implementation for User Story 2

- [ ] T022 [US2] Replace deterministic/UUID-based conversation ID derivation in `NateOhaAcpClient.ensure_conversation` (`src/nate_ntm/runtime/acp_client.py`) with ACP-owned semantics:
  - When metadata has no `conversation_id`, open an ACP session and call `session/new` via the ACP SDK.
  - Persist the returned conversation identifier to `AgentMetadata` via `MetadataStore`.
  - Cache the identifier in-memory for subsequent calls in the same process.
- [ ] T023 [US2] Update `RuntimeDaemon.resume` (`src/nate_ntm/runtime/daemon.py`) and any other call sites that validate conversation IDs to treat the persisted `AgentMetadata.conversation_id` as canonical and to call `NateOhaAcpClient.ensure_conversation` only to validate that the adapter’s view matches metadata, never to allocate new IDs during resume.
- [ ] T024 [US2] Update `NateOhaAcpClient.start_agent` (and any future turn-start helpers) in `src/nate_ntm/runtime/acp_client.py` to propagate the persisted conversation identifier to `nate-oha acp` via `--resume CONVERSATION_ID` when resuming an existing conversation, and avoid creating a fresh ACP session in those cases.
- [ ] T025 [US2] Remove or revise tests and documentation that assert deterministic, locally derived conversation IDs for NateOhaAcpClient or OpenHandsAcpClient (e.g., `tests/unit/runtime/test_acp_client.py`, `tests/integration/runtime_acp/test_nate_oha_acp_client_integration_002.py`, `CONOP_NATEOHAv2_FEEDBACK.md`) so that all expectations treat conversation IDs as opaque ACP-owned values.

**Checkpoint**: User Stories 1 and 2 both work independently; conversational continuity is ACP-owned and resume-safe.

---

## Phase 5: User Story 3 - Exercise production ACP code paths in echo mode (Priority: P3)

**Goal**: Development and test execution use `NateOhaAcpClient` with Nate OHA configured in echo mode so that tests exercise the same subprocess, ACP, event, shutdown, and resume paths used in production.

**Independent Test (from spec)**: Launch an agent with `runtime.mode=echo`, exchange ACP messages, inspect emitted events, stop the process, and resume the same conversation using the same ACP client implementation used in agent mode.

### Tests for User Story 3

- [ ] T030 [P] [US3] Add or update integration tests in `tests/integration/runtime_acp/test_nate_oha_echo_mode_p3.py` that:
  - Start agents via `RuntimeDaemon`/`NateOhaAcpClient` with `runtime.mode=echo`.
  - Verify ACP session establishment, event streaming, shutdown, and resume paths.
  - Compare behavior to agent-mode runs to confirm the same code paths are exercised.
- [ ] T031 [P] [US3] Add unit tests in `tests/unit/runtime/test_acp_client.py` and `tests/unit/runtime/test_adapters_real_acp_t102.py` (or equivalent) that verify:
  - `create_runtime_adapters` (`src/nate_ntm/runtime/adapters.py`) always selects `NateOhaAcpClient` for ACP in both dev/fake and real/production modes.
  - The only behavioral difference between these modes is the `runtime.mode` configuration passed to Nate OHA.

### Implementation for User Story 3

- [ ] T032 [US3] Simplify ACP adapter selection in `src/nate_ntm/runtime/adapters.py` so that `create_runtime_adapters` always constructs `NateOhaAcpClient` for ACP (for both `AdapterKind.FAKE` and `AdapterKind.REAL`), and remove `OpenHandsAcpClient` from the selection logic.
- [ ] T033 [US3] Implement configuration logic in `NateOhaAcpClient` (likely in `_build_command` and associated helpers) to set `runtime.mode=echo` for development/test executions and `runtime.mode=agent` for production executions, using `RuntimeConfig`/swarm metadata rather than raw environment variables.
- [ ] T034 [US3] Update scheduler/daemon wiring and tests so that development/test flows (for example, `adapter_mode=fake`) launch Nate OHA in echo mode via `NateOhaAcpClient` instead of using `FakeAcpClient` (`src/nate_ntm/runtime/daemon.py`, `src/nate_ntm/runtime/state.py`, `tests/unit/runtime/test_acp_client.py`, `tests/integration/runtime_acp/`).
- [ ] T035 [US3] Deprecate or remove `FakeAcpClient` as a runtime-selected ACP implementation in `src/nate_ntm/runtime/acp_client.py`, keeping only minimal test-only helpers if still required, and update references in specs/docs (`specs/001-swarm-runtime-orchestrator/`, `specs/002-nate-oha-acp-adapter/`, `README.md`) to describe echo-mode Nate OHA as the dev/test ACP path.

**Checkpoint**: User Stories 1–3 work independently; echo-mode runs fully exercise the production ACP code path.

---

## Phase 6: User Story 4 - Run swarms with optional real Agent Mail coordination (Priority: P4)

**Goal**: Enable real `mcp_agent_mail` coordination for swarms while preserving the ability to create, supervise, stop, and resume swarms with Agent Mail completely disabled.

**Independent Test (from spec)**: Run one swarm with Agent Mail disabled and another with Agent Mail enabled against a running `mcp_agent_mail` server. Verify that both swarms can launch and resume, and that only the enabled swarm contacts Agent Mail.

### Tests for User Story 4

- [ ] T040 [P] [US4] Add integration tests in `tests/integration/runtime_mail/` that:
  - Start a swarm with Agent Mail disabled and assert that no calls are made to a `mcp_agent_mail` server (for example, by running without a server and confirming no failures).
  - Start a swarm with Agent Mail enabled against a running `mcp_agent_mail` reference server and verify that project, identity, credential reference, and upstream URL are passed correctly to Nate OHA and that attempts to run without a reachable server fail clearly.
- [ ] T041 [P] [US4] Add unit tests for `McpAgentMailClient` in `tests/unit/runtime/test_agent_mail_client.py` exercising:
  - `ensure_project`, `ensure_agent_identity_with_credentials`, and `get_unread_mail_flags` happy paths.
  - JSON-RPC error handling and network failures raising `AgentMailClientError` (`src/nate_ntm/runtime/agent_mail_client.py`).

### Implementation for User Story 4

- [ ] T042 [US4] Introduce explicit Agent Mail enable/disable semantics in the runtime configuration and metadata:
  - Extend `RuntimeConfig`/`load_runtime_config` in `src/nate_ntm/config/runtime_config.py` and `SwarmMetadata` in `src/nate_ntm/runtime/metadata_store.py` to represent whether Agent Mail is enabled for a swarm.
  - Treat missing/empty Agent Mail settings as "Agent Mail disabled" while ensuring that this state does not prevent swarm creation/resume.
- [ ] T043 [US4] Update `create_runtime_adapters` (`src/nate_ntm/runtime/adapters.py`) and `RuntimeDaemon.create`/`RuntimeDaemon.resume` (`src/nate_ntm/runtime/daemon.py`) so that:
  - `McpAgentMailClient` is constructed and used only when Agent Mail is enabled.
  - No Agent Mail adapter is used, and no Agent Mail APIs are contacted, when Agent Mail is disabled.
  - Runtime behavior remains correct for create/resume/supervision with Agent Mail off.
- [ ] T044 [US4] Remove or quarantine `FakeAgentMailClient` from runtime code paths:
  - Ensure it is no longer selectable via configuration (`AdapterKind`, `create_runtime_adapters`).
  - If retained, move it to a clearly test-only context or mark it as deprecated so production/runtime flows cannot depend on it.
- [ ] T045 [US4] Update documentation to reflect real-only Agent Mail behavior and optionality, including `NATE_OHA_GUIDE.md`, `specs/005-nate-oha-migration/spec-appendix-*.md`, and `README.md`, with guidance on running Agent Mail–dependent tests and expected failure modes when `mcp_agent_mail` is unreachable.

**Checkpoint**: User Stories 1–4 work independently; Agent Mail is a real, optional integration with clear test behavior.

---

## Phase 7: User Story 5 - Configure Nate OHA from a base JSON file plus runtime overrides (Priority: P5)

**Goal**: `nate_ntm` launches agents from a shared Nate OHA JSON configuration and provides only swarm-/agent-specific overrides so that Nate OHA remains responsible for its own runtime and prompt configuration.

**Independent Test (from spec)**: Launch multiple agents from the same base configuration while supplying different runtime mode, prompt identity, Agent Mail, model, or credential overrides, and verify that each process receives the expected configuration.

### Tests for User Story 5

- [ ] T050 [P] [US5] Add integration tests in `tests/integration/runtime_acp/test_nate_oha_config_overrides_p5.py` that:
  - Use a shared base JSON configuration (e.g., `nate-oha-profiles/profile1.json`).
  - Launch multiple agents with different runtime overrides (mode, model, `prompt.soul_content`, Agent Mail settings).
  - Verify via ACP events, logs, or other observable signals that each process receives the correct configuration derived from base + overrides.

### Implementation for User Story 5

- [ ] T051 [US5] Extend `RuntimeConfig`/`load_runtime_config` in `src/nate_ntm/config/runtime_config.py` to capture Nate OHA launch settings, including:
  - Base Nate OHA configuration path (defaulting to `nate-oha-profiles/profile1.json` for repository tests when not explicitly set).
  - Optional default `llm.model` and `llm.api_key` values.
  - Optional default `prompt.soul_content` and related prompt overrides.
  - Any additional fields needed to compute `--set` overrides, with appropriate environment variable names documented.
- [ ] T052 [US5] Update the CLI entrypoint in `src/nate_ntm/cli.py` (and any related API wiring) to accept optional flags for base Nate OHA config path and key runtime overrides (model, API key, prompt soul, Agent Mail options), forwarding them into `load_runtime_config`.
- [ ] T053 [US5] Refactor `NateOhaAcpClient._build_command` and related helpers in `src/nate_ntm/runtime/acp_client.py` to:
  - Always pass `--config <base_json>` using the configured base path from `RuntimeConfig`.
  - Pass `--resume <conversation_id>` when launching an agent with a persisted conversation.
  - Materialize runtime-specific values (`runtime.mode`, `llm.model`, `llm.api_key`, `prompt.soul_content`, `features.agent_mail.*`) as repeated `--set path=value` arguments rather than constructing full Nate OHA/OpenHands configuration in-process.
- [ ] T054 [US5] Update unit tests in `tests/unit/runtime/test_acp_client.py` to assert on the new NateOhaAcpClient command-line contract (including `--config`, `--resume`, and `--set` arguments) for both echo and agent modes, with Agent Mail enabled/disabled.
- [ ] T055 [US5] Sweep the codebase for any remaining places that construct OpenHands or Nate OHA configuration dictionaries directly (for example, legacy HTTP ACP client code in `OpenHandsAcpClient` within `src/nate_ntm/runtime/acp_client.py`) and either adapt them to the base-config-plus-overrides model or remove them entirely in line with FR-011/FR-023/FR-024.

**Checkpoint**: User Stories 1–5 work independently; all Nate OHA launches use a base JSON configuration plus explicit runtime overrides.

---

## Phase N: Polish & Cross-Cutting Concerns

**Purpose**: Remove obsolete implementations, align documentation, and validate the integrated runtime end-to-end.

- [ ] T060 [P] Remove `OpenHandsAcpClient` from `src/nate_ntm/runtime/acp_client.py` and its associated tests in `tests/unit/runtime/test_acp_client.py` and `tests/integration/runtime_acp/test_openhands_acp_client_integration_t102.py`, after confirming that NateOhaAcpClient-based flows meet all acceptance criteria.
- [ ] T061 [P] Ensure no runtime configuration path or CLI option can select `FakeAcpClient` or `FakeAgentMailClient` by searching the repository and either deleting these classes, moving them to clearly test-only modules, or marking them as deprecated fixtures with no production callers.
- [ ] T062 Update high-level docs and specs (`README.md`, `AGENTS_MK2.md`, `specs/001-swarm-runtime-orchestrator/*`, `specs/002-nate-oha-acp-adapter/*`, `specs/005-nate-oha-migration/*`) to:
  - Describe `NateOhaAcpClient` as the single ACP adapter.
  - Emphasize ACP-owned conversation IDs and history.
  - Document echo vs agent runtime modes and Agent Mail optionality.
  - Document integration-test gating via environment variables (e.g., `NATE_OHA_INTEGRATION`, an Agent Mail integration flag).
- [ ] T063 Add or update end-to-end tests in `tests/e2e/test_real_runtime_nate_oha_agent_mail.py` that exercise the full stack (CLI → `RuntimeDaemon` → `NateOhaAcpClient` → Nate OHA → optional `mcp_agent_mail`) and verify success criteria SC-001–SC-010 from `specs/005-nate-oha-migration/spec.md`.
- [ ] T064 Run the full test suite under representative configurations (echo mode only, agent mode with valid LLM credentials, Agent Mail disabled, Agent Mail enabled with a real server) and fix any brittle tests or missing gating markers (e.g., pytest markers, environment guards), updating `pyproject.toml` or test helpers in `tests/` as needed.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies – can start immediately.
- **Foundational (Phase 2)**: Depends on Setup completion – provides shared design context for all user stories.
- **User Stories (Phases 3–7)**:
  - All depend on the Foundational phase for agreed data-model and terminology.
  - **US1 (P1)** can start as soon as Phase 2 is complete.
  - **US5 (P5)** configuration work (base JSON + overrides) can begin in parallel with US1 but must be in place before full acceptance of US1/US2/US3.
  - **US2 (P2)** depends on US1’s ACP launch/event wiring and on the base `--config`/`--resume` helpers from US5.
  - **US3 (P3)** depends on US1 (ACP runtime path) and US5 (ability to override `runtime.mode`).
  - **US4 (P4)** depends on US1 (runtime orchestration) but is otherwise orthogonal; it can proceed in parallel with US2/US3 once Phase 2 is complete.
- **Polish (Final Phase)**: Depends on all desired user stories being complete; it removes legacy adapters, aligns docs, and validates the integrated system.

### User Story Dependencies

- **User Story 1 (P1)**: Baseline for ACP subprocess launch, session management, and event streaming; no dependencies on other stories beyond Phase 2.
- **User Story 2 (P2)**: Depends on US1 (ACP session and event framework) and on US5’s command-line/resume wiring; focuses on ACP-owned conversation IDs and resume semantics.
- **User Story 3 (P3)**: Depends on US1 and US5; ensures echo-mode runs exercise the same ACP code paths as agent mode and retires fake ACP implementations.
- **User Story 4 (P4)**: Depends on US1; ensures Agent Mail is both real and optional, without affecting core ACP lifecycle.
- **User Story 5 (P5)**: Shares dependencies with US1; can be developed in parallel but is required for full compliance with the launch architecture in the spec.

### Within Each User Story

- Write or update tests (T010/T011, T020/T021, etc.) to describe the desired behavior and ensure they FAIL before implementation.
- Implement adapter/configuration changes next.
- Integrate with `RuntimeDaemon`, scheduler, and control API where applicable.
- Confirm the independent test from the spec passes before moving to the next story.

### Parallel Opportunities

- Setup and Foundational tasks touch mostly documentation and can be completed quickly up front.
- US1 and the configuration portions of US5 can proceed in parallel as long as the ACP launch contract is coordinated.
- After US1+US5 basics are in place, US2, US3, and US4 can be worked on in parallel by different contributors (they primarily touch different areas: conversations, echo-mode wiring, Agent Mail).
- Polish tasks (T060–T064) should be deferred until after the new architecture is validating all user stories, to avoid prematurely deleting useful references.

---

## Implementation Strategy

### MVP First (User Story 1 + minimal config)

1. Complete Phase 1 (Setup) and Phase 2 (Foundational).
2. Implement Phase 3 (US1) alongside the minimal configuration support from Phase 7 (enough to pass `--config` and `--resume`).
3. **Validate**: Run US1 integration tests (T011) to ensure agents launch via `nate-oha acp`, produce ACP events, and shut down cleanly.

### Incremental Delivery

1. US1 + minimal US5 → MVP ACP launch + supervision.
2. US2 → Persistent ACP-owned conversations (`session/new` + `--resume`).
3. US3 → Echo-mode exercising production ACP code paths.
4. US4 → Optional real Agent Mail integration with clear failure modes.
5. Remaining US5 tasks → Full base-config-plus-overrides launch model.
6. Polish phase → Remove legacy adapters, align docs, and finalize E2E validation.

### Parallel Team Strategy

With multiple contributors:

- After Setup + Foundational:
  - Developer A: US1 (ACP launch + events) + core US5 CLI/config work.
  - Developer B: US2 (ACP-owned conversations) + resume semantics.
  - Developer C: US3 (echo-mode) and US4 (Agent Mail optionality).
- Coordinate on shared files (`acp_client.py`, `runtime_config.py`, `adapters.py`) to avoid conflicts.
- Use checkpoints (end of each phase) as points to run the integration/E2E tests and adjust the plan if needed.

---

## Notes

- `[P]` tasks target different files or logically independent work and can be parallelized when capacity allows.
- `[USn]` labels map tasks to specific user stories for traceability.
- Each user story should be independently completable and testable using its acceptance criteria and independent test from the spec.
- Prefer simplifying or deleting obsolete code and tests (per FR-023/FR-024) over preserving compatibility with the legacy HTTP ACP design.
- Commit after each task or logical group and keep `specs/005-nate-oha-migration/tasks.md` up to date as new work is discovered.