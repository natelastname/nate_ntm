# Quickstart: nate_ntm Swarm Runtime Orchestrator (MVP)

This quickstart describes how to run and validate the nate_ntm Swarm Runtime Orchestrator MVP end-to-end on a local machine.

It is **not** an implementation guide; it assumes the runtime and its CLI/API are implemented according to the spec, plan, and contracts.

## 1. Prerequisites

- Python 3.11 installed and available on `PATH`.
- The `nate_ntm` repository cloned locally.
- Dependencies installed (example):

  ```bash
  cd /path/to/nate_ntm
  pip install -e .
  ```

- Access to required external services (can be real or mocked for testing):
  - OpenHands agent server (for ACP/conversation handling).
  - Agent Mail service (for mailbox coordination).
- Any necessary credentials configured via environment variables or config files (outside the runtime codebase).

## 2. Start a New Swarm

### 2.1 Start the Runtime Daemon

From the project root:

```bash
nate-ntm runtime start --project /abs/path/to/your/project
```

Expected behavior:

- A single Runtime daemon process starts.
- It creates or loads swarm metadata under `.nate_ntm/` in the project directory.
- It initializes or reuses the corresponding coordination project in Agent Mail.
- It launches the configured agents and begins polling Agent Mail.

### 2.2 Check Runtime Status via API

Use a CLI helper or JSON-RPC client to query `runtime.get_status` (see `contracts/runtime-api.md`):

```bash
nate-ntm api call runtime.get_status
```

Expected outcome (high level):

- Runtime status is `Running`.
- Swarm ID and project path are correct.
- Agent counts reflect the configured agents (e.g., some `Starting` then `Idle`/`Running`).

## 3. Resume a Previous Swarm

### 3.1 Shut Down Gracefully

From another terminal:

```bash
nate-ntm api call runtime.shutdown --param timeout_seconds=30
```

Expected outcome:

- Runtime transitions to `ShuttingDown` and then exits.
- Swarm metadata (including Agent Mail identities and conversation IDs) remains stored under `.nate_ntm/`.

### 3.2 Restart in Resume Mode

Start the Runtime again:

```bash
nate-ntm runtime start --project /abs/path/to/your/project --mode resume
```

Then query status:

```bash
nate-ntm api call runtime.get_status
```

Expected outcome:

- Runtime loads existing metadata from `.nate_ntm/`.
- Agents are relaunched with the same Agent Mail identities and conversation IDs.
- Any unread Agent Mail present at shutdown is still available and eligible for scheduling.

## 4. Inspect a Single Agent

### 4.1 Get Swarm Overview

```bash
nate-ntm api call swarm.get_overview
```

Expected outcome:

- You see a list of agents with IDs, display names, and statuses.

### 4.2 Inspect Agent Detail

Pick an `agent_id` from the overview and run:

```bash
nate-ntm api call agent.get_detail --param agent_id=<agent_id> --param max_events=50
```

Expected outcome:

- You receive metadata for the agent (status, Agent Mail identity, conversation ID).
- You see a recent sequence of events from the Agent Event Stream (turns, tool calls, errors, etc.).

### 4.3 Live Event Streaming (Optional)

To attach a live inspection view:

```bash
nate-ntm api call events.subscribe --param agent_ids='["<agent_id>"]' --param include_runtime=true
```

Then watch for `events.notify` messages via the appropriate client.

Expected outcome:

- New events for the agent and runtime are delivered with end-to-end latency under ~1 second for the vast majority of events under normal load.

## 5. Validation Checklist (Mapped to Spec Success Criteria)

- **SC-001 (Startup & Status)**:
  - [ ] `runtime.get_status` returns `Running` within ~10 seconds of starting the Runtime under normal conditions.
- **SC-002 (Resume Behavior)**:
  - [ ] After shutdown and `--mode resume`, agents reuse the same Agent Mail identities and conversation IDs, and unread mail present at shutdown is still available.
- **SC-003 (Agent Failure Handling)**:
  - [ ] When an agent subprocess is intentionally killed (e.g., via OS signal) during a run, the Runtime detects the failure and restarts the agent according to policy.
- **SC-004 (Inspection Latency)**:
  - [ ] `agent.get_detail` returns recent events, and `events.notify` delivers new events with end-to-end latency under ~1 second for at least 95% of events in a normal run.
- **SC-005 (15–20 Agent Scenario)**:
  - [ ] With a swarm of ~15–20 active agents, the checks above (SC-001 through SC-004) still hold for at least 90% of runs under normal conditions.

## 6. Notes

- This quickstart assumes a single Runtime instance per project directory; running multiple swarms concurrently requires running multiple Runtime processes.
- The runtime control API is bound to `localhost` only in the MVP; any remote usage should be via SSH or equivalent until explicit remote access support is added in a future iteration.
