# Quickstart: nate_OHA ACP Production Adapter (NateOhaAcpClient)

This quickstart shows how to validate the `NateOhaAcpClient` integration end-to-end using the existing `nate-ntm` CLI and the `nate_OHA` ACP runtime with Agent Mail.

It assumes the feature has been implemented according to `spec.md`, `data-model.md`, and `contracts/nate_oha_process_launch.md`.

## 1. Prerequisites

1. **Tools installed** on the host:
   - Python (compatible with this repo; see `pyproject.toml`).
   - `uv` (recommended for running commands in this repo).
   - `nate_ntm` installed in editable mode (from this repo).
   - `nate_OHA` installed and on `PATH` (from the companion repo).

2. **Agent Mail**:
   - A running `mcp_agent_mail` server.
   - A project identifier, agent identity, and token.

3. **Environment variables** (per `NATE_OHA_GUIDE.md`):

   ```bash
   export AGENT_MAIL_PROJECT="your-project-id"
   export AGENT_MAIL_AGENT="your-public-agent-name"
   export AGENT_MAIL_TOKEN="your-secret-token"
   export AGENT_MAIL_UPSTREAM_URL="https://your-mcp-agent-mail.example.com/mcp"
   ```

4. **Project directory** for the swarm (example):

   ```bash
   export PROJECT_ROOT="/path/to/swarm-project"
   mkdir -p "$PROJECT_ROOT"
   ```

## 2. Sanity-check nate_OHA with Agent Mail

Before involving `nate_ntm`, verify that `nate_OHA` can start with Agent Mail enabled as described in `NATE_OHA_GUIDE.md`:

```bash
cd /path/to/nate_OHA/repo   # if running from source

uv run nate_OHA --enable-agent-mail
```

You should see the ACP server start without configuration errors. Exit the process before proceeding.

## 3. Create a swarm using NateOhaAcpClient

Use the `nate-ntm` CLI to create a new swarm that uses **real** adapters for both Agent Mail and ACP. In this configuration:

- `AdapterKind.REAL` for **ACP** is implemented by `NateOhaAcpClient`, the nate_OHA production ACP adapter.
- `AdapterKind.REAL` for **Agent Mail** is implemented by `McpAgentMailClient`, configured via `RuntimeConfig.agent_mail_project` and `RuntimeConfig.agent_mail_upstream_url` (resolved from `NATE_NTM_AGENT_MAIL_PROJECT` / `NATE_NTM_AGENT_MAIL_URL` with fallbacks to `AGENT_MAIL_PROJECT` / `AGENT_MAIL_UPSTREAM_URL` / `AGENT_MAIL_URL`).

```bash
cd /path/to/nate_ntm/repo

uv run nate-ntm runtime start \
  --project "$PROJECT_ROOT" \
  --mode create \
  --agents 1 \
  --adapter-mode real \
  --with-control-api
```

Expected behavior:

- Swarm metadata is created under `$PROJECT_ROOT/.nate_ntm/`.
- The runtime daemon starts and runs until shut down via the control API.
- A nate_OHA process is launched for the single agent *only after* the runtime begins supervising agents (via `start_agent`), with Agent Mail enabled and configured from the `RuntimeConfig` fields described above.

## 4. Inspect runtime and agent state

In a separate terminal, use the JSON-RPC CLI to inspect runtime and agent state.

1. **Get runtime status**:

   ```bash
   cd /path/to/nate_ntm/repo
   uv run nate-ntm api call runtime.get_status
   ```

   You should see a `RUNNING` status and at least one agent listed.

2. **Get swarm overview**:

   ```bash
   uv run nate-ntm api call swarm.get_overview
   ```

   Confirm that the swarm reports one agent and that the ACP adapter is `NateOhaAcpClient` (or equivalent identifier in the overview).

3. **Inspect a specific agent** (replace `agent-1` with the actual ID from the overview):

   ```bash
   uv run nate-ntm api call agent.get_detail --param agent_id="agent-1" --param max_events=20
   ```

   Expected:

   - Agent status reflects a healthy nate_OHA subprocess.
   - Recent events include nate_OHA process start and readiness, plus any ACP events produced so far.

4. **Check on-disk metadata** (optional, for deeper verification):

   ```bash
   cd "$PROJECT_ROOT/.nate_ntm"
   cat swarm.json | jq '.agents[] | select(.agent_id=="agent-1")'
   ```

   Confirm:

   - `agent_mail_identity` matches `AGENT_MAIL_AGENT`.
   - `conversation_id` is non-empty once the first OpenHands conversation has been established.

## 5. Validate shutdown and resume behavior

1. **Request a graceful shutdown via the control API**:

   ```bash
   uv run nate-ntm api call runtime.shutdown --param timeout_seconds=30
   ```

   Expected:

   - The runtime acknowledges the shutdown request.
   - The daemon process exits after shutting down agents and nate_OHA subprocesses.

2. **Resume the swarm using the same project directory**:

   ```bash
   uv run nate-ntm runtime start \
     --project "$PROJECT_ROOT" \
     --mode resume \
     --adapter-mode real \
     --with-control-api
   ```

   Expected:

   - The runtime loads existing metadata from `$PROJECT_ROOT/.nate_ntm/`.
   - A nate_OHA process is relaunched for the agent.
   - The adapter reuses the same Agent Mail identity and the same persisted OpenHands conversation identifier.
   - If the adapter would derive a different conversation ID than the one
     stored for the agent in `SwarmState`/`swarm.json`, resume fails fast with
     `RuntimeStartupError` and logs a mismatch to protect conversation
     continuity.


3. **Re-inspect agent detail and metadata**:

   ```bash
   uv run nate-ntm api call agent.get_detail --param agent_id="agent-1" --param max_events=20

   cd "$PROJECT_ROOT/.nate_ntm"
   cat swarm.json | jq '.agents[] | select(.agent_id=="agent-1") | .conversation_id'
   ```

   Validation criteria (SC-002):

   - `agent_mail_identity` is unchanged across shutdown/resume.
   - `conversation_id` before and after resume are identical.
   - No duplicate or orphaned conversations are created.

## 6. Observability via event subscriptions (optional)

To further validate event propagation from nate_OHA into the runtime event stream:

1. **Subscribe to events for all agents** (assuming the control API is running):

   ```bash
   uv run nate-ntm api call events.subscribe
   ```

   Note the returned `subscription_id`.

2. **Trigger work for the nate_OHA-backed agent** (via whatever mechanism the runtime exposes for scheduling turns).

3. **Inspect event notifications** (once `events.notify` or equivalent wiring is implemented) and confirm that:
   - Turn completions and errors are surfaced as `AgentEvent` records.
   - Process lifecycle events (start, readiness, failures) are visible through the same APIs.


## 7. Developer quick-check (offline-safe)

If you do not have access to a real nate_OHA + Agent Mail environment yet,
you can still validate the core NateOhaAcpClient behavior and its integration
with the runtime using the existing unit and integration tests. From the repo
root:

```bash
PYTHONPATH=src pytest tests/unit/runtime/test_acp_client.py -q
PYTHONPATH=src pytest tests/unit/runtime/test_daemon.py -q
PYTHONPATH=src pytest tests/integration/quickstart/test_resume_swarm_us2.py -q
```

These tests cover:

- The NateOhaAcpClient adapter contract and conversation ID behavior
  (including metadata-aware `ensure_conversation` semantics).
- Metadata persistence and reuse of Agent Mail identities and conversation
  IDs across shutdown/resume.
- Event routing from adapters into the runtime (agent event stream, runtime
  overview, and status APIs).
- REAL-ACP resume behavior for nate_OHA-backed swarms, including enforcement
  of conversation continuity and surfacing mismatches as `RuntimeStartupError`,
  without requiring a real nate_OHA process or live Agent Mail server.

This quickstart, together with `spec.md`, `data-model.md`, and `contracts/nate_oha_process_launch.md`, provides an end-to-end validation path for the NateOhaAcpClient integration.
