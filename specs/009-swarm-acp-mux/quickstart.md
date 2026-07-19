# Quickstart / Validation Scenario: SwarmACPMux (Feature 008)

This document describes how to validate SwarmACPMux behavior **via tests** once the feature is implemented. It is aligned with the existing runtime orchestrator quickstart (spec 001) and the project’s `uv`/pytest workflow.

It does **not** introduce new CLI commands; instead, it focuses on integration tests under `tests/integration/runtime_acp/`.

## 1. Prerequisites

- Python 3.13+ on `PATH`.
- `nate_ntm` repository cloned locally.
- Dependencies installed using `uv` (per `.specify/memory/constitution.md`):

  ```bash
  cd /path/to/nate_ntm
  uv sync
  ```

- Ability to run tests:

  ```bash
  uv run pytest -q
  ```

- External services available (real or mocked):
  - OpenHands agent server for ACP/conversation handling.
  - Agent Mail service for mailbox coordination.

  (These are the same prerequisites as spec 001; see its quickstart for details.)

## 2. Relevant Tests

Once SwarmACPMux and its adapter integration are implemented, you should be able to validate behavior using tests such as:

- `tests/integration/runtime_acp/test_runtime_daemon_acp_async_real_path_epic005.py`
  - Existing baseline for the async ACP event path without the mux.
- `tests/integration/runtime_acp/test_swarm_acp_mux_real_path.py`
  - New real-path test that exercises the same scenario through the mux and Swarm ACP server adapter.
- `tests/integration/runtime_acp/test_reserved_swarm_controls.py`
  - New test focusing on `_attach`, `_detach`, `_swarm_status`, `_agent_detail` behavior and error codes.

Use `uv run pytest` to run these tests:

```bash
uv run pytest tests/integration/runtime_acp/test_swarm_acp_mux_real_path.py -vv
uv run pytest tests/integration/runtime_acp/test_reserved_swarm_controls.py -vv
```

## 3. High-Level Scenario: Real-Path with Mux

The `test_swarm_acp_mux_real_path.py` test should perform roughly the following steps:

1. **Start the runtime daemon** (create or resume) using the existing CLI, as in spec 001 quickstart:

   - `nate-ntm runtime start --project ... --mode create --agents N --with-control-api`
   - The test harness will typically start this as a subprocess or fixture.

2. **Start the Swarm ACP server adapter** bound to the runtime’s control API.

   - The adapter creates a `SwarmACPMux` per incoming ACP session.
   - The details (host/port, transport type) are test-fixture specific.

3. **Connect an ACP-capable client** (test harness) to the Swarm ACP server.

4. **Call the logical `_swarm_status` operation** via the ACP extension mechanism.

   - Expect a payload equivalent to `swarm.get_overview` (spec 001), wrapped with `attached_agent_id` from the mux.

5. **Choose an `agent_id`** from the status response.

6. **Attach to that agent** via `_attach`.

   - Expect:
     - a successful attach response identifying the attached agent; and
     - subsequent replay of that agent’s recent events, followed by live events.

7. **Send ordinary ACP requests** to the attached agent.

   - The client issues prompts/tool calls using the existing ACP methods.
   - The mux forwards these via `SwarmAgentClient`.
   - The test asserts that the resulting ACP updates appear on the same external session, and that corresponding `AgentEvent`s are published.

8. **Optionally, inspect the same agent via the runtime control API**.

   - Use `agent.get_detail` (runtime control API) to confirm that `events` align with what the ACP client saw.

9. **Detach** using `_detach`.

   - Expect:
     - a success response `{ "detached": true }` regardless of current attachment (idempotent);
     - cessation of new events on the external session, while the agent itself continues to run.

10. **Shut down** the ACP client, adapter, and runtime daemon cleanly.

The test should verify that the sequence and content of updates matches expectations from the baseline epic005 test, modulo the mux’s additional responsibilities.

## 4. Reserved Swarm-Control Behavior

The `test_reserved_swarm_controls.py` test should focus on logical reserved operations and error conditions.

### 4.1 `_swarm_status`

- Send the logical `_swarm_status` operation via ACP.
- Assert that:
  - The payload’s `swarm` field matches `swarm.get_overview` from the runtime control API.
  - `attached_agent_id` reflects the current mux attachment (or `null`).

### 4.2 `_agent_detail`

- For a chosen `agent_id`:
  - Send `_agent_detail(agent_id, max_events=K)`.
  - Assert that:
    - The `agent` and `events` fields match `agent.get_detail` results (bounded by `max_events`).
    - The `attached` flag is `true` iff the mux is currently attached to that agent.

### 4.3 `_attach`

- Attempt `_attach` with a valid `agent_id`:
  - Assert that the response includes `attached_agent_id`.
  - Assert that the attach acknowledgment is observed **before** any replayed events for the new attachment.

- Attempt `_attach` with an unknown `agent_id`:
  - Assert that the adapter returns an error with code `MUX_UNKNOWN_AGENT`.

### 4.4 `_detach`

- Call `_detach` twice in a row:
  - Assert that both calls succeed and return `{ "detached": true }`.

## 5. Error Handling Behavior

Tests should also cover mux-level error conditions and their ACP-visible manifestations:

- Sending an agent-directed operation with no attachment returns an error with code `MUX_NO_ATTACHED_AGENT`.
- Unknown reserved operation names (if surfaced at all) are mapped to a stable error (e.g., `MUX_INVALID_REQUEST` or a specific `UnsupportedReservedUpdate` code), depending on adapter design.
- Internal runtime or adapter failures produce `MUX_INTERNAL_ERROR` with sufficient logging on the server side.

## 6. Checklist for Feature 008

When SwarmACPMux is implemented and these tests pass, you should be able to say:

- [ ] A client can attach to any durable swarm agent and receive a bounded replay of recent events followed by live updates.
- [ ] Reserved swarm-control operations `_swarm_status` and `_agent_detail` mirror the shapes from the runtime control API.
- [ ] Reserved operations are handled at the adapter/mux boundary and never appear as tool calls or messages inside the agent conversation.
- [ ] Detach is idempotent and does not stop the agent.
- [ ] Agent-directed operations with no attachment fail with `MUX_NO_ATTACHED_AGENT`.
- [ ] The real-path ACP test through the mux (epic005-style) behaves consistently with the baseline runtime ACP path.
