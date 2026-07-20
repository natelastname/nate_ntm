# Implementation Plan: SwarmACPMux (external ACP session router)

**Branch**: `[009-swarm-acp-mux]` | **Date**: 2026-07-20 | **Spec**: `specs/009-swarm-acp-mux/spec.md`

**Input**: SwarmACPMux feature specification (`specs/009-swarm-acp-mux/spec.md`) and the existing runtime orchestrator contracts (spec 001).

## Summary

Implement `SwarmACPMux` as a connection-scoped routing layer between one external swarm ACP session and the swarm runtime, built on top of the typed ACP session streaming layer from Epic 008.

For each external ACP session, the mux:

- exposes swarm-level control operations such as `_attach`, `_detach`, `_swarm_status`, and `_agent_detail`;
- attaches the session to at most one agent at a time and may switch attachments over its lifetime;
- consumes the attached agent’s typed ACP updates via `subscribe_acp_updates()` as an async iterator of `ReceivedSessionUpdate`;
- forwards each underlying `SessionUpdate` to the external ACP connection using `ExternalACPConnection.session_update(...)`;
- routes ordinary prompt/interrupt requests to the attached agent through `SwarmAgentClient`.

The mux is a thin, connection-local coordinator. It does not implement ACP transport, replay buffers, subscriber queues, or telemetry history; these concerns are owned by Epic 008 and the existing runtime/ACP integration.

## Technical Context

<!--
  ACTION REQUIRED: Replace the content in this section with the technical details
  for the project. The structure here is presented in advisory capacity to guide
  the iteration process.
-->

**Language/Version**: Python 3.13+ (aligned with spec 001 and the project toolchain).

**Primary Dependencies**:
- Python stdlib: `asyncio`, `dataclasses`, `logging`, `typing`, `contextlib`.
- Internal runtime modules: `nate_ntm.runtime.daemon`, `state`, `swarm_state`, `events`, `acp_client`, `acp_protocol_client`.
- ACP typed session streaming layer (Epic 008) providing `AcpSessionUpdateStream`, `ReceivedSessionUpdate`, and `subscribe_acp_updates()`.

**Storage**:
- No new durable storage.
- Per-agent typed ACP update streams are in-memory, bounded, replay-capable structures owned by the ACP client layer.
- Existing `.nate_ntm/` swarm metadata and `AgentEvent` history remain the sources of truth for swarm/agent state.

**Testing**:
- Unit tests with `pytest` (invoked via `uv run pytest`), covering:
  - SwarmACPMux attachment lifecycle, forwarding behavior, failure handling, and concurrency.
- Integration tests under `tests/integration/runtime_acp/`, exercising:
  - Real-path ACP flows through the mux and ACP server adapter.
  - Reserved swarm-control operations and error mapping.

**Target Platform**: Linux-first runtime environment (development and CI). macOS allowed for development; Windows out of scope.

**Project Type**: Runtime subcomponent / library inside `src/nate_ntm/runtime/`, no new entrypoints.

**Performance Goals**:
- Preserve existing end-to-end ACP update latency as observed in current runtime ACP tests.
- Avoid head-of-line blocking by keeping per-subscriber queues bounded with drop-oldest semantics at the ACP streaming layer.
- Support multiple concurrent external ACP sessions, each with an independent mux instance.

**Constraints**:
- SwarmACPMux remains connection-scoped and must not own agent lifecycle or ACP transport.
- The runtime core (`RuntimeDaemon`, swarm state, `AgentEvent` history) remains ACP-agnostic; typed ACP objects stay confined to the ACP integration and mux layers.
- All new Python dependencies, if any, must be managed via `uv` and declared in `pyproject.toml` / `uv.lock`.

**Scale/Scope**:
- One mux per external ACP session; each mux may attach to different agents over its lifetime but never more than one at a time.
- Single-swarm runtime; cross-swarm routing and multi-project coordination are out of scope.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

For each plan, confirm at minimum:

- Dependency choices prefer mature libraries over custom code. Do not add "optional" dependencies; either depend on a library fully and declare it, or avoid it entirely.
- The target runtime is a Linux environment similar to the development/CI host. Any non-Linux targets are explicitly called out and justified.
- Any intentional deviations from the constitution are recorded in the "Complexity Tracking" table below.

For this feature:

- Dependencies are limited to Python stdlib and existing runtime/ACP integration modules; no new third-party libraries are introduced at this stage.
- All Python dependencies remain managed exclusively via `uv`, with `pyproject.toml` and `uv.lock` as the sources of truth.
- The implementation targets a Linux runtime environment consistent with development and CI.
- We do not introduce non-Linux targets or optional/soft dependencies.

Result: **PASS** – no constitution violations identified at planning time. The Complexity Tracking table below is intentionally left empty unless future changes require exceptions.

## Project Structure

### Documentation (this feature)

```text
specs/009-swarm-acp-mux/
├── spec.md        # SwarmACPMux feature specification (Epic 009, normative)
├── plan.md        # This implementation plan (/speckit.plan output)
├── research.md    # Phase 0 design decisions and resolved unknowns
├── data-model.md  # Phase 1 data/state model for mux + typed ACP streams
├── quickstart.md  # Phase 1 validation / test scenarios
└── contracts/
    └── swarm-acp-mux-session.md  # Logical ACP session contract for mux + adapter
# (tasks.md will be generated later by /speckit.tasks and is not created by /speckit.plan)
```

### Source Code (repository root)

```text
src/
└── nate_ntm/
    └── runtime/
        ├── daemon.py              # Runtime daemon and swarm management
        ├── state.py               # SwarmState, AgentState, etc.
        ├── swarm_state.py         # Swarm-level helpers
        ├── events.py              # AgentEvent and event stream telemetry
        ├── acp_client.py          # NateOhaAcpClient, AcpAgentSession, typed ACP streams
        ├── acp_protocol_client.py # NateNtmAcpProtocolClient (wire-level ACP)
        └── swarm_acp_mux.py       # New SwarmACPMux implementation (Epic 009)

tests/
├── unit/
│   └── runtime/
│       └── test_swarm_acp_mux.py          # Mux behavior, lifecycle, failure modes
└── integration/
    └── runtime_acp/
        ├── test_runtime_daemon_acp_async_real_path_epic005.py  # existing ACP baseline
        ├── test_swarm_acp_mux_real_path.py                     # real path through mux + adapter
        └── test_reserved_swarm_controls.py                     # reserved operations + error mapping
```

**Structure Decision**: Use the existing `nate_ntm.runtime` package as the home for `SwarmACPMux`, alongside the runtime daemon and ACP integration modules, and test it via unit tests under `tests/unit/runtime/` and integration tests under `tests/integration/runtime_acp/`.

## Complexity Tracking

> **Fill ONLY if Constitution Check has violations that must be justified**

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| [e.g., 4th project] | [current need] | [why 3 projects insufficient] |
| [e.g., Repository pattern] | [specific problem] | [why direct DB access insufficient] |
