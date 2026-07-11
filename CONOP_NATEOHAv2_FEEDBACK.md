# Feedback on CONOP_NATEOHAv2: Nate OHA Runtime Integration

## 0. High‑level assessment

The CONOP correctly recenters the runtime around the **`nate-oha acp` process + config contract** instead of the older HTTP ACP abstraction. It lines up well with the direction already started in `specs/002-nate-oha-acp-adapter/` and the `NateOhaAcpClient` implementation, but it also **intentionally supersedes** several earlier decisions:

- The legacy `OpenHandsAcpClient` HTTP adapter is now explicitly obsolete.
- The in‑memory `FakeAcpClient` and `FakeAgentMailClient` are no longer acceptable long‑term foundations.
- `nate-oha`’s JSON config + `--set` interface should become the primary source of truth for agent runtime behaviour.

From the `nate_ntm` side, this epic is a **major architecture change** concentrated in:

- `src/nate_ntm/runtime/acp_client.py` (ACP adapters, especially `NateOhaAcpClient`).
- `src/nate_ntm/runtime/adapters.py` (adapter selection).
- `src/nate_ntm/runtime/agent_mail_client.py` (Agent Mail abstraction, fake vs real).
- `src/nate_ntm/config/runtime_config.py` (adapter selection + Agent Mail config resolution).
- `src/nate_ntm/runtime/daemon.py` and `src/nate_ntm/runtime/scheduler.py` (where ACP & Agent Mail are actually used).
- Specs and tests under `specs/002-nate-oha-acp-adapter/` and `tests/*` that still encode the older `nate_OHA` CLI and environment contract.

The good news: the current codebase already has a clean separation of responsibilities (daemon, scheduler, `AgentSupervisor`, adapters, metadata) and a rich test harness. The epic is mostly about **re‑anchoring those abstractions on `nate-oha acp` and its config model**, and about **deleting or radically simplifying the fake paths**, rather than inventing entirely new layers.

Below I go goal‑by‑goal, then call out migration strategy, risks, and concrete milestones.

---

## 1. Replace the ACP adapter architecture

### 1.1 Current ACP adapter design

Key pieces:

- **BaseAcpClient** (`src/nate_ntm/runtime/acp_client.py`):
  - Defines the runtime‑facing contract: `ensure_conversation`, `start_agent`, `start_turn`, `stop_agent`, `get_status` plus an `on_event: Callable[[AgentEvent], None] | None` callback (lines ~97–174).

- **FakeAcpClient** (same file, lines ~175–278):
  - Pure in‑memory dev/test implementation. No subprocesses, no real ACP.
  - `ensure_conversation` returns `"fake-conversation:{agent_id}"`.
  - `start_agent` just sets an internal state map to `"running"`.
  - `start_turn` increments a counter and may emit an `AgentEvent` of type `"TurnCompleted"` with `payload["adapter"] == "fake"`.
  - Used heavily in unit and quickstart tests, and as the default (`AdapterKind.FAKE`).

- **OpenHandsAcpClient** (HTTP adapter, lines ~280–469):
  - Talks to an OpenHands ACP server over HTTP using `/threads` and `/threads/{thread_id}/runs`.
  - Derives deterministic thread IDs using a UUID namespace + `swarm_id` + `project_path` + `agent_id`.
  - `start_agent` is explicitly a **no‑op** beyond ensuring a thread exists.
  - `start_turn` POSTs a run and returns a `run_id`.
  - Still used by tests (see `tests/unit/runtime/test_acp_client.py::test_openhands_acp_client_*`), but **not** wired into `RuntimeAdapters.create_runtime_adapters`.

- **NateOhaAcpClient** (production adapter, lines ~79–113, 513+):
  - Already implements most of the process‑lifecycle side of Feature 002:
    - Per‑agent `NateOhaProcessRecord` (`status`, `pid`, `last_exit_code`, `last_error`, `restart_count`).
    - `start_agent` launches a `nate_OHA` subprocess via `subprocess.Popen` using `_build_command` + `_build_env`, attaches a record, and emits events `nate_oha_process_started` and `nate_oha_process_ready` (lines ~575–676).
    - `stop_agent` performs `terminate` / `wait` / `kill` and emits `nate_oha_process_exited` or `nate_oha_process_crashed` (lines ~751–842).
    - `get_status` maps the record back to `AcpAgentStatus` (lines ~843–867).
  - `ensure_conversation` derives a deterministic, per‑agent UUID based on `swarm_id`, `project_path`, and `agent_id`, reusing an existing `AgentMetadata.conversation_id` when present and persisting a newly derived ID back into metadata (`MetadataStore`) when needed (lines ~513–573).
  - `_build_command` currently builds `['nate_OHA', '--enable-agent-mail']` when `metadata.agent_mail_identity` is non‑empty (lines ~869–887).
  - `_build_env` still uses the **old env‑based contract** from `NATE_OHA_GUIDE.md`:
    - Sets correlation vars `NATE_NTM_PROJECT_PATH`, `NATE_NTM_SWARM_ID`, `NATE_NTM_AGENT_ID`, and `NATE_NTM_AGENT_CONVERSATION_ID`.
    - Sets `LLM_MODEL` default `openai/gpt-4o`.
    - When Agent Mail is enabled (non‑empty `agent_mail_identity`), populates `AGENT_MAIL_PROJECT`, `AGENT_MAIL_AGENT`, `AGENT_MAIL_TOKEN`, `AGENT_MAIL_UPSTREAM_URL`, failing fast if config is incomplete (lines ~889–980).
  - `_check_version` runs `nate_OHA --help` and parses stdout to enforce a minimum version (lines ~1004–1092).
  - **Critically, `start_turn` is a `NotImplementedError` placeholder** (lines ~741–749) and there is **no ACP stream consumption**; the adapter supervises the process but never talks to the ACP server side.

- **Adapter wiring** (`src/nate_ntm/runtime/adapters.py`):
  - `RuntimeAdapters` bundles `agent_mail: BaseAgentMailClient` and `acp: BaseAcpClient`.
  - `create_runtime_adapters` chooses concrete implementations based on `RuntimeConfig.adapter_mode`, `agent_mail_adapter`, and `acp_adapter`:
    - For ACP:
      - `AdapterKind.FAKE` → `FakeAcpClient(config)`.
      - `AdapterKind.REAL` → `NateOhaAcpClient(config)` (lines ~85–91).
    - `OpenHandsAcpClient` is not selected here; it is effectively legacy.

- **Daemon wiring** (`src/nate_ntm/runtime/daemon.py`):
  - `RuntimeDaemon.create` and `resume` accept a `RuntimeAdapters` bundle.
  - They wire `acp_client.on_event = agent_supervisor.append_agent_event` so that ACP events land in `AgentEventStream` and flow to the control API via `runtime.runner` (see lines ~331–336 and ~407–411 in `daemon.py` plus `runtime/runner.py` lines ~132–159).
  - **No part of the scheduler currently calls `acp_client.start_agent` or `start_turn`.** `RuntimeScheduler.start()` only calls `agent_supervisor.launch_all_agents()`, which simulates subprocesses with placeholder `object()` handles and does not intersect with the ACP adapter at all (`src/nate_ntm/runtime/scheduler.py` lines ~78–90, and `agents.py` lines ~288–329).

### 1.2 Fit with CONOP goal #1

CONOP goal #1 says:

> Implement a new `NateOhaAcpClient` whose responsibility is to:
>
> - launch `nate-oha acp`
> - manage the subprocess lifecycle
> - connect to the ACP stream exposed by the process
> - consume ACP events
> - expose those events to the rest of the runtime
> - persist and reuse conversation IDs during resume

Current state vs desired state:

- **Launch semantics**:
  - Current: `NateOhaAcpClient` launches `nate_OHA` directly with `--enable-agent-mail` and environment variables; no `acp` subcommand; no `--config`; no `--resume` or `--set` (see `_build_command` and `_build_env`).
  - Desired: `nate-oha acp --config CONFIG_PATH [--resume CONVERSATION_ID] [--set path=value]…` using the JSON config model in `nate-oha-profiles/profile1.json`.
  - Implication: `_build_command` and the surrounding launch contract need to be **almost completely rewritten** to target the new CLI and config interface.

- **Process lifecycle**:
  - Largely aligned: `start_agent`/`stop_agent` and the `NateOhaProcessRecord` capture most of what the CONOP wants for subprocess launch, readiness, shutdown, and status, albeit with a minimal readiness check (poll immediately after spawn).
  - But these semantics are still "best effort" and oriented around the older `nate_OHA` CLI (no ACP readiness check, just "did the process die instantly").

- **ACP stream + event consumption**:
  - Currently missing entirely. `NateOhaAcpClient` does **not**:
    - Connect to any ACP stream or HTTP/WebSocket endpoint exposed by `nate-oha`.
    - Translate ACP‑level events (turn completions, tool calls, errors) into `AgentEvent` instances.
    - Implement `start_turn` at all.
  - All current `AgentEvent` emissions from `NateOhaAcpClient` are **process‑lifecycle only** (`*_process_started`, `*_process_ready`, `*_process_exited`, `*_process_crashed`, `*_process_start_failed`, `*_process_stop_failed`).
  - This is the largest functional gap between the current code and the CONOP: **ACP protocol and event streaming have not been wired in yet.**

- **Conversation ID persistence and reuse**:
  - Already implemented in a way that lines up well with the CONOP’s intent:
    - Deterministic per‑agent IDs derived from `swarm_id`, `project_path`, and `agent_id` (see `ensure_conversation` and `_conversation_namespace`).
    - Reuses an existing `AgentMetadata.conversation_id` when present, and persists new IDs back to metadata.
    - Enforced on resume: `RuntimeDaemon.resume` calls `acp_client.ensure_conversation(agent_id)` and compares the result to `AgentMetadata.conversation_id`, raising `RuntimeStartupError` on mismatch (see `daemon.py` lines ~413–446 + the `conv_id` check around `runtime_resume_acp_conversation_mismatch`).
  - This design is compatible with the new CLI’s `--resume CONVERSATION_ID` flag: we already have a stable, persisted identifier per agent.

- **Integration with the rest of the runtime**:
  - `RuntimeDaemon` wires `acp_client.on_event` into `AgentSupervisor.append_agent_event`, and `runtime.runner` wires `AgentSupervisor.on_agent_event` into the FastAPI `/events` WebSocket streaming endpoint.
  - This pipeline is ready to carry ACP events **once** `NateOhaAcpClient` actually emits them.

### 1.3 Concerns and ambiguities

1. **True ACP protocol details for `nate-oha acp` are not yet reflected in the code.**
   - We currently do not know (from this repo) whether `nate-oha` exposes ACP over HTTP, WebSockets, stdio, or some custom transport, nor what the canonical event schema looks like.
   - Before implementing stream handling in `NateOhaAcpClient`, we will need an authoritative contract from the `nate-oha` side.

2. **`start_turn` semantics are undefined for `NateOhaAcpClient`.**
   - The current placeholder suggests we expect a `start_turn(agent_id, prompt)` hook, but the CONOP describes an ACP stream model. It may be that `nate-oha` expects turns to be initiated via ACP messages, not CLI flags.
   - We need to decide whether `BaseAcpClient.start_turn` remains the right abstraction or whether it should evolve (e.g., send a "work available" signal to an existing ACP session rather than create fresh runs).

3. **Legacy HTTP adapter (`OpenHandsAcpClient`) and its tests are now explicitly obsolete.**
   - CONOP explicitly states that compatibility with the old HTTP ACP design is not a goal.
   - Keeping `OpenHandsAcpClient` and its tests in the codebase risks confusion and split maintenance. They should be clearly marked as deprecated or removed once the new design is in place.

4. **Scheduler does not own ACP start/stop yet.**
   - `RuntimeScheduler.start()` currently only calls `agent_supervisor.launch_all_agents()`, which simulates subprocess handles with `object()` instances and never consults `acp_client`.
   - In the desired architecture, the scheduler should be the component that:
     - Ensures agents are registered in runtime state (`AgentSupervisor.ensure_agents_registered`).
     - **Instructs `NateOhaAcpClient` to start/stop agents** according to swarm state and scheduling policy.

### 1.4 Recommendations for goal #1

**1. Make `NateOhaAcpClient` the sole production ACP adapter.**

- Treat `NateOhaAcpClient` as the canonical implementation of `BaseAcpClient` for all non‑test use.
- Move `OpenHandsAcpClient` to a clearly marked legacy module or delete it once any remaining tests are adapted.

**2. Re‑anchor `_build_command` and `_build_env` around the new CLI and config model.**

- Introduce a notion of a **base Nate OHA config path**:
  - Either:
    - A field on `RuntimeConfig` (e.g. `nate_oha_config_path: Path | None`), or
    - A value stored in `SwarmMetadata.runtime_options["nate_oha_config_path"]`, initialised at `RuntimeDaemon.create` time.
  - Default to a project‑relative profile like `nate-oha-profiles/profile1.json` when unspecified (the existing `nate-oha-profiles/profile1.json` is a strong candidate template).
- Change `_build_command` to construct:

  ```python
  cmd = [self.executable, "acp", "--config", str(config_path)]
  
  # Optionally, pass a resume conversation ID when metadata has one
  if metadata.conversation_id:
      cmd += ["--resume", metadata.conversation_id]
  
  cmd += self._build_set_overrides(agent_id, metadata)
  ```

- `_build_set_overrides` should translate swarm/agent metadata into `--set path=value` entries for at least:

  - `runtime.mode` (`"agent"` for production; `"echo"` for fake‑mode – see goal #2).
  - `llm.model` and `llm.api_key` (see section 4 for detailed mapping).
  - `prompt.soul_content`.
  - `features.agent_mail.*` when Agent Mail is enabled.

- Restrict environment usage to **non‑behavioural correlation variables** (`NATE_NTM_PROJECT_PATH`, `NATE_NTM_SWARM_ID`, `NATE_NTM_AGENT_ID`, `NATE_NTM_AGENT_CONVERSATION_ID`) and keep feature toggles and secrets in the JSON config + `--set` overrides.

**3. Plan for ACP stream ownership inside `NateOhaAcpClient`.**

- Once the `nate-oha acp` transport contract is known, extend `NateOhaAcpClient` with:
  - Per‑agent ACP connection handles (e.g., websockets, HTTP long‑poll, or stdio pipes).
  - A background reader loop that:
    - Parses ACP events from `nate-oha`.
    - Maps them into `AgentEvent` instances.
    - Emits them via `self.on_event`, which already flows into `AgentSupervisor` and then the WebSocket control API (`runtime.runner` → `runtime_api`).
  - A way to initiate turns (either via `start_turn` or by posting appropriate messages onto the ACP stream).

- We may need to revisit the exact signature of `BaseAcpClient.start_turn` depending on `nate-oha`’s protocol; for now, keeping the placeholder and documenting the upcoming semantics is acceptable.

**4. Move process lifecycle initiation from `AgentSupervisor.launch_all_agents` into the scheduler + ACP adapter.**

- Refactor `RuntimeScheduler.start()` to:

  1. Call `agent_supervisor.ensure_agents_registered()` (idempotent).
  2. For each configured agent, call `acp_client.start_agent(agent_id, metadata)` if that agent should be running.
  3. Rely on ACP events (`nate_oha_process_started`, `nate_oha_process_ready`, etc.) to update runtime state and event streams via `AgentSupervisor`.

- Re‑purpose or remove `AgentSupervisor.launch_all_agents()` once real process management is handled by `NateOhaAcpClient`.

---

## 2. Use the same ACP implementation for the fake runtime

### 2.1 Current fake vs real split

- **Fake ACP**:
  - `FakeAcpClient` is selected whenever `AdapterKind.FAKE` is active for ACP (`adapters.py` line ~85).
  - It is widely used in:
    - Unit tests that assert on conversation IDs and `AcpAgentStatus` state without touching external binaries.
    - Quickstart/integration tests that exercise runtime/daemon behaviour without requiring `nate_OHA`.

- **Real ACP**:
  - `NateOhaAcpClient` is selected for `AdapterKind.REAL`.
  - E2E and integration tests use it with real or stubbed `subprocess.Popen` (`tests/unit/runtime/test_acp_client.py` and `tests/e2e/test_real_runtime_nate_oha_agent_mail.py`).

The CONOP explicitly states:

> Going forward, there should no longer be two fundamentally different ACP implementations. … The “fake” ACP adapter should simply launch `nate-oha` in `runtime.mode = "echo"`.

### 2.2 Implications

1. **`FakeAcpClient` can no longer be treated as a behavioural stand‑in for production.**
   - It does not exercise any of the following production concerns:
     - Subprocess behaviour (spawn/exit, signals, timeouts).
     - `nate-oha` version compatibility checks.
     - ACP connection management.
     - Agent Mail configuration propagation.
   - Keeping it as the primary dev‑mode path hides integration issues that will only show up under `NateOhaAcpClient`.

2. **Tests must move from “fake ACP” semantics to “real ACP semantics with controlled dependencies”.**
   - Instead of verifying behaviour of `FakeAcpClient`, tests should:
     - Stub `subprocess.Popen` and `NateOhaAcpClient._check_version` (as many already do in `tests/unit/runtime/test_acp_client.py`) to avoid requiring a real binary.
     - Assert against the `nate-oha acp` command line and JSON config overrides we expect to issue.
     - Optionally run gated tests that require a real `nate-oha` installation.

3. **AdapterKind semantics will need to evolve.**
   - Today:
     - `AdapterKind.FAKE` → `FakeAcpClient`.
     - `AdapterKind.REAL` → `NateOhaAcpClient`.
   - Tomorrow:
     - Both FAKE and REAL can use `NateOhaAcpClient`, but with **different config profiles**:
       - FAKE: `runtime.mode="echo"`, Agent Mail typically disabled, and heavy use of mocks/stubs for subprocess and network.
       - REAL: `runtime.mode="agent"`, full Agent Mail configuration, real subprocesses and ACP connections (optionally gated by env flags like `NATE_OHA_INTEGRATION` / `NATE_OHA_E2E`).

### 2.3 Recommendations for goal #2

**1. Deprecate and eventually remove `FakeAcpClient`.**

- In the short term, you can keep `FakeAcpClient` around as a narrow testing helper, but:
  - Stop wiring it through `create_runtime_adapters` for production or CLI flows.
  - Migrate runtime tests to target `NateOhaAcpClient` with stubbed subprocess behaviour (as unit tests already do).

**2. Reinterpret `AdapterKind` for ACP as "profile" rather than "implementation type".**

- Adjust `create_runtime_adapters` so that:

  ```python
  if acp_kind is AdapterKind.FAKE:
      acp = NateOhaAcpClient(config=config, profile="echo")  # or similar flag
  elif acp_kind is AdapterKind.REAL:
      acp = NateOhaAcpClient(config=config, profile="agent")
  ```

- The `profile` (name TBD) would influence:
  - The `--set runtime.mode=...` override.
  - Possibly additional `--set` toggles (e.g., turning off certain tools or side effects in echo mode).

**3. Make `runtime.mode="echo"` the canonical fake runtime behaviour.**

- Use the new JSON config + `--set` path to configure echo mode for dev/test swarms.
- Ensure the behaviour of `echo` is documented on the `nate-oha` side:
  - What events does it emit?
  - How are prompts and responses surfaced in the ACP stream?
  - How does it treat tools and Agent Mail?

**4. Tighten unit tests around `NateOhaAcpClient` instead of `FakeAcpClient`.**

- Many tests already stub `subprocess.Popen` and assert on:
  - Emitted events (`nate_oha_process_*`).
  - Status transitions.
  - Command and env construction (see `tests/unit/runtime/test_acp_client.py::test_nate_oha_acp_client_builds_command_and_env_for_agent_mail`).
- Update these tests to:
  - Assert on the **new command line** (`['nate-oha', 'acp', '--config', ..., '--set', ...]`) rather than `['nate_OHA', '--enable-agent-mail']`.
  - Validate `--set runtime.mode` and Agent Mail JSON config fields instead of env vars.
- Replace tests of `FakeAcpClient` that exercise behaviour no longer needed (e.g., `fake-turn:*` IDs) with tests that focus on behaviour we still care about (e.g., deterministic conversation IDs, adapter‑level status mapping) via `NateOhaAcpClient`.

---

## 3. Make Agent Mail the real integration, but keep it optional

Goals #3 and #4 interact, so I’ll cover them together.

### 3.1 Current Agent Mail layering

- **Abstraction**: `BaseAgentMailClient` (`src/nate_ntm/runtime/agent_mail_client.py`, lines ~91–161) defines:
  - `ensure_project()` → swarm‑level project identifier.
  - `ensure_agent_identity()` / `ensure_agent_identity_with_credentials()` → per‑agent identity + token.
  - `get_unread_mail_flags(agent_ids)` → map of `agent_id` → `has_unread_mail`.

- **FakeAgentMailClient**:
  - In‑memory, deterministic IDs (`"fake-mail-project:..."`, `"fake-mail-identity:{agent_id}"`).
  - Unread counts / flags stored in process memory.
  - Selected when `AdapterKind.FAKE` is used for Agent Mail in `create_runtime_adapters` (lines ~76–82 in `adapters.py`).

- **McpAgentMailClient**:
  - Talks to a real `mcp_agent_mail` instance via JSON‑RPC over HTTP (`_post_jsonrpc` and `_call_tool`).
  - Uses `ensure_project` and `register_agent` tools to allocate project and per‑agent identities and tokens (lines ~292–390).
  - Uses `fetch_inbox` to implement `get_unread_mail_flags` (lines ~392–444).
  - Endpoint and auth resolved from env (`NATE_NTM_AGENT_MAIL_URL`, `AGENT_MAIL_URL`, `NATE_NTM_AGENT_MAIL_TOKEN`, etc.) in `__post_init__` (lines ~269–287).

- **Daemon integration**:
  - `RuntimeDaemon.create` (lines ~261–296):
    - Calls `agent_mail_client.ensure_project()` and persists the returned `agent_mail_project_id` in `SwarmMetadata`.
    - For each initial agent (if `agent_count` is set), calls `ensure_agent_identity_with_credentials` and `acp_client.ensure_conversation` to populate `AgentMetadata.agent_mail_identity`, `agent_mail_credentials_ref`, and `conversation_id`.
  - `RuntimeDaemon.resume` (lines ~391 onwards):
    - Reconstructs adapters, then **revalidates** Agent Mail and ACP invariants:
      - For FakeAgentMailClient, ensures the derived project ID matches `swarm.agent_mail_project_id` when it uses the `fake-mail-project:` scheme.
      - For `McpAgentMailClient`, always enforces strict project ID equality (see the `runtime_resume_agent_mail_project_mismatch_real` block printed by `sed` earlier).
      - For each agent with a configured `agent_mail_identity`, calls `ensure_agent_identity_with_credentials` and checks identity equality.
      - For each agent with a `conversation_id`, calls `acp_client.ensure_conversation` and checks equality.

- **Scheduler integration**:
  - `RuntimeScheduler.start()` optionally polls unread flags via `agent_mail_client.get_unread_mail_flags(agent_ids)` and enqueues `MailReceived` events via `AgentSupervisor.record_unread_mail` for agents with unread mail (lines ~101–127).

- **NateOhaAcpClient integration**:
  - `_build_env` uses `RuntimeConfig.agent_mail_project` and `agent_mail_upstream_url`, plus `AgentMetadata.agent_mail_identity` and `agent_mail_credentials_ref`, to populate `AGENT_MAIL_*` environment variables when Agent Mail is enabled, and raises `AcpClientError` if configuration is incomplete (lines ~943–979).
  - This matches the older `NATE_OHA_GUIDE.md` env‑based contract, not the new JSON config model.

### 3.2 CONOP expectations vs current design

CONOP goal #3:

> Remove all runtime support for the fake Agent Mail implementation. The runtime should interact exclusively with a real `mcp_agent_mail` instance.

CONOP goal #4:

> Agent Mail must not become a requirement for running a swarm. … When Agent Mail is disabled, the runtime should simply omit the Agent Mail configuration passed to `nate-oha`, and no Agent Mail APIs should be contacted.

Today’s runtime behaves as if:

- Agent Mail is always **logically present**, but may be backed by:
  - A fake in‑memory adapter (`FakeAgentMailClient`), or
  - A real HTTP adapter (`McpAgentMailClient`).
- Crucially, **`RuntimeDaemon.create` always calls `ensure_project`**, even when you don’t care about mail; that is fine for the fake adapter but incompatible with "Agent Mail optional" once only the real adapter remains.

### 3.3 Recommendations for goals #3 and #4

**1. Remove FakeAgentMailClient from production adapter selection.**

- In `create_runtime_adapters` (`adapters.py`):
  - Replace the `AdapterKind.FAKE → FakeAgentMailClient` branch with either:
    - `Agent Mail disabled` (represented by `agent_mail_client=None`), or
    - `McpAgentMailClient` with a dedicated config profile for tests.
- For pure in‑process unit tests, you can still instantiate `FakeAgentMailClient` directly where needed, but it should not be reachable via normal CLI or runtime configuration.

**2. Introduce an explicit "Agent Mail enabled" flag in configuration/metadata.**

- Today, Agent Mail enablement is inferred implicitly:
  - `SwarmMetadata.agent_mail_project_id` non‑empty, plus
  - Per‑agent `AgentMetadata.agent_mail_identity` fields.
- For clarity and to satisfy the "optional" requirement, add a runtime‑level flag such as:
  - `RuntimeConfig.agent_mail_enabled: bool` or
  - `SwarmMetadata.runtime_options["agent_mail_enabled"]: bool`.
- When this flag is **false**:
  - Do not create or call any `BaseAgentMailClient` from the daemon.
  - Do not create per‑agent mail identities or credentials during `RuntimeDaemon.create`.
  - Do not call any Agent Mail APIs on resume.
  - `NateOhaAcpClient` must be instructed **not** to set `features.agent_mail.enabled` or any Agent Mail fields in its config overrides.

**3. Allow `RuntimeDaemon.create` / `resume` to run without Agent Mail.**

- Update `RuntimeDaemon.create` so that:

  ```python
  if agent_mail_client is None or not agent_mail_enabled:
      agent_mail_project_id = ""  # or None if we update the dataclass
      # Create agents without mail identities/credentials.
  else:
      agent_mail_project_id = agent_mail_client.ensure_project()
      # Allocate identities + credentials.
  ```

- Likewise in `RuntimeDaemon.resume`:
  - Skip all `ensure_project` / `ensure_agent_identity_with_credentials` checks when no Agent Mail client is configured or when the swarm was created with Agent Mail disabled.

- This will require relaxing invariants in `SwarmMetadata` and some tests to allow `agent_mail_project_id` to be empty or optional when Agent Mail is off.

**4. Move Agent Mail configuration for `nate-oha` from env vars into JSON config overrides.**

- Instead of building `AGENT_MAIL_*` env vars, have `_build_set_overrides` in `NateOhaAcpClient` compute:

  ```text
  features.agent_mail.enabled
  features.agent_mail.project
  features.agent_mail.agent_identity
  features.agent_mail.credentials_ref
  features.agent_mail.upstream_url
  ```

- Use the same sources you already rely on:
  - `features.agent_mail.project` ← `RuntimeConfig.agent_mail_project` or `SwarmMetadata.agent_mail_project_id`.
  - `features.agent_mail.agent_identity` ← `AgentMetadata.agent_mail_identity`.
  - `features.agent_mail.credentials_ref` ← `AgentMetadata.agent_mail_credentials_ref`.
  - `features.agent_mail.upstream_url` ← `RuntimeConfig.agent_mail_upstream_url`.
- Only set these when Agent Mail is enabled; otherwise, leave the feature disabled in the config file.

**5. Keep Agent Mail interaction strictly optional at runtime level.**

- For swarms that do not use Agent Mail:
  - `RuntimeDaemon` should never construct an Agent Mail adapter.
  - `RuntimeScheduler` should skip unread‑mail polling entirely.
  - `get_swarm_overview` should default `has_unread_mail=False` for all agents (already supported via the `else` branch in `get_swarm_overview`, lines ~596–603).

**6. Tests that rely on Agent Mail should be explicitly gated.**

- Follow the pattern already used in:
  - `tests/e2e/test_real_runtime_nate_oha_agent_mail.py` (gated by `NATE_OHA_E2E`).
  - `tests/integration/quickstart/test_nate_oha_agent_mail_integration_t242.py` (gated by `NATE_OHA_INTEGRATION`).
- For tests that require a real `mcp_agent_mail` instance:
  - Clearly document the expectations at the top of the test modules.
  - Fail fast with a skip reason when the environment is not configured.

---

## 4. Use the new Nate OHA configuration interface

### 4.1 Current configuration sources for Nate OHA

- `RuntimeConfig` (`config/runtime_config.py`):
  - Knows nothing about Nate OHA config files; it only manages:
    - Project path & metadata dir.
    - Control API host/port.
    - Swarm ID.
    - Adapter selection (`adapter_mode`, `agent_mail_adapter`, `acp_adapter`).
    - Agent Mail project and upstream URL (env‑resolved).

- `AgentMetadata` (`runtime/metadata_store.py`):
  - Contains fields that are logically relevant to `nate-oha` config:
    - `role: str | None`.
    - `launch_config: Mapping[str, Any]`.
    - `model: str | None`.
    - `task_description: str | None`.
    - `restart_policy: Mapping[str, Any]`.
  - These are currently unused by `NateOhaAcpClient`.

- `NateOhaAcpClient`:
  - Uses **environment variables** to drive Nate OHA behaviour (`LLM_MODEL`, `AGENT_MAIL_*`, `NATE_NTM_*`).
  - Does not use the JSON config file or `--set` semantics.

- `nate-oha-profiles/profile1.json`:
  - Defines a rich, typed config structure for `nate_oha`:
    - `runtime.mode`, `llm.*`, `openhands.*`, `prompt.*`, `features.agent_mail.*`, `features.python_uv_project`, `features.allow_proc_management`, and `mcp_config`.
  - Currently **unused** by the runtime.

### 4.2 Target mapping to `--set` paths

CONOP lists the primary configuration paths to manage:

```text
runtime.mode

llm.model
llm.api_key

prompt.soul_content

features.agent_mail.enabled
features.agent_mail.project
features.agent_mail.agent_identity
features.agent_mail.credentials_ref
features.agent_mail.upstream_url
```

A reasonable mapping from `nate_ntm` state to these paths is:

- `runtime.mode`:
  - `"agent"` for production/REAL ACP.
  - `"echo"` for the fake/dev runtime (goal #2).

- `llm.model`:
  - Primary source could be:
    - `AgentMetadata.model` when set; otherwise
    - A runtime‑level default from the base config file (e.g. `openai/gpt-4o` in `profile1.json`).
  - You may still allow overriding via environment for convenience (e.g. `NATE_OHA_LLM_MODEL` or reuse `LLM_MODEL`), but the canonical representation should be in JSON + `--set`.

- `llm.api_key`:
  - Should be sourced from environment variables or a secret manager and **never** persisted into `AgentMetadata` or `SwarmMetadata`.
  - For example: read `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`/`NATE_OHA_API_KEY` from `os.environ` in `_build_set_overrides` and pass them directly to `nate-oha` via `--set llm.api_key=...`.

- `prompt.soul_content`:
  - Can be constructed from:
    - `AgentMetadata.role`.
    - `AgentMetadata.task_description`.
    - Optional swarm‑wide defaults (e.g. from `SwarmMetadata.runtime_options` or an external file).
  - A simple first cut: generate a canonical soul text along the lines of:

    > "You are {display_name}. Role: {role}. Task: {task_description}."

  - This should be generated at launch time and not persisted as config; if we want per‑agent prompt customisation, we can store it in `AgentMetadata.launch_config` or another dedicated field.

- `features.agent_mail.*`:
  - As noted earlier:
    - `enabled` ← whether Agent Mail is enabled for this agent/swarm.
    - `project` ← `RuntimeConfig.agent_mail_project` or `SwarmMetadata.agent_mail_project_id`.
    - `agent_identity` ← `AgentMetadata.agent_mail_identity`.
    - `credentials_ref` ← `AgentMetadata.agent_mail_credentials_ref`.
    - `upstream_url` ← `RuntimeConfig.agent_mail_upstream_url`.

### 4.3 Where to store the base config path

Two plausible options:

1. **RuntimeConfig‑centric**:
   - Add a field `nate_oha_config_path: Path | None` to `RuntimeConfig`.
   - Resolve it from:
     - An explicit CLI option (e.g. `--nate-oha-config` on `nate-ntm runtime start`).
     - `NATE_OHA_CONFIG_PATH` env var.
     - A project‑relative default such as `project_root/nate-oha-config.json` or `project_root/nate-oha-profiles/profile1.json`.
   - Store the resolved path directly in `RuntimeConfig` and use it in `_build_command`.

2. **SwarmMetadata‑centric**:
   - Keep `RuntimeConfig` environment‑focused and store the config path in `SwarmMetadata.runtime_options["nate_oha_config_path"]` during `RuntimeDaemon.create`.
   - On resume, read the path from `swarm.runtime_options` to ensure stability across daemon restarts.

Given the CONOP’s emphasis on the runtime being responsible for swarm metadata and `nate-oha` being responsible for runtime behaviour, **option 2** (SwarmMetadata‑centric) aligns slightly better: the config path becomes part of the persisted swarm description.

### 4.4 Concrete steps

1. **Define a small helper for building `--set` arguments.**

   ```python
   def _set_arg(path: str, value: str | int | bool | None) -> list[str]:
       if value is None:
           return []
       # Basic JSON encoding for safety.
       encoded = json.dumps(value)
       return ["--set", f"{path}={encoded}"]
   ```

2. **Implement `_build_set_overrides(agent_id, metadata)` in `NateOhaAcpClient`.**

   - Assemble a list of arguments by calling `_set_arg` for the paths above.
   - Keep the mapping logic entirely within `NateOhaAcpClient` so higher layers only talk in terms of metadata and runtime config.

3. **Stop setting behaviour‑changing environment variables.**

   - Retain only correlation vars (`NATE_NTM_*`) in `_build_env`.
   - Remove `LLM_MODEL` and `AGENT_MAIL_*` from `_build_env` once the JSON config path is live, to avoid conflicting knobs.

4. **Update tests to assert on `--set` rather than env.**

   - For example, `test_nate_oha_acp_client_builds_command_and_env_for_agent_mail` should become something like:
     - Assert `cmd` equals `['nate-oha', 'acp', '--config', <path>, '--set', 'features.agent_mail.enabled=true', ...]`.
     - Keep a minimal env assertion (`NATE_NTM_PROJECT_PATH`, etc.).

---

## 5. Runtime vs Nate OHA responsibilities

CONOP’s “Desired Architecture” section draws a clear boundary:

- Runtime (`nate_ntm`) is responsible for:
  - Swarm metadata.
  - Process supervision.
  - Scheduling.
  - Resume semantics.
  - ACP stream management.
  - Agent Mail coordination (when enabled).

- `nate-oha` is responsible for:
  - LLM execution.
  - Prompt construction.
  - ACP server behaviour.
  - OpenHands integration.
  - Agent Mail feature implementation.

The current code already aligns reasonably well with this split, with two notable caveats:

1. **The runtime still tries to encode parts of the Nate OHA configuration model in env vars.**
   - Moving to JSON config + `--set` will push the detailed prompt and model semantics fully into `nate-oha`.

2. **Agent Mail appears in both runtime and Nate OHA layers.**
   - Runtime uses Agent Mail for project and identity management and unread‑mail flags.
   - Nate OHA uses Agent Mail as an MCP server for in‑agent tools.
   - This is acceptable as long as *configuration* flows one way (runtime → `nate-oha`), and the runtime does not attempt to duplicate the agent‑level integration logic.

The epic’s guidance to prioritize architectural clarity over legacy compatibility is consistent with:

- Simplifying or removing dev‑mode artefacts (`FakeAcpClient`, `FakeAgentMailClient`).
- Dropping the legacy HTTP ACP path.
- Shifting configuration entirely to `nate-oha`’s JSON model.

---

## 6. Migration strategy and milestones

Given the breadth of change, an incremental plan is important. Here is a suggested sequence that keeps the system runnable while progressively moving toward the CONOP architecture.

### Milestone 1 – Switch to `nate-oha` CLI + config file (no ACP stream yet)

**Scope:** `NateOhaAcpClient` CLI/launch contract only.

- Introduce a base config path (as discussed in §4.3) and change `_build_command` to:
  - Use `self.executable = "nate-oha"`.
  - Call `nate-oha acp --config <path> [--resume <conversation_id>] [--set ...]`.
- Implement JSON‑based `--set` overrides for:
  - `runtime.mode` (always `"agent"` for now).
  - `llm.model`, `llm.api_key`.
  - `prompt.soul_content`.
  - `features.agent_mail.*`.
- Keep `_build_env` but strip out behaviour‑defining env vars in favour of JSON config.
- Update unit tests for `NateOhaAcpClient` to assert on the new command shape and `--set` arguments.
- Leave `FakeAcpClient` and `FakeAgentMailClient` in place temporarily, but start marking them as deprecated in docstrings and tests.

### Milestone 2 – Unify fake and real ACP via `NateOhaAcpClient`

**Scope:** Adapter selection and tests.

- Change `create_runtime_adapters` to always use `NateOhaAcpClient` for ACP (with a profile/flag for `runtime.mode`):
  - `AdapterKind.FAKE` → `runtime.mode="echo"`.
  - `AdapterKind.REAL` → `runtime.mode="agent"`.
- Remove `FakeAcpClient` from normal runtime flows; keep it only as a test helper if still needed.
- Update tests that currently assert on `FakeAcpClient` behaviour to target `NateOhaAcpClient` instead, using stubbed `subprocess.Popen`.

### Milestone 3 – Make Agent Mail optional and remove fake Agent Mail

**Scope:** Agent Mail adapters and daemon.

- Introduce an explicit Agent Mail enablement flag and propagate it through `RuntimeConfig` / `SwarmMetadata`.
- Update `RuntimeDaemon.create` and `resume` to allow running with `agent_mail_client=None`:
  - Skip `ensure_project` and identity provisioning when disabled.
  - Allow `SwarmMetadata.agent_mail_project_id` to be empty.
- Remove `FakeAgentMailClient` from `create_runtime_adapters`; only `McpAgentMailClient` remains as a selectable adapter.
- Move Agent Mail settings for `nate-oha` into JSON config overrides and stop using `AGENT_MAIL_*` env vars.
- Update tests to:
  - Use real `McpAgentMailClient` only in gated scenarios with a live server.
  - Use stubbed HTTP responses for non‑gated unit tests.

### Milestone 4 – Implement ACP stream management and `start_turn`

**Scope:** `NateOhaAcpClient` internals and scheduler.

- Based on the authoritative `nate-oha acp` protocol:
  - Add per‑agent ACP connections to `NateOhaAcpClient`.
  - Implement `start_turn` in terms of ACP messages or HTTP calls.
  - Implement a background reader that parses ACP events and emits `AgentEvent`s via `on_event`.
- Extend `RuntimeScheduler` to:
  - Use Agent Mail (when enabled) and other signals to decide when to request turns (`acp_client.start_turn`).
  - React to error events from `nate-oha` to invoke `mark_agent_failed` or restart policies.

### Milestone 5 – Cleanup, docs, and test suite alignment

- Remove or quarantine obsolete components:
  - `OpenHandsAcpClient` and related specs/tests.
  - `FakeAcpClient` and `FakeAgentMailClient` once no longer needed.
- Update specs:
  - `specs/002-nate-oha-acp-adapter/*` to reflect the new `nate-oha acp` CLI and JSON config contract.
  - `NATE_OHA_GUIDE.md` to either:
    - Defer all runtime‑specific config details to `CONOP_NATEOHAv2` + new specs, or
    - Be clearly marked as covering *only* manual/nongraph runtime scenarios.
- Adjust `README.md` and `AGENTS_MK2.md` references to point at the new architecture.
- Prune tests that exist solely to validate behaviour the CONOP now treats as obsolete.

---

## 7. Open questions and required clarifications

1. **Exact `nate-oha acp` ACP protocol and event schema.**
   - We need a concrete definition of how the ACP stream is exposed and which events it emits so that `NateOhaAcpClient` can:
     - Establish connections.
     - Implement `start_turn` correctly.
     - Map ACP events onto `AgentEvent` in a stable way.

2. **Source of truth for the base Nate OHA config file.**
   - Should this be:
     - A repo‑local default like `nate-oha-profiles/profile1.json`?
     - A project‑specific file that operators create under their project root?
     - A value provided via CLI/ENV and persisted into `SwarmMetadata.runtime_options`?

3. **How strong should the runtime’s contract be around LLM credentials?**
   - Is `llm.api_key` expected to be single‑swarm/global, or can it vary per agent?
   - How should this interact with secret management (env vars vs external secret stores)?

4. **Long‑term role of unread‑mail polling in the scheduler.**
   - With `nate-oha` owning "Agent Mail feature implementation", should new‑mail notifications eventually be driven by ACP events rather than the runtime polling Agent Mail directly via `McpAgentMailClient`?
   - If so, the scheduler’s `get_unread_mail_flags` usage may become a transitional mechanism.

5. **AdapterKind future: retire, repurpose, or extend?**
   - Once both FAKE and REAL ACP use `NateOhaAcpClient`, and fake Agent Mail is gone, do we still need `AdapterKind` at all, or should we replace it with more precise flags (`agent_mail_enabled`, `nate_oha_profile`, etc.)?

Clarifying these points up front will make the implementation of this epic much smoother and will minimize churn in the runtime’s public API.
