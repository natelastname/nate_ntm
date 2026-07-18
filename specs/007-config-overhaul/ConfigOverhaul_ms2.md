# Milestone 2

## Objective

Finish the persistence/configuration overhaul so that:

- the entire durable swarm is one `SwarmState` Pydantic object;
- every `AgentState` contains a required, fully resolved `NateOhaConfig`;
- that embedded config is the sole source of Nate OHA configuration at launch;
- the only separate ACP-specific durable field is `conversation_id`;
- the old persistence format and legacy compatibility fields are not supported.

## Current implementation

The launch command is already mostly correct:

```
config_path = materialize_nate_oha_config(
    config=agent_state.nate_oha_config,
)

argv = [
    executable,
    “acp”,
    “--config”,
    str(config_path),
]

if agent_state.conversation_id:
    argv.extend(["--resume”, agent_state.conversation_id])
```

Do not replace or regress this behavior.

## Required changes

### 1. Tighten AgentState

Change it so that `nate_oha_config` is required.

Remove:

```
agent_mail_identity
agent_mail_credentials_ref
```

Use strict Pydantic validation so obsolete fields are rejected rather than silently accepted. Prefer:

```
class AgentState(BaseModel):
    model_config = ConfigDict(extra="forbid”)

    agent_id: str
    display_name: str
    nate_oha_config: NateOhaConfig
    conversation_id: str | None = None
```

Retain other genuinely runtime-owned durable fields only when they are still needed, such as restart policy or last-known status.

### 2. Remove Agent Mail environment translation

`NateOhaConfig.features.agent_mail` is already part of the materialized config file.

Remove construction of:

```
AGENT_MAIL_PROJECT
AGENT_MAIL_AGENT
AGENT_MAIL_TOKEN
AGENT_MAIL_UPSTREAM_URL
```

from `NateOhaAcpClient._build_env()`.

Do not reconstruct Nate OHA configuration through environment variables. `_build_env()` may retain unrelated process-correlation variables if still needed.

Remove legacy checks that inspect:

```
agent_state.agent_mail_identity
agent_state.agent_mail_credentials_ref
RuntimeConfig.agent_mail_project
RuntimeConfig.agent_mail_upstream_url
```

The old format is unsupported.

### 3. Remove fallbacks from tests

In:

```
tests/integration/quickstart/test_resume_swarm_us2.py
```

remove `_get_persisted_identity_and_conversation()` fallback behavior. Read Agent Mail identity directly from the required embedded `NateOhaConfig`.

Tests must fail if an `AgentState` does not contain a valid effective config.

### 4. Rewrite legacy runtime-mail fixtures

In:

```
tests/integration/runtime_mail/test_resume_error_paths_us2.py
```

remove fixtures and tests that create agents using:

```
AgentState(agent_mail_identity=…)
```

Every `AgentState` fixture must contain a valid `NateOhaConfig`.

Delete or rewrite the “incomplete legacy metadata” test. Legacy persisted state is not supported.

### 5. Preserve correct conversation-ID semantics

`conversation_id` is an opaque ACP-owned value.

Do not invent a local preflight comparison against some second conversation ID.

Test instead that:

- the persisted ID is passed unchanged through `--resume`;
- the ACP connection attempts to load that same session;
- a rejected or nonexistent session produces an actionable startup failure through the actual Nate OHA/ACP path.

Do not silently weaken an existing acceptance requirement merely to match dev-mode behavior.

## Effective Nate OHA configuration

“Effective configuration” means the complete `NateOhaConfig` after all base configuration, swarm-level changes, per-agent changes, prompt settings, model settings, and Agent Mail provisioning values have already been applied.

Persist that resulting model.

Do not persist:

- the original config path;
- unapplied override expressions;
- separate Agent Mail identity or credentials fields;
- materialized temporary config paths.

## Validation

Use the project-managed environment only:

```
uv sync
uv run pytest
```

Do not use raw `pytest`, `PYTHONPATH=src`, `pip install -e .`, mocks, or skip markers to bypass dependency or environment problems.

Run focused tests while developing, then finish with:

```
uv run pytest
```

Report:

- code changed;
- tests changed or deleted;
- focused test results;
- full-suite result;
- any genuine remaining limitation.

Do not introduce `ManagedSwarm` or `ManagedAgent` in this task.

## Implementation Checklist

- [ ] Tighten `AgentState` model
  - [ ] Make `nate_oha_config` required
  - [ ] Remove `agent_mail_identity` and `agent_mail_credentials_ref`
  - [ ] Enable strict Pydantic validation with `extra="forbid"`
  - [ ] Retain only still-needed runtime-owned durable fields (e.g., restart policy, last-known status)
- [ ] Remove Agent Mail environment translation from `NateOhaAcpClient._build_env()`
  - [ ] Remove construction of `AGENT_MAIL_PROJECT`, `AGENT_MAIL_AGENT`, `AGENT_MAIL_TOKEN`, `AGENT_MAIL_UPSTREAM_URL`
  - [ ] Remove legacy checks for `agent_state.agent_mail_identity`, `agent_state.agent_mail_credentials_ref`, `RuntimeConfig.agent_mail_project`, `RuntimeConfig.agent_mail_upstream_url`
  - [ ] Ensure `_build_env()` only retains unrelated, still-needed process-correlation variables
- [ ] Update tests for new config semantics
  - [x] `tests/integration/quickstart/test_resume_swarm_us2.py` reads Agent Mail identity directly from embedded `NateOhaConfig`
  - [x] Remove `_get_persisted_identity_and_conversation()` fallback behavior
  - [x] Ensure tests fail if an `AgentState` lacks a valid effective config
- [ ] Rewrite legacy runtime-mail fixtures and tests
  - [ ] Update `tests/integration/runtime_mail/test_resume_error_paths_us2.py` fixtures to always include valid `NateOhaConfig`
  - [ ] Delete or rewrite the "incomplete legacy metadata" test to drop legacy persisted state support
- [ ] Verify conversation ID behavior
  - [ ] Ensure `conversation_id` is passed unchanged to `--resume`
  - [ ] Confirm ACP attempts to load the same session
  - [ ] Ensure rejected/nonexistent sessions surface as actionable startup failures via the real Nate OHA/ACP path
  - [ ] Avoid introducing any secondary/local comparison of conversation IDs
- [ ] Ensure only effective `NateOhaConfig` is persisted
  - [ ] Persist the fully resolved `NateOhaConfig` only
  - [ ] Do not persist original config path, unapplied overrides, separate Agent Mail identity/credentials, or materialized temp config paths
- [ ] Validation and reporting
  - [ ] Run `uv sync`
  - [ ] Run focused tests relevant to the changes
  - [ ] Run full test suite with `uv run pytest`
  - [ ] Document: code changed, tests changed/deleted, focused test results, full-suite result, and any remaining limitations

