# Implementation Plan: nate_OHA ACP Production Adapter (NateOhaAcpClient)

**Branch**: `[002-nate-oha-acp-adapter]` | **Date**: 2026-07-05 | **Spec**: `specs/002-nate-oha-acp-adapter/spec.md`

**Input**: Feature specification from `specs/002-nate-oha-acp-adapter/spec.md`

**Note**: This template is filled in by the `/speckit.plan` command. See `.specify/templates/plan-template.md` for the execution workflow.

## Summary

This plan describes how to implement the NateOhaAcpClient as the canonical production implementation of `BaseAcpClient` for `nate_ntm`. The runtime will use `NateOhaAcpClient` in production to launch and supervise one `nate_OHA` ACP process per managed agent, using the CLI and environment contract defined in `NATE_OHA_GUIDE.md`. The adapter will own each `nate_OHA` subprocess lifecycle, surface process and ACP events into the existing runtime event pipeline, and ensure that swarm shutdown/resume preserves Agent Mail identities and persisted OpenHands conversation identifiers for all `nate_OHA`-backed agents.

## Technical Context

**Language/Version**: Python 3.12 (project requires `>=3.12,<3.14.1`)

**Primary Dependencies**:
- `nate_ntm` runtime components (`runtime.acp_client`, `runtime.adapters`, `runtime.daemon`, `runtime.events`, `runtime.metadata_store`)
- CLI/runtime entrypoint: `nate-ntm` (`nate_ntm.cli:cli`)
- Third-party libraries: `typer` (CLI), `websockets` (runtime I/O), `python-dotenv` (environment configuration)
- External systems: `nate_OHA` CLI/ACP runtime (per `NATE_OHA_GUIDE.md`), Agent Mail service, OpenHands backend providing conversations

**Storage**:
- No new persistent datastore introduced by this feature
- Reuses existing runtime metadata mechanisms for swarm and agent state
- Relies on Agent Mail and OpenHands for durable coordination state and conversation history

**Testing**:
- `pytest` for unit and integration tests (`tests/unit`, `tests/integration`)
- New tests focused on:
  - Adapter selection behavior (`FakeAcpClient` vs `NateOhaAcpClient`)
  - `nate_OHA` process launch/supervision and failure handling
  - Shutdown/resume preserving Agent Mail identity and OpenHands conversation IDs
  - Event propagation from `nate_OHA` into `AgentEventStream`

**Target Platform**:
- Linux server environment running Python 3.12
- `nate_OHA` executable available on PATH (or otherwise discoverable) on the same host as the `nate_ntm` runtime

**Project Type**:
- Python library + CLI-driven swarm orchestrator runtime

**Performance Goals**:
- Satisfy spec success criteria:
  - SC-001: `nate_OHA`-backed swarm startup within 15 seconds in 95% of runs
  - SC-003: ACP events surfaced to clients with <1 second end-to-end latency in 95% of cases
  - SC-004: Process failure/restart handling correct in ≥95% of injected fault cases

**Constraints**:
- Adapter must conform to the `BaseAcpClient` abstraction and existing runtime event model
- All interaction with `nate_OHA` flows through `NateOhaAcpClient`; no direct calls from other runtime components
- Process supervision must avoid unbounded restart loops and must surface failures for policy-driven handling
- Must use `NATE_OHA_GUIDE.md` as the normative source of CLI and environment configuration

**Scale/Scope**:
- Typical swarms of O(10–100) active `nate_OHA`-backed agents
- Bounded by host CPU/memory and process limits; no additional distributed coordination introduced by this feature

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

The project constitution file (`.specify/memory/constitution.md`) is currently a template and does not yet define concrete principles or non-negotiable gates. For this feature, we apply the following implicit gates:

- Keep the design minimal: introduce no new top-level projects or packages beyond what is required to integrate `NateOhaAcpClient`.
- Prefer existing runtime patterns: reuse the current `BaseAcpClient` abstraction, event pipeline, and metadata store rather than introducing parallel mechanisms.
- Require test coverage: new behavior (adapter selection, process lifecycle, shutdown/resume, event propagation) must be covered by unit and/or integration tests.

At this stage of the plan, no constitution violations are identified, and no additional complexity needs justification. Complexity Tracking can remain empty unless later design changes add new projects, frameworks, or cross-cutting patterns beyond these gates.

## Project Structure

### Documentation (this feature)

```text
specs/002-nate-oha-acp-adapter/
├── spec.md              # Approved feature specification
├── plan.md              # This file (/speckit.plan command output)
├── research.md          # Phase 0 output (/speckit.plan command)
├── data-model.md        # Phase 1 output (/speckit.plan command)
├── quickstart.md        # Phase 1 output (/speckit.plan command)
├── contracts/           # Phase 1 output (/speckit.plan command)
└── tasks.md             # Phase 2 output (/speckit.tasks command - NOT created by /speckit.plan)
```

### Source Code (repository root)

```text
src/
└── nate_ntm/
    ├── cli.py                  # CLI entrypoint (nate-ntm)
    ├── api/                    # Public runtime API surface
    ├── config/                 # Runtime configuration helpers
    ├── runtime/
    │   ├── acp_client.py       # BaseAcpClient abstraction and ACP client types
    │   ├── adapters.py         # Adapter selection and wiring
    │   ├── agent_mail_client.py# Agent Mail integration
    │   ├── agents.py           # Agent definitions and lifecycle
    │   ├── daemon.py           # Long-lived swarm runtime process
    │   ├── events.py           # Runtime event model and AgentEventStream
    │   ├── metadata_store.py   # Swarm/agent metadata persistence
    │   ├── runner.py           # Runtime orchestration and main loop
    │   ├── scheduler.py        # Agent scheduling and work dispatch
    │   └── state.py            # Swarm/agent state management
    └── util.py                 # Shared utilities

tests/
├── unit/
│   ├── ...                     # Existing unit tests
│   └── (new) test_nate_oha_acp_client.py, test_adapter_selection.py
├── integration/
│   ├── ...                     # Existing integration tests
│   └── (new) test_nate_oha_end_to_end.py
└── test_util.py                # Existing utility tests
```

**Structure Decision**: Reuse the existing single-project layout (`src/nate_ntm`, `tests/`) and introduce `NateOhaAcpClient` and related wiring inside `src/nate_ntm/runtime` (primarily `acp_client.py`, `adapters.py`, and `daemon.py`). No new top-level packages or projects are required. Tests will be added under the existing `tests/unit` and `tests/integration` trees.

## Design Amendments (from FEEDBACK4.md)

This feature adopts a narrow but explicit expansion of the `BaseAcpClient` abstraction so that it reflects the real responsibilities of an ACP runtime adapter.

### BaseAcpClient as the ACP runtime adapter contract

For this feature, `BaseAcpClient` is treated as the runtime-facing contract for ACP-backed agent execution, not just a conversation/turn ID helper.

Required operations (to be implemented by both `FakeAcpClient` and `NateOhaAcpClient`):

- `ensure_conversation(agent_id: str) -> str` – ensure an ACP conversation exists for the agent.
- `start_agent(agent_id: str, *, metadata: AgentMetadata) -> None` – launch or attach to the ACP runtime backing the agent.
- `start_turn(agent_id: str, prompt: str | None = None) -> str` – initiate a new unit of work for the agent and return its identifier.
- `stop_agent(agent_id: str, *, timeout: float) -> None` – stop the ACP runtime for the agent with a bounded timeout and escalation.
- `get_status(agent_id: str) -> AcpAgentStatus` – report the current ACP/runtime status in a small, structured form.

Event delivery uses a callback style:

- `BaseAcpClient` accepts or is configured with `on_event: Callable[[AgentEvent], None] | None`.
- `NateOhaAcpClient` forwards ACP/runtime events through this callback.
- `AgentSupervisor` routes those events into `AgentEventStream` and the existing WebSocket/JSON-RPC pipeline; adapters do not talk directly to transports.

Lifecycle ownership boundary:

- `BaseAcpClient` implementations own ACP runtime lifecycle for managed agents (process launch, readiness checks, shutdown, and status reporting).
- `AgentSupervisor` owns in-memory runtime state and event routing, but does not spawn or kill ACP processes directly.
- `RuntimeDaemon` and the scheduler invoke the adapter via the `BaseAcpClient` interface rather than reaching around it.

NateOhaAcpClient is the concrete `BaseAcpClient` implementation responsible for nate_OHA process lifecycle, conversation setup, turn execution, status reporting, and event emission; it must not be hidden behind a separate sidecar-specific interface.

### Implementation outline for this amendment

These items will be reflected in `tasks.md` when `/speckit.tasks` is run:

- Verify `NATE_OHA_GUIDE.md` and extract the nate_OHA process launch contract (executable/command, arguments, required environment variables, readiness signal, shutdown behavior, version/self-check command, and conversation-id resume mechanism) as the authoritative reference for NateOhaAcpClient.
- Expand `BaseAcpClient` with `start_agent`, `stop_agent`, `get_status`, and event callback support.
- Update `FakeAcpClient` to implement the expanded lifecycle contract in memory.
- Remove `OpenHandsAcpClient` from production adapter selection while keeping fake vs real ACP modes clear.
- Implement `NateOhaAcpClient` as the real ACP runtime adapter against the expanded `BaseAcpClient` contract.
- Model a `NateOhaProcessRecord` inside `NateOhaAcpClient` and persist only the subset of that record needed for swarm shutdown/resume via the existing metadata store, surfacing structured status and events (not raw process internals) to the rest of the runtime.

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

No constitution violations or additional structural complexity have been introduced by this plan. The table below is intentionally left empty.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|---------------------------------------|
|           |            |                                       |

