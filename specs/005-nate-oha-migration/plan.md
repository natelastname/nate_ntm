# Implementation Plan: nate-oha Runtime Integration (Epic 005)

**Branch**: `005-nate-oha-migration`
**Date**: 2026-07-12
**Spec**: `specs/005-nate-oha-migration/spec.md`

## Summary

This epic migrates the swarm runtime from the legacy OpenHands-oriented ACP architecture to a nate-oha–centric runtime architecture.

Rather than treating ACP as a generic HTTP service, the runtime will treat `nate-oha acp` as the canonical implementation of an agent. Each swarm agent will be represented by a supervised nate-oha subprocess communicating over the official Agent Client Protocol (ACP) using the maintained ACP Python SDK.

This is intentionally an architectural replacement rather than an incremental migration. Existing ACP abstractions, fake adapters, and compatibility layers may be removed wherever they complicate the new design.

At the completion of this epic:

- every runtime agent is backed by a `nate-oha acp` subprocess;
- development (“echo”) and production (“agent”) modes exercise the same ACP implementation;
- conversation ownership belongs entirely to nate-oha;
- Agent Mail becomes a real optional integration rather than a simulated runtime component;
- `nate_ntm` is responsible only for orchestration, supervision, scheduling, persistence, and launch configuration.

------------------------------------------------------------------------

## Technical Context

### Language

Python 3.13 (managed through `uv`).

### Primary Dependencies

- `agent-client-protocol`
- `nate-oha`
- `mcp_agent_mail`
- existing runtime modules

No custom ACP implementation will be developed.

The runtime is expected to compose with upstream libraries whenever practical.

------------------------------------------------------------------------

### Runtime Responsibilities

After this migration the responsibilities are explicitly divided.

### nate_ntm

Owns:

- swarm metadata
- scheduling
- process supervision
- restart policy
- launch specification generation
- ACP event translation
- Agent Mail coordination
- runtime APIs

### nate-oha

Owns:

- ACP server
- conversation history
- prompt construction
- OpenHands integration
- LLM execution
- Agent Mail runtime feature
- replay semantics

### ACP SDK

Owns:

- protocol transport
- protocol negotiation
- JSON-RPC framing
- schema models
- callback dispatch
- request correlation

------------------------------------------------------------------------

## Launch Architecture

The runtime launches nate-oha using a shared configuration plus runtime-specific overrides.

Conceptually every agent launch becomes:

``` overflow-visible!
base_config.json
        │
        │
runtime state
        │
        ▼
LaunchSpec
        │
        ▼
nate-oha acp
    --config BASE
    --resume …
    --set …
```

Generating this launch specification becomes one of the primary responsibilities of `nate_ntm`.

The runtime no longer constructs complete OpenHands configuration internally.

------------------------------------------------------------------------

## ACP Architecture

The runtime owns one ACP implementation:

``` overflow-visible!
Scheduler
      │
      ▼
NateOhaAcpClient
      │
      ▼
ACP SDK
      │
      ▼
nate-oha
```

`NateOhaAcpClient` is not a thin protocol wrapper.

It is responsible for:

- launching nate-oha
- supervising the subprocess
- managing ACP connections
- translating ACP events
- exposing runtime-facing agent lifecycle operations

Protocol mechanics remain entirely inside the ACP SDK.

------------------------------------------------------------------------

## Conversation Ownership

Conversation identifiers belong exclusively to nate-oha.

The runtime:

- receives them from ACP;
- persists them;
- supplies them back during resume.

The runtime must never synthesize conversation identifiers.

Resume is therefore based entirely on ACP-owned conversation identity.

------------------------------------------------------------------------

## Agent Mail

Agent Mail becomes a runtime integration rather than a runtime simulation.

When disabled:

- no Agent Mail APIs are contacted;
- nate-oha receives no Agent Mail configuration;
- swarms remain fully functional.

When enabled:

- `mcp_agent_mail` is required;
- fake implementations are not used;
- runtime tests expecting Agent Mail require a reachable service.

------------------------------------------------------------------------

## Project Structure

No major restructuring of the repository is expected.

Implementation work is concentrated under:

``` overflow-visible!
src/nate_ntm/runtime/
```

with corresponding updates to:

``` overflow-visible!
tests/unit/runtime
tests/integration/runtime_acp
tests/integration/runtime_mail
tests/integration/quickstart
```

Detailed repository layout for this epic:

``` overflow-visible!
src/
└── nate_ntm/
    ├── runtime/
    │   ├── acp_client.py         # NateOhaAcpClient + ACP orchestration
    │   ├── adapters.py           # Swarm adapter wiring that calls NateOhaAcpClient
    │   ├── agent_mail_client.py  # Real Agent Mail integration
    │   ├── metadata_store.py     # Swarm metadata, including ACP conversation IDs
    │   ├── events.py             # Runtime events mapped from ACP events
    │   ├── scheduler.py          # Existing scheduler (reused)
    │   ├── runner.py             # Existing runtime runner (reused)
    │   └── ...                   # daemon.py, state.py, agents.py, etc.
    ├── api/
    │   ├── runtime_api.py        # Runtime control / event API
    │   └── ...                   # JSON-RPC server plumbing
    └── cli.py                    # nate_ntm CLI entrypoint

tests/
├── unit/
│   ├── runtime/
│   │   ├── test_acp_client.py              # NateOhaAcpClient behavior
│   │   ├── test_adapters_real_acp_t102.py  # Adapter wiring for real ACP
│   │   ├── test_agent_mail_client.py       # Real Agent Mail client integration
│   │   └── ...                             # daemon, scheduler, metadata_store, etc.
│   └── ...                                 # API, CLI, config
├── integration/
│   ├── runtime_acp/                        # Subprocess + ACP lifecycle
│   ├── runtime_mail/                       # Agent Mail integration + resume
│   └── quickstart/                         # Runtime CLI + WebSocket control/events
└── e2e/
        	└── test_real_runtime_nate_oha_agent_mail.py  # Full-stack nate-oha + Agent Mail

```


The previous HTTP ACP implementation and fake ACP implementation are expected to disappear during this migration.

------------------------------------------------------------------------

# Validation Strategy

This epic validates orchestration rather than ACP itself.

## Primary Validation

Integration tests should verify:

- launching nate-oha
- ACP initialization
- capability negotiation
- conversation creation
- conversation resume
- ACP event streaming
- graceful shutdown
- process restart
- Agent Mail integration (when enabled)

These tests should use the official ACP SDK and real nate-oha subprocesses.

Echo mode should be preferred whenever LLM behavior is irrelevant.

------------------------------------------------------------------------

## Secondary Validation

Unit tests should be limited to runtime-owned logic, including:

- launch specification construction
- scheduler behavior
- metadata persistence
- timeout handling
- restart policy
- ACP event translation
- runtime state transitions

Protocol mechanics should not be re-tested.

------------------------------------------------------------------------

## Agent Mode Validation

A smaller collection of integration tests should verify the same runtime lifecycle using:

``` overflow-visible!
runtime.mode = agent
```

with real LLM credentials.

------------------------------------------------------------------------

# Constitution Check

This design satisfies the project constitution.

### Library Usage

✔ Uses the maintained ACP SDK rather than implementing ACP.

✔ Uses nate-oha rather than embedding OpenHands runtime logic.

✔ Uses `mcp_agent_mail` directly rather than maintaining a fake implementation.

### Linux

✔ All process management assumes Linux and POSIX semantics.

### Dependency Philosophy

✔ No optional protocol implementations.

✔ No compatibility shims for obsolete ACP implementations.

✔ Preference given to upstream libraries over custom code.

------------------------------------------------------------------------

# Complexity Tracking

None.

The purpose of this epic is to **remove** architectural complexity by collapsing multiple ACP implementations into a single nate-oha–based architecture, eliminating fake runtime layers, and clearly separating runtime orchestration from protocol implementation.
