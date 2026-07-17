# Feature: Persist the Entire Swarm as a Single Pydantic Model

## Overview

The current persistence model should be replaced with a single Pydantic model representing the complete durable state of a swarm.

The goal is simple:

- the **entire persisted swarm** should be one Pydantic object;
- loading a swarm means loading that object;
- saving a swarm means writing that object back to disk.

This feature intentionally replaces the previous persistence format. Backwards compatibility is **not** required.

## Effective Nate OHA Configuration

Each persisted agent must contain its **effective Nate OHA configuration**:

```
from nate_oha.config import NateOhaConfig
```

The *effective* configuration is the configuration **after every override has already been applied**.

For example, suppose an agent was originally created from:

```
base.json
```

with

```
--set runtime.mode=echo
--set llm.model=gpt-5
--set prompt.soul_content="You are Agent 3"
```

The persisted model must contain the resulting `NateOhaConfig` object after those overrides have been applied.

It must **not** store:

- the original config path;
- the list of overrides;
- any information needed to reconstruct the effective configuration later.

The persisted `NateOhaConfig` itself is now the source of truth.

## Representative model

The long-term goal is for the **entire swarm to be represented by a single Pydantic model**.

Rather than storing a base configuration plus separate runtime metadata (such as Agent Mail identity, credentials, or `--set` overrides), each agent should persist its **effective `NateOhaConfig`** — the fully resolved configuration obtained after applying all swarm-level and agent-level overrides.

```
from pydantic import BaseModel
from nate_oha.config import NateOhaConfig

class AgentState(BaseModel):
    agent_id: str
    display_name: str

    # Fully resolved Nate OHA configuration for this agent.
    nate_oha_config: NateOhaConfig

    # ACP-owned conversation identifier used for --resume.
    conversation_id: str | None = None

class SwarmState(BaseModel):
    schema_version: int = 1
    swarm_id: str
    agents: dict[str, AgentState]
```

In particular, Agent Mail configuration should **not** be persisted separately. Values such as:

- `features.agent_mail.project`
- `features.agent_mail.agent_identity`
- `features.agent_mail.credentials_ref`
- `features.agent_mail.upstream_url`

already live inside the effective `NateOhaConfig`.

## Launching an Agent

When launching an agent:

1. retrieve the agent's `NateOhaConfig` from `SwarmState`;
2. serialize that model to a temporary configuration file;
3. launch `nate-oha` using that generated file;
4. if `conversation_id` exists, pass it via `--resume`.

Conceptually:

```
config_path = materialize(agent.nate_oha_config)

await acp_client.start_agent(
    config=config_path,
    resume_conversation_id=agent.conversation_id,
)
```

The temporary config file is runtime state and must never be persisted.

## Conversation IDs

Conversation IDs remain ACP-owned.

When Nate OHA creates a new session, the returned conversation ID is written into the corresponding `AgentState`, and the updated `SwarmState` is saved.

## Persistence

`MetadataStore` should load and save only the complete `SwarmState`.

Conceptually:

```
state = SwarmState.model_validate_json(path.read_text())

path.write_text(
    state.model_dump_json(indent=2) + “\n"
)
```

There should not be multiple authoritative persisted representations of the same swarm.

## Runtime State

`SwarmState` is durable state only.

It must not contain:

- subprocesses;
- ACP client instances;
- event queues;
- scheduler state;
- supervisor state;
- temporary config files;
- any other runtime-only objects.

Those remain in memory.

## Result

After this feature is complete, an entire swarm can be recreated using only:

- one `SwarmState`;
- the embedded `NateOhaConfig` for each agent;
- each agent's `conversation_id`.

No original configuration files, environment variables, or remembered `--set` overrides are required.

## Implementation Checklist

### 1. Define SwarmState and AgentState models
- [x] Add `SwarmState` and `AgentState` Pydantic models as described above.
  - [x] `AgentState` includes: `agent_id`, `display_name`, `nate_oha_config: NateOhaConfig`, `conversation_id: str | None = None` (currently modeled as a `str` with empty-string meaning "no conversation yet" in `runtime/swarm_state.py`).
  - [x] `SwarmState` includes: `schema_version: int = 1`, `swarm_id`, `agents: dict[str, AgentState]` (plus additional compatibility fields mirrored from `SwarmMetadata`).
- [x] Decide where these live (e.g. `src/nate_ntm/runtime/state_models.py` or similar) and avoid circular imports. (Implemented as `src/nate_ntm/runtime/swarm_state.py`.)
- [x] Add `model_validate_json` / `model_dump_json` usage helpers if needed. (See `SwarmState.from_json` / `SwarmState.to_json`.)

### 2. Replace SwarmMetadata / AgentMetadata as the persisted source of truth
- [x] Introduce `SwarmState` as the **only** persisted swarm representation. (On-disk metadata is now a single `swarm.json` containing a `SwarmState` object graph; `SwarmMetadata` / `AgentMetadata` are materialized from it via `MetadataStore`.)
- [x] Decide whether to:
  - [ ] (Preferred) Gradually retire `SwarmMetadata` / `AgentMetadata` in favor of `SwarmState`, or
  - [x] Keep them as thin, internal compatibility wrappers around `SwarmState` during migration. (Current design; see `runtime/metadata_store.py`.)
- [x] Ensure that Agent Mail–related fields (project, identity, credentials ref, upstream URL) are removed from separate metadata and only come from `NateOhaConfig`. (Runtime now prefers `AgentState.nate_oha_config.features.agent_mail` when available and falls back to legacy `RuntimeConfig`/`AgentMetadata` only for older swarms.)

### 3. Update MetadataStore to work with SwarmState only
- [x] Change `MetadataStore` to load and save a single `SwarmState` JSON file.
  - [x] Replace `load_swarm_metadata` / `save_swarm_metadata` with `load_swarm_state` / `save_swarm_state` (or equivalent names) that operate on `SwarmState`. (`load_swarm_metadata` / `save_swarm_metadata` now delegate to these helpers.)
  - [x] Remove or deprecate the per-agent files under `.nate_ntm/agents/`. (All per-agent JSON files have been replaced by a single `swarm.json`; legacy helpers have been removed from the code.)
- [x] Re-implement `load_agent_metadata` / `save_agent_metadata` semantics, if they must remain, as convenience helpers around `SwarmState`:
  - [x] `load_agent_*` loads the full `SwarmState`, then returns `state.agents[agent_id]`.
  - [x] `save_agent_*` loads `SwarmState`, mutates a single `AgentState`, and re-persists the full `SwarmState`.
- [x] Preserve atomic write semantics when persisting `SwarmState`. (Uses `_atomic_write_json` together with `SwarmState.model_dump(mode="json")`.)
- [x] Remove any remaining assumptions in `MetadataStore` about separate `swarm.json` vs per-agent JSON files.

### 4. Persist effective NateOhaConfig per agent
- [x] Identify and implement the point in the runtime where a `NateOhaConfig` has had **all** overrides applied (base file + `--set` + swarm-level + agent-level overrides) – see `build_nate_oha_launch_spec` / `build_effective_nate_oha_config` and `RuntimeDaemon.create`.
- [x] Add `nate_oha_config: NateOhaConfig` to `AgentState`/`PersistedAgentState` and persist it via `SwarmState` and `MetadataStore` for all created agents. (Legacy `launch_config` / `model` / `task_description` fields have been removed from the durable representation in favour of `NateOhaConfig`.)
- [x] Confirm that `NateOhaConfig` can be serialized and deserialized reliably as part of `SwarmState` using Pydantic.
- [x] Add tests to verify round-trip persistence of `NateOhaConfig` inside `AgentState` and to exercise `RuntimeDaemon.create` wiring.

### 5. Wire SwarmState through the runtime daemon and scheduler
- [x] Update `RuntimeDaemon` to rely on `SwarmState` as the persisted swarm representation:
  - [x] `create` constructs an initial `SwarmMetadata` (with agents if requested) and persists it via `MetadataStore`, which converts it to a `SwarmState` before writing `swarm.json`.
  - [x] `resume` loads `SwarmState` via `MetadataStore.load_swarm_state` / `load_swarm_metadata` and passes the materialized `SwarmMetadata` view to `AgentSupervisor`, `RuntimeScheduler`, etc.
- [x] Ensure any code that currently iterates over `SwarmMetadata.agents` is updated to work with the `SwarmState.agents` / `AgentState` mapping via the compatibility layer in `MetadataStore` (no direct reads of per-agent JSON files remain).
- [x] Remove any references to `AgentMetadata`-specific fields that are now part of `NateOhaConfig`. (Runtime code no longer consults `launch_config`, `model`, or `task_description`; effective Nate OHA behavior is driven via `AgentMetadata.nate_oha_config` / `AgentState.nate_oha_config`.)

### 6. Implement agent launch from NateOhaConfig
- [x] Add a helper to materialize `NateOhaConfig` into a temporary configuration file:
  - [x] Serialize `agent.nate_oha_config` to a JSON file in a temporary directory. (See `runtime/nate_oha_launch.py:materialize_nate_oha_config`.)
  - [x] Ensure the file is treated as runtime-only and **never** persisted as part of durable state. (Callers are responsible for cleaning up the temporary directory.)
- [ ] Update the ACP client / agent launch path to:
  - [ ] Retrieve `AgentState` from `SwarmState` (or `AgentMetadata.nate_oha_config` as a compatibility view).
  - [ ] Call the materialization helper to obtain a config path.
  - [ ] Launch `nate-oha` with that config file.
  - [ ] If `AgentState.conversation_id` is non-empty, pass it via `--resume` (or the equivalent ACP parameter).
- [ ] Add tests that verify that agents are launched with the materialized effective configuration, not the original base file plus overrides.

### 7. Conversation ID persistence in SwarmState
- [x] Move `conversation_id` persistence from `AgentMetadata` into `AgentState`. (`AgentState.conversation_id` is now the durable field in `SwarmState`; `AgentMetadata.conversation_id` is a thin view over that value via `MetadataStore`.)
- [x] Update `acp_client` to:
  - [x] Read `conversation_id` from `AgentState` (via `MetadataStore.load_agent_metadata` / `AgentMetadata.conversation_id`) to decide between `load_session` vs `new_session`.
  - [x] When a new session is created, update `AgentState.conversation_id` and persist the updated `SwarmState` via `MetadataStore`. (`NateOhaAcpClient.start_agent_async` writes back the ACP-assigned `session_id` through `MetadataStore.save_agent_metadata`.)
- [x] Ensure this update path uses the same atomic write semantics as other `SwarmState` writes. (Conversation IDs are persisted through the same `_atomic_write_json` path in `MetadataStore.save_swarm_state`.)
- [x] Add tests that cover:
  - [x] Creating a new conversation and persisting its ID. (See `tests/integration/runtime_acp/test_runtime_daemon_acp_async_real_path_epic005.py` and `tests/e2e/test_real_runtime_nate_oha_agent_mail.py`.)
  - [x] Resuming from an existing `conversation_id` stored in `SwarmState`. (The same integration/US2 tests validate persistence across create→resume.)

### 8. Maintain and update runtime invariants
- [ ] Port any validation currently done in `SwarmMetadata.validate` to equivalent checks on `SwarmState`:
  - [ ] `swarm_id` matches the current `RuntimeConfig`.
  - [ ] Project path invariants still hold where appropriate.
- [ ] Reconcile Agent Mail and ACP invariants (for example, FR-009) with the new model:
  - [ ] Ensure Agent Mail project ID checks still occur against the right fields (now inside `NateOhaConfig` where applicable).
  - [ ] Ensure agent identities and credentials ref checks continue to function, now driven by the effective configuration.

### 9. Cleanup of legacy fields and structures
- [x] Remove any unused or redundant fields from older metadata structures (`launch_config`, `model`, `task_description`, etc.) once all call sites are migrated. (See `runtime/swarm_state.py` and `runtime/metadata_store.py` for the final durable shape.)
- [x] Delete or repurpose the `.nate_ntm/agents/` directory and any code that depends on per-agent JSON files. (The runtime no longer creates or consumes per-agent JSON files; only `swarm.json` is used.)
- [ ] Audit the codebase for any remaining references to `SwarmMetadata` / `AgentMetadata` and either:
  - [ ] Migrate them to `SwarmState` / `AgentState`, or
  - [ ] Clearly mark and isolate any remaining compatibility shims.

### 10. Testing and migration validation
- [x] Update unit tests that:
  - [x] Construct `SwarmMetadata` / `AgentMetadata` directly.
  - [x] Assert on the current single-file `swarm.json` layout under `.nate_ntm/` (no per-agent JSON files).
- [x] Update integration tests that:
  - [x] Inspect on-disk metadata layout.
  - [x] Previously depended on separate `swarm.json` + per-agent files, to expect only `swarm.json`.
- [x] Add new tests for:
  - [x] `SwarmState` / `AgentState` persistence and validation.
  - [ ] Agent launch using materialized `NateOhaConfig`.
  - [x] Conversation ID creation and reuse across create→resume flows.
- [ ] (Optional) Add a one-time migration or guard that fails fast if old-style metadata files are detected, since backward compatibility is explicitly not required.

