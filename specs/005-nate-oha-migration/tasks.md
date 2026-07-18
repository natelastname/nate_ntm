---
description: "Implementation tasks for Epic 005: nate-oha runtime integration"
---

# Tasks: nate-oha runtime integration

**Input**: Design documents under `specs/005-nate-oha-migration/`

**Required design artifacts**:

- `spec.md`
- `plan.md`
- `spec-appendix-B.md`
- `spec-appendix-C.md`

**Supporting artifacts**:

- `research.md`
- `data-model.md`
- `contracts/`
- `quickstart.md`

**Validation approach**: Prefer real subprocess and integration tests over mocked ACP transports. Unit tests should cover runtime-owned logic only. Existing tests may be rewritten or removed when they preserve obsolete ACP or fake-adapter behavior.

## Format

```
[ID] [P?] [Story?] Description
```

- **\[P\]**: Can proceed in parallel with other tasks in the same phase.
- **\[Story\]**: Maps the task to a Speckit user story where applicable.
- Every implementation task includes the relevant file paths.
- Tasks are ordered by architectural dependency, not merely by file or user-story number.

------------------------------------------------------------------------

# Phase 1: Research and Contract Confirmation

**Purpose**: Confirm the external contracts before changing the runtime architecture.

-
  T001 Review `specs/005-nate-oha-migration/spec.md`, `plan.md`, `spec-appendix-B.md`, and `spec-appendix-C.md`; record the final architectural decisions in `specs/005-nate-oha-migration/research.md`.
-
  T002 \[P\] Verify the installed `agent-client-protocol` Python SDK APIs required by this epic and document the selected interfaces in `specs/005-nate-oha-migration/research.md`, including:

  - agent process spawning;
  - `ClientSideConnection`;
  - initialization and capability negotiation;
  - `session/new`;
        - the nate-oha resume-session flow;

  - prompting;
  - cancellation;
  - session updates;
  - connection shutdown.

-
  T003 \[P\] Verify the concrete `nate-oha acp` contract against the current nate-oha implementation and document it in `specs/005-nate-oha-migration/contracts/nate-oha-launch.md`, including:

  - executable name;
  - `acp` subcommand;
  - `--config`;
  - `--resume`;
  - repeated `--set path=value`;
  - stdio ownership;
  - stderr behavior;
  - conversation ID returned by `session/new`;
  - behavior of resumed sessions and conversation-history replay.

-
  T004 \[P\] Verify the real `mcp_agent_mail` API contract used by `McpAgentMailClient` and record the supported project, identity, credential, and inbox operations in `specs/005-nate-oha-migration/contracts/agent-mail.md`.
-
  T005 Verify the `agent-client-protocol` SDK dependency version, update `uv.lock` if needed using `uv sync`, and confirm the project environment can be reconstructed from a clean checkout.

**Checkpoint**: The ACP SDK, nate-oha launch contract, resume behavior, and Agent Mail contract are explicit and no longer inferred from legacy code.

------------------------------------------------------------------------

# Phase 2: Foundational Runtime Model

**Purpose**: Establish the new runtime-owned abstractions before replacing existing behavior.

## Launch specification

-
  T010 Define `NateOhaLaunchSpec` in `src/nate_ntm/runtime/nate_oha_launch.py` as the single representation of a nate-oha process launch, including:

  - executable;
  - base configuration path;
  - working directory;
  - runtime mode;
  - optional persisted conversation ID;
  - model and API-key overrides;
  - prompt soul content;
  - optional Agent Mail settings;
  - final argument vector.

-
  T011 Implement deterministic argument construction in `src/nate_ntm/runtime/nate_oha_launch.py` for:

  ```
  nate-oha acp
      --config BASE_CONFIG
      [--resume CONVERSATION_ID]
      [--set path=value]…
  ```

  The implementation must build an argv list directly, avoid shell interpolation, and omit unset optional overrides.
-
  T012 \[P\] Add focused tests in `tests/unit/runtime/test_nate_oha_launch.py` covering:

  - required `--config`;
  - echo and agent modes;
  - optional `--resume`;
  - supported `--set` paths;
  - Agent Mail enabled and disabled;
  - values containing whitespace and punctuation;
  - deterministic argument ordering.

## Configuration

-
  T013 Extend `RuntimeConfig` and `load_runtime_config` in `src/nate_ntm/config/runtime_config.py` with the runtime-owned inputs needed to construct `NateOhaLaunchSpec`, including:

  - nate-oha executable;
  - base JSON configuration path;
  - runtime mode;
  - optional model;
  - optional API key;
  - optional prompt soul content;
  - Agent Mail enabled state;
  - Agent Mail upstream URL.

-
  T014 Update `src/nate_ntm/cli.py` to expose only the operator-facing nate-oha launch options required by the specification and pass them into `load_runtime_config`.
-
  T015 \[P\] Add or update configuration and CLI tests under `tests/unit/config/` and `tests/unit/cli/` for the new nate-oha fields and precedence rules.

## ACP runtime types

-
  T016 Redesign the runtime-facing ACP types in `src/nate_ntm/runtime/acp_client.py` around agent lifecycle rather than conversations or turns.

  The public interface must provide asynchronous operations equivalent to:

  ```
  async def start_agent(…)
  async def prompt(…)
  async def interrupt(…)
  async def stop_agent(…)
  def get_status(…)
  ```

  Remove `ensure_conversation()` and `start_turn()` from the public runtime-facing contract.
-
  T017 Define active-session state in `src/nate_ntm/runtime/acp_client.py`, including:

  - agent ID;
  - ACP-owned conversation ID;
  - managed process;
  - `ClientSideConnection`;
  - protocol callback client;
  - lifecycle status;
  - background tasks required to drain stderr or monitor process exit.

-
  T018 Update `specs/005-nate-oha-migration/data-model.md` to reflect:

  - ACP-owned opaque conversation IDs;
  - persisted versus transient state;
  - `NateOhaLaunchSpec`;
  - active ACP sessions;
  - optional Agent Mail metadata;
  - removal of synthetic conversation and fake-adapter state.

**Checkpoint**: The runtime has a clear launch specification, configuration model, and agent-centric ACP interface before any production behavior is migrated.

------------------------------------------------------------------------

# Phase 3: User Story 1 — Launch and Supervise nate-oha agents

**Priority**: P1

**Goal**: Each managed agent is backed by a real `nate-oha acp` subprocess connected through the official ACP SDK.

**Independent test**: Start a swarm in echo mode and verify that each configured agent launches through `nate-oha acp`, establishes an ACP session, emits ACP events, reports runtime status, and shuts down cleanly.

## ACP SDK integration

-
  T020 \[US1\] Implement `NateNtmAcpProtocolClient` in `src/nate_ntm/runtime/acp_protocol_client.py` using the official ACP SDK client interface.

  It must:

  - advertise explicit `ClientCapabilities`;
  - receive structured session updates;
  - translate supported updates into runtime events;
  - provide explicit protocol-appropriate responses for unsupported client capabilities.

-
  T021 \[US1\] Implement ACP update translation in `src/nate_ntm/runtime/acp_event_translation.py`, converting official ACP SDK models into JSON-serializable `AgentEvent` values without exposing ACP SDK models to the rest of the runtime.
-
  T022 \[P\] \[US1\] Add unit tests in `tests/unit/runtime/test_acp_event_translation.py` for representative ACP update variants and stable runtime event output.

## Process and session lifecycle

-
  T023 \[US1\] Replace the existing `NateOhaAcpClient.start_agent` implementation in `src/nate_ntm/runtime/acp_client.py` so it:

  - accepts a `NateOhaLaunchSpec`;
  - launches `nate-oha acp` through the official ACP SDK process helper where practical;
  - establishes stdio ACP transport;
  - initializes ACP;
  - negotiates capabilities;
  - creates the ACP session;
  - stores the active session;
  - reports the agent ready only after session establishment succeeds.

-
  T024 \[US1\] Implement continuous stderr draining and process-exit monitoring in `src/nate_ntm/runtime/acp_client.py`, ensuring:

  - stdout remains ACP-only;
  - stderr is exposed through logging or runtime diagnostics;
  - unexpected process exit updates agent status and emits a failure event.

-
  T025 \[US1\] Implement `prompt`, `interrupt`, `stop_agent`, and `get_status` in `src/nate_ntm/runtime/acp_client.py` using the official ACP SDK and Linux process supervision semantics.
-
  T026 \[US1\] Implement graceful shutdown escalation in `src/nate_ntm/runtime/acp_client.py`:

  - request protocol-level cancellation or closure where applicable;
  - terminate the process group after the graceful timeout;
  - kill the process group only after the termination timeout;
  - clean up SDK connections and background tasks.

## Scheduler integration

-
  T027 \[US1\] Replace placeholder agent launch behavior in `src/nate_ntm/runtime/agents.py` and `src/nate_ntm/runtime/scheduler.py` with calls to `NateOhaAcpClient.start_agent`.
-
  T028 \[US1\] Update scheduler and daemon startup/shutdown flows in:

  - `src/nate_ntm/runtime/scheduler.py`;
  - `src/nate_ntm/runtime/daemon.py`;
  - `src/nate_ntm/runtime/runner.py`;

  so all managed agents are started, supervised, and stopped through `NateOhaAcpClient`.
-
  T029 \[US1\] Ensure ACP events pass through the existing runtime event pipeline into:

  - per-agent `AgentEventStream`;
  - `agent.get_detail`;
  - WebSocket event subscriptions.

## Validation

-
  T030 \[US1\] Add a real echo-mode subprocess integration test in `tests/integration/runtime_acp/test_nate_oha_agent_lifecycle.py` covering:

  - process launch;
  - ACP initialization;
  - capability negotiation;
  - session creation;
  - prompt exchange;
  - event receipt;
  - clean shutdown.

-
  T031 \[US1\] Add a runtime-level integration test in `tests/integration/quickstart/test_nate_oha_swarm_start.py` covering multiple echo-mode agents through `RuntimeDaemon`, scheduler status, inspection, and shutdown.

**Checkpoint**: The runtime no longer simulates agent processes. Echo-mode agents run through the full nate-oha and ACP pipeline.

------------------------------------------------------------------------

# Phase 4: User Story 2 — Persist and Resume ACP Conversations

**Priority**: P2

**Goal**: Conversation IDs come from ACP, are persisted by `nate_ntm`, and are supplied back to nate-oha during resume.

**Independent test**: Create a conversation, persist the `session/new` result, stop the runtime, resume with `--resume`, receive the prior conversation history, and continue the same conversation.

## Conversation persistence

-
  T040 \[US2\] Remove deterministic or synthetic conversation-ID generation from `src/nate_ntm/runtime/acp_client.py` and all related helpers.
-
  T041 \[US2\] Update `NateOhaAcpClient.start_agent` so a new launch:

  - obtains the canonical session ID from ACP `session/new`;
  - returns it as part of the active session;
  - does not generate or infer an ID locally.

-
  T042 \[US2\] Update `src/nate_ntm/runtime/daemon.py` and `src/nate_ntm/runtime/metadata_store.py` so a newly returned ACP session ID is persisted into the corresponding `AgentMetadata.conversation_id`.
-
  T043 \[P\] \[US2\] Add metadata tests in `tests/unit/runtime/test_metadata_store.py` proving ACP-provided conversation IDs round-trip unchanged and remain opaque.

## Resume flow

-
  T044 \[US2\] Update `NateOhaLaunchSpec` construction so an agent with a persisted conversation ID receives:

  ```
  --resume CONVERSATION_ID
  ```
-
  T045 \[US2\] Implement the nate-oha-defined ACP establishment flow after launching with `--resume` in `src/nate_ntm/runtime/acp_client.py`.
-
  T046 \[US2\] Verify that the resumed ACP session reports the same canonical session ID as persisted metadata and fail startup with an actionable error on disagreement.
-
  T047 \[US2\] Ensure resumed ACP history flows through the same event translation and `AgentEventStream` pipeline used for newly generated events, without adding a second durable event store.
-
  T048 \[US2\] Update runtime resume logic in `src/nate_ntm/runtime/daemon.py` so it:

  - requires a persisted conversation ID for agents being resumed;
  - delegates session reconstruction to nate-oha and ACP;
  - does not call obsolete conversation-allocation helpers.

## Validation

-
  T049 \[US2\] Add a real echo-mode integration test in `tests/integration/runtime_acp/test_nate_oha_resume.py` that:

  - creates a session;
  - emits multiple identifiable events;
  - persists the session ID;
  - stops nate-oha;
  - relaunches with `--resume`;
  - receives the prior conversation history;
  - sends a new prompt;
  - verifies continuation on the same conversation.

-
  T050 \[US2\] Add a runtime-level create → shutdown → resume test in `tests/integration/quickstart/test_nate_oha_swarm_resume.py` covering metadata persistence, agent reconstruction, status, event inspection, and shutdown.

**Checkpoint**: Conversation identity and durable history are entirely owned by nate-oha; `nate_ntm` persists only the opaque session ID and transiently projects ACP events.

------------------------------------------------------------------------

# Phase 5: User Story 3 — Use One ACP Path for Echo and Agent Modes

**Priority**: P3

**Goal**: Echo and agent modes use the same process, ACP, event, shutdown, and resume implementation.

**Independent test**: Run the same lifecycle once with `runtime.mode=echo` and once with `runtime.mode=agent`; only the configuration differs.

-
  T060 \[US3\] Remove `FakeAcpClient` and all runtime selection paths that construct it from:

  - `src/nate_ntm/runtime/acp_client.py`;
  - `src/nate_ntm/runtime/adapters.py`;
  - `src/nate_ntm/config/runtime_config.py`;
  - `src/nate_ntm/cli.py`.

-
  T061 \[US3\] Replace binary fake/real ACP adapter selection with nate-oha runtime mode selection in `src/nate_ntm/config/runtime_config.py`.
-
  T062 \[US3\] Update runtime construction in `src/nate_ntm/runtime/adapters.py` or its replacement so all ACP-enabled agents receive `NateOhaAcpClient`, regardless of echo or agent mode.
-
  T063 \[US3\] Remove fake-ACP-specific tests and fixtures under `tests/` that simulate conversations, turns, or agent status without launching nate-oha.
-
  T064 \[US3\] Add a shared lifecycle test parametrized across echo and agent modes in `tests/integration/runtime_acp/test_nate_oha_runtime_modes.py`.

  Agent-mode execution may require configured credentials and should be run through an explicit pytest command or marker, but the default test collection must remain complete and discoverable through:

  ```
  uv run pytest
  ```
-
  T065 \[US3\] Remove `OpenHandsAcpClient` and its HTTP-specific tests from:

  - `src/nate_ntm/runtime/acp_client.py`;
  - `tests/unit/runtime/`;
  - `tests/integration/runtime_acp/`.

**Checkpoint**: There is exactly one ACP implementation: `NateOhaAcpClient`. Echo and agent behavior differ only through nate-oha configuration.

------------------------------------------------------------------------

# Phase 6: User Story 4 — Optional Real Agent Mail

**Priority**: P4

**Goal**: Agent Mail is absent when disabled and uses only the real `mcp_agent_mail` integration when enabled.

**Independent test**: Run a swarm successfully with Agent Mail disabled, then run an Agent Mail-enabled swarm against a real server and verify project/identity configuration reaches nate-oha.

## Runtime model

-
  T070 \[US4\] Replace adapter-kind-based Agent Mail configuration with explicit enabled/disabled semantics in `src/nate_ntm/config/runtime_config.py`.
-
  T071 \[US4\] Update runtime adapter construction so:

  - disabled Agent Mail produces no Agent Mail client;
  - enabled Agent Mail constructs `McpAgentMailClient`;
  - no fallback or fake implementation exists.

-
  T072 \[US4\] Remove `FakeAgentMailClient`, its configuration values, and runtime callers from:

  - `src/nate_ntm/runtime/agent_mail_client.py`;
  - `src/nate_ntm/runtime/adapters.py`;
  - `src/nate_ntm/config/runtime_config.py`;
  - `src/nate_ntm/cli.py`.

## nate-oha configuration

-
  T073 \[US4\] Update `NateOhaLaunchSpec` so Agent Mail-disabled launches set or inherit:

  ```
  features.agent_mail.enabled=false
  ```

  and omit project, identity, credentials, and upstream URL overrides.
-
  T074 \[US4\] Update `NateOhaLaunchSpec` so Agent Mail-enabled launches provide:

  ```
  features.agent_mail.enabled=true
  features.agent_mail.project=…
  features.agent_mail.agent_identity=…
  features.agent_mail.credentials_ref=…
  features.agent_mail.upstream_url=…
  ```
-
  T075 \[US4\] Update create and resume flows in `src/nate_ntm/runtime/daemon.py` so real Agent Mail project and identity data are established only when Agent Mail is enabled.
-
  T076 \[US4\] Ensure Agent Mail connection or registration failures prevent startup only when Agent Mail is enabled and produce actionable runtime errors.

## Validation

-
  T077 \[P\] \[US4\] Add an Agent Mail-disabled integration test in `tests/integration/runtime_mail/test_agent_mail_disabled.py` proving create, ACP startup, shutdown, and resume work without an Agent Mail server.
-
  T078 \[US4\] Add Agent Mail integration tests in `tests/integration/runtime_mail/test_mcp_agent_mail_runtime.py` that require a reachable `mcp_agent_mail` instance and fail clearly when the configured service cannot be contacted.
-
  T079 \[US4\] Remove tests that skip or silently fall back when an Agent Mail-dependent test cannot reach the configured real service.

**Checkpoint**: Agent Mail is genuinely optional, but when enabled it is always real.

------------------------------------------------------------------------

# Phase 7: Cross-Cutting Cleanup and Runtime Simplification

**Purpose**: Remove obsolete architecture as part of the migration rather than preserving compatibility indefinitely.

-
  T080 Remove remaining legacy ACP abstractions, helper methods, comments, and configuration references that describe:

  - HTTP OpenHands ACP;
  - locally allocated conversations;
  - turn-centric runtime APIs;
  - fake ACP implementations.

-
  T081 Remove remaining fake Agent Mail code, tests, configuration values, and documentation.
-
  T082 Remove direct OpenHands or nate-oha configuration construction from `src/nate_ntm/`; all agent launches must use a base JSON file plus `NateOhaLaunchSpec` overrides.
-
  T083 Review `src/nate_ntm/runtime/adapters.py`; either simplify it to construct only real runtime integrations or replace it with a more accurate factory module such as `runtime/integrations.py`.
-
  T084 Update runtime state and scheduler code to remove placeholder subprocess handles, transitional status simulation, and code paths no longer reachable after real nate-oha process supervision is active.
-
  T085 Update error types and logging across:

  - `src/nate_ntm/runtime/acp_client.py`;
  - `src/nate_ntm/runtime/scheduler.py`;
  - `src/nate_ntm/runtime/daemon.py`;
  - `src/nate_ntm/runtime/runner.py`;

  so process, ACP, resume, configuration, and Agent Mail failures remain distinguishable and actionable.

**Checkpoint**: No production or test architecture depends on the superseded fake or HTTP ACP models.

------------------------------------------------------------------------

# Phase 8: Documentation and Final Validation

-
  T090 Update `specs/005-nate-oha-migration/quickstart.md` with:

  - echo-mode startup;
  - agent-mode startup;
  - base nate-oha configuration;
  - conversation creation and resume;
  - Agent Mail-disabled operation;
  - Agent Mail-enabled operation.

-
  T091 Update `README.md` with the new runtime architecture and link to the Epic 005 quickstart.
-
  T092 Update historical specs and guidance that may otherwise mislead implementers, including:

  - `specs/001-swarm-runtime-orchestrator/`;
  - `specs/002-nate-oha-acp-adapter/`;
  - `NATE_OHA_GUIDE.md`;
  - `AGENTS_MK2.md`.

  Preserve history where useful, but clearly identify superseded architecture.
-
  T093 Run the complete default suite:

  ```
  uv run pytest
  ```

  The default invocation must collect the complete test suite rather than an artificially reduced subset.
-
  T094 Run focused echo-mode ACP integration tests:

  ```
  uv run pytest tests/integration/runtime_acp
  ```
-
  T095 Run Agent Mail integration tests with a real configured service and confirm they fail clearly when the service is unavailable.
-
  T096 Run agent-mode integration tests with valid LLM credentials using explicit pytest selection.
-
  T097 Add or update a full runtime integration test in `tests/e2e/test_nate_oha_runtime.py` covering:

  ```
  CLI
    → RuntimeDaemon
    → Scheduler
    → NateOhaAcpClient
    → nate-oha acp
    → ACP event stream
    → runtime API
    → shutdown
    → resume
  ```
-
  T098 Record validation results, intentional test removals, and any deferred work in `specs/005-nate-oha-migration/plan_feedback.md`.

------------------------------------------------------------------------

# Dependencies and Execution Order

## Phase dependencies

- Phase 1 is required before protocol implementation.
- Phase 2 blocks all subsequent runtime work.
- Phase 3 establishes the real nate-oha process and ACP path.
- Phase 4 depends on Phase 3 and adds ACP-owned persistence and resume.
- Phase 5 depends on Phases 3–4 and removes parallel ACP implementations.
- Phase 6 depends on the launch specification and real nate-oha process path, but may otherwise proceed alongside late Phase 4 or Phase 5 work.
- Phases 7–8 follow the completed migration.

## Parallel work

The following work may proceed concurrently after Phase 2:

- ACP event translation;
- launch-specification tests;
- runtime configuration/CLI updates;
- Agent Mail contract validation;
- metadata model updates.

Do not mark tasks parallel when they modify the same central modules, especially:

- `src/nate_ntm/runtime/acp_client.py`;
- `src/nate_ntm/runtime/daemon.py`;
- `src/nate_ntm/runtime/scheduler.py`;
- `src/nate_ntm/config/runtime_config.py`.

## Migration checkpoints

### Checkpoint A: Real echo-mode agent

After Phase 3, one echo-mode nate-oha agent can be launched, prompted, inspected, and stopped through the real ACP stack.

### Checkpoint B: Resume

After Phase 4, the same agent can be stopped and resumed using its ACP-owned conversation ID, with prior conversation history visible through ACP.

### Checkpoint C: Single ACP implementation

After Phase 5, no fake or generic HTTP ACP implementation remains.

### Checkpoint D: Optional real Agent Mail

After Phase 6, swarms work both without Agent Mail and with a real Agent Mail server.

### Checkpoint E: Migration complete

After Phase 8, the runtime operates exclusively through the new nate-oha architecture and the documented validation scenarios pass.

------------------------------------------------------------------------

# Implementation Guidance

- Prefer maintained libraries over custom protocol or process infrastructure.
- Use `uv add` for dependencies and `uv run` for project commands.
- Do not preserve obsolete code or tests merely to maintain compatibility.
- Prefer a few strong subprocess integration tests over extensive mocked protocol tests.
- Do not mock ACP framing, session creation, or event transport when echo-mode nate-oha can exercise the real path.
- Remove obsolete implementations as their replacements become functional rather than deferring all deletion until the end.
- Keep ACP protocol models isolated from the scheduler, daemon, and runtime API through explicit translation boundaries.
