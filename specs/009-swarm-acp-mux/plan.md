# Implementation Plan: SwarmACPMux (external ACP session router)

**Branch**: `[008-swarm-acp-mux]` | **Date**: 2026-07-18 | **Spec**: `specs/008-swarm-acp-mux/spec.md`

**Input**: SwarmACPMux feature spec (`specs/008-swarm-acp-mux/spec.md`) and the existing runtime orchestrator contracts (spec 001).

> This plan supersedes an earlier draft that over-committed to ACP SDK types in the runtime and modeled reserved swarm controls as `SessionUpdate` names. It keeps the core architecture but aligns the design with the current code and spec 001 contracts.

## Summary

Implement `SwarmACPMux` as a **connection-scoped routing object** between a single external ACP client session and the nate_ntm swarm runtime.

For each external ACP session, the mux:

- owns attachment state (`attached_agent_id`);
- manages a single forwarding task that subscribes to a **replay-capable per-agent ACP update stream** (exact `SessionUpdate` objects) provided by the ACP client layer;
- forwards those `SessionUpdate` objects (or a lightly transformed equivalent) to the external ACP connection;
- exposes logical **swarm-control operations** such as `_attach`, `_detach`, `_swarm_status`, and `_agent_detail`, invoked by the Swarm ACP server adapter when it detects the corresponding ACP extension requests.

The mux coordinates existing runtime services instead of introducing new runtimes or lifecycle managers:

- `RuntimeDaemon` remains authoritative for swarm and agent metadata, status, and retained event history, via the contracts in `specs/001-swarm-runtime-orchestrator/contracts/runtime-api.md`.
- `NateOhaAcpClient` (or another `SwarmAgentClient` implementation) continues to own per-agent ACP sessions and event publication.
- A replay-capable `AgentEventStream` abstraction provides ordered **replay-then-live** streams per agent, preserving the existing per-subscriber bounded-queue semantics.
- The Swarm ACP server adapter owns protocol-level decoding/encoding and maps between logical mux operations and concrete ACP method/notification shapes.

MVP goals:

- One `SwarmACPMux` per external ACP session; at most one attached agent at a time.
- Correct attach/detach semantics that never stop or tear down the agent itself.
- Logical swarm-control operations (`_swarm_status`, `_agent_detail`, `_attach`, `_detach`) that reuse the **existing runtime API shapes** for swarm status and agent detail.
- A single ordered replay-then-live stream of events for the attached agent, observable via both the ACP-facing mux and existing runtime subscribers.

## Technical Context

**Language/Version**

- Python 3.13+ (consistent with spec 001 quickstart and project toolchain).

**Primary Dependencies**

- Python stdlib: `asyncio`, `dataclasses`, `logging`, `typing`, `contextlib`.
- Runtime modules: `nate_ntm.runtime.daemon`, `state`, `swarm_state`, `events`.
- ACP integration: `NateOhaAcpClient`, `NateNtmAcpProtocolClient`, and the Swarm ACP server adapter.
- ACP-specific SDK types remain **at the adapter boundary**; the core runtime continues to use normalized JSON-serializable `AgentEvent` payloads.

**Storage**

- No new durable storage.
- Per-agent ACP update streams (e.g., `AcpUpdateStream[SessionUpdate]`) are in-memory, bounded, replay-capable structures owned by the ACP client layer.
- `AgentEvent` history used by the runtime control API remains separate, as defined in spec 001.
- `.nate_ntm/` remains the single source of truth for swarm metadata, agent metadata, and resume behavior.

**Testing**

- Unit tests:
  - `SwarmACPMux` behavior (attachment lifecycle, prompt/interrupt routing, close semantics, error cases).
  - `AgentEventStream` behavior (bounded history, per-subscriber queues, drop-oldest semantics, replay-then-live ordering, closure).
- Integration tests under `tests/integration/runtime_acp/`:
  - Real-path async tests paralleling `test_runtime_daemon_acp_async_real_path_epic005.py`, but passing through the mux and ACP adapter.
  - Reserved-operation routing: logical `_attach`, `_detach`, `_swarm_status`, `_agent_detail` mapped to mux methods, with precise error codes and attach→replay ordering.
- All tests invoked via `uv run pytest ...` per `AGENTS.md`.

**Target Platform**

- Linux-first runtime environment (dev and CI), in line with `.specify/memory/constitution.md`.
- macOS allowed for development; Windows out of scope.

**Project Type**

- Runtime subcomponent inside `src/nate_ntm/runtime/`.
- New module: `swarm_acp_mux.py` with a dataclass-style `SwarmACPMux` and helper error types.
- No new entrypoints; integration happens through the existing daemon and ACP adapter.

**Performance Goals**

- Preserve the current end-to-end latency for ACP updates as observed in existing runtime ACP tests.
- Avoid head-of-line blocking by keeping per-subscriber queues bounded with drop-oldest semantics.
- Ensure attach/detach and forwarding logic behave correctly under multiple concurrent external sessions.

**Constraints**

- `SwarmACPMux` remains a **thin, connection-scoped** coordinator:
  - It must not take ownership of agent lifecycle, scheduling, or ACP connections.
- The core runtime (`RuntimeDaemon`, `SwarmState`, `AgentEvent`) remains **ACP-agnostic**:
  - No direct dependency on ACP SDK `SessionUpdate` types inside `AgentEvent`.
  - ACP encoding/decoding lives in the ACP integration modules and the Swarm ACP server adapter.
- Swarm status and agent detail payloads must be consistent with spec 001:
  - `_swarm_status` reuses `swarm.get_overview` result shape.
  - `_agent_detail` reuses `agent.get_detail` result shape.

**Scale/Scope**

- One mux per external ACP session; each mux may be attached to different agents over its lifetime but never more than one at a time.
- Single-swarm runtime; routing across multiple swarms or projects is explicitly out of scope.

## Constitution Check

The nate_ntm constitution (v1.1.1, `.specify/memory/constitution.md`) emphasizes:

- `uv` as the sole Python dependency manager.
- Linux as the target runtime environment.
- A bias toward using well-maintained libraries instead of ad hoc copies.

For this feature:

- We do not change dependency management; dependencies remain declared in `pyproject.toml`/`uv.lock` and installed via `uv`.
- Implementation and tests run on the same Linux-first stack as the existing runtime ACP features.
- We reuse existing ACP integration and runtime event plumbing instead of introducing new protocol stacks.

Result: **PASS** – no constitution violations identified at planning time.

## Project Structure

### Documentation (this feature)

```text
specs/008-swarm-acp-mux/
├── spec.md      # SwarmACPMux feature specification (normative)
├── plan.md      # This implementation plan
├── research.md  # Design decisions and rationale for SwarmACPMux
├── data-model.md# Data/state model for mux + event streams
├── quickstart.md# Testable integration scenario for the mux
└── contracts/
    └── swarm-acp-mux-session.md  # Logical ACP session contract for mux
```

### Source Code (repository root)

```text
src/
└── nate_ntm/
    └── runtime/
        ├── daemon.py             # Runtime daemon and swarm management
        ├── state.py              # SwarmState, AgentState, etc.
        ├── swarm_state.py        # Swarm-level helpers
        ├── events.py             # AgentEvent and event stream plumbing
        ├── acp_client.py         # NateOhaAcpClient (per-agent ACP)
        ├── acp_protocol_client.py# NateNtmAcpProtocolClient (wire-level ACP)
        ├── acp_event_translation.py
        └── swarm_acp_mux.py      # New SwarmACPMux implementation

tests/
├── unit/
│   └── runtime/
│       ├── test_swarm_acp_mux.py          # Mux behavior and error model
│       └── test_agent_event_stream.py     # Replay-capable event stream
└── integration/
    └── runtime_acp/
        ├── test_runtime_daemon_acp_async_real_path_epic005.py  # existing baseline
        ├── test_swarm_acp_mux_real_path.py                     # mux + ACP adapter
        └── test_reserved_swarm_controls.py                     # logical reserved ops
```

**Structure Decision**: Keep mux and event-stream logic in `nate_ntm.runtime`, aligned with the orchestrator components, and test them with the existing ACP integration harness under `tests/integration/runtime_acp/`.
