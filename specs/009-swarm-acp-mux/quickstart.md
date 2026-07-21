# Quickstart / Validation Scenario: SwarmACPMux (Epic 009)

This document describes how to validate SwarmACPMux behavior **via tests** once the feature is implemented. It is aligned with the existing runtime orchestrator quickstart (spec 001) and the project’s `uv`/pytest workflow.

It focuses on integration tests under `tests/integration/runtime_acp/` and `tests/integration/acp/`, plus unit tests for the mux and adapter.

---

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

- External components required for the **real-path** integration tests:

  - An ACP-capable agent server (for example, OpenHands) that can host at least one agent with a live typed `AcpSessionUpdateStream`.
  - The Swarm ACP server adapter process bound to the runtime control API.

  Small fakes or mocks are acceptable for *targeted* unit tests, but at least one end-to-end test MUST exercise the real typed streaming path (`AcpSessionUpdateStream` → subscription context → mux attachment transaction → external connection).

---

## 2. Relevant Tests

Once SwarmACPMux and its adapter integration are implemented, you should be able to validate behavior using tests such as:

- `tests/unit/runtime/test_swarm_acp_mux.py`
  - Unit tests for mux behavior: attachment lifecycle, forwarding, error model, concurrency, and `wait_failed()` semantics.

- `tests/unit/runtime/test_swarm_acp_server.py`
  - Unit tests for the Swarm ACP server adapter: reserved-control routing, error mapping, and connection lifetime behavior.

- `tests/integration/runtime_acp/test_runtime_daemon_acp_async_real_path_epic005.py`
  - Existing baseline for the async ACP event path without the mux.

- `tests/integration/acp/test_swarm_acp_mux_real_path.py`
  - Real-path test that exercises a similar scenario through the mux and Swarm ACP server adapter.

- `tests/integration/acp/test_reserved_swarm_controls.py`
  - Tests focusing on `_attach`, `_detach`, `_swarm_status`, `_agent_detail` behavior and error codes.

- `tests/integration/acp/test_swarm_acp_server_transport.py`
  - Real ACP transport tests for the Swarm ACP server adapter over JSON-RPC.

Run these with `uv run pytest`:

```bash
uv run pytest tests/unit/runtime/test_swarm_acp_mux.py -vv
uv run pytest tests/unit/runtime/test_swarm_acp_server.py -vv
uv run pytest tests/integration/acp/test_swarm_acp_mux_real_path.py -vv
uv run pytest tests/integration/acp/test_reserved_swarm_controls.py -vv
uv run pytest tests/integration/acp/test_swarm_acp_server_transport.py -vv
```

---

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

   - Expect a payload equivalent to `get_swarm_status` (spec 001), wrapped with `attached_agent_id` from the mux.

5. **Choose an `agent_id`** from the status response.

6. **Attach to that agent** via `_attach`.

   - The adapter should implement the three-stage attachment transaction, with a token-/flag-aware abort on acknowledgment failure:

     ```python
     prepared = await mux.prepare_attach(agent_id)

     try:
         await external_connection.send_attach_acknowledgment(...)
     except BaseException:
         # MUST roll back any newly prepared attachment without tearing down
         # a pre-existing healthy attachment that was reused idempotently
         await mux.abort_attachment(prepared)  # or equivalent token-/flag-aware abort
         raise

     await mux.activate_attachment(prepared)
     ```

   - Expect:
     - a successful attach response identifying the attached agent; and
     - subsequent replay of that agent’s recent typed ACP updates (`SessionUpdate` objects), followed by live updates.
   - Verify that **no update from the new agent appears before the `_attach` acknowledgment**.

7. **Send ordinary ACP requests** to the attached agent.

   - The client issues prompts/tool calls using the existing ACP methods.
   - The mux routes these via `SwarmAgentClient` to the attached agent.
   - The test asserts that the resulting typed `SessionUpdate` objects appear on the same external session in the correct order.

8. **Optionally, inspect the same agent via the runtime control API**.

   - Use `get_agent_detail` (runtime control API) to confirm that recent `AgentEvent` telemetry aligns with what the ACP client saw (modulo intentional loss of protocol-level detail).

9. **Detach** using `_detach`.

   - Expect:
     - a success response `{ "detached": true }` regardless of current attachment (idempotent);
     - cessation of new updates on the external session, while the agent itself continues to run and other subscribers remain active.

10. **Shut down** the ACP client, adapter, and runtime daemon cleanly.

The test should verify that the sequence and content of updates matches expectations from the baseline epic005 test, modulo the mux’s additional attachment and failure-handling semantics.

---

## 4. Reserved Swarm-Control Behavior

The `test_reserved_swarm_controls.py` test should focus on logical reserved operations and error conditions.

### 4.1 `_swarm_status`

- Send the logical `_swarm_status` operation via ACP.
- Assert that:
  - The payload’s `swarm` field matches `get_swarm_status` from the runtime control API.
  - `attached_agent_id` reflects the current mux attachment (or `null`).

### 4.2 `_agent_detail`

- For a chosen `agent_id`:
  - Send `_agent_detail(agent_id, max_events=K)`.
  - Assert that:
    - The `agent` and `events` fields match `get_agent_detail` results (bounded by `max_events`).
    - The `attached` flag is `true` iff the mux is currently attached to that agent.

### 4.3 `_attach`

- Attempt `_attach` with a valid `agent_id`:
  - Assert that the response includes `attached_agent_id`.
  - Assert that the attach acknowledgment is observed **before** any replayed updates for the new attachment.

- Attempt `_attach` with an unknown `agent_id`:
  - Assert that the adapter returns an error with code `MUX_UNKNOWN_AGENT`.

- Attempt to activate an attachment with a stale `PreparedAttachment` token:
  - Assert that the adapter maps `StaleAttachmentError` to `MUX_STALE_ATTACHMENT`.

### 4.4 `_detach`

- Call `_detach` twice in a row:
  - Assert that both calls succeed and return `{ "detached": true }`.

---

## 5. Error Handling Behavior

Tests should also cover mux-level error conditions and their ACP-visible manifestations:

- Sending an agent-directed operation with an **open** mux and no attachment returns an error with code `MUX_NO_ATTACHED_AGENT`.
- Sending any mux-dependent operation after the mux has been closed returns an error with code `MUX_CLOSED`.
- Attempting to attach to a known `agent_id` that currently has no active ACP session returns an error with code `MUX_AGENT_SESSION_NOT_ACTIVE`.
- Unknown reserved operation names (if surfaced at all) are mapped to a stable error (for example, `MUX_INVALID_REQUEST`), depending on adapter design.
- Internal runtime or adapter failures produce `MUX_INTERNAL_ERROR` with sufficient logging on the server side.

---

## 6. Checklist for Epic 009

When SwarmACPMux is implemented and these tests pass, you should be able to say:

- [ ] A client can attach to any durable swarm agent with an active ACP session and receive a bounded replay of recent typed updates followed by live updates.
- [ ] Reserved swarm-control operations `_swarm_status` and `_agent_detail` mirror the shapes from the runtime control API.
- [ ] Reserved operations are handled at the adapter/mux boundary and never appear as tool calls or messages inside the agent conversation.
- [ ] Detach is idempotent and does not stop the agent or affect other subscribers.
- [ ] Agent-directed operations with no attachment fail with `MUX_NO_ATTACHED_AGENT`.
- [ ] The real-path ACP test through the mux behaves consistently with the baseline runtime ACP path, modulo the mux’s additional attachment and failure-handling semantics.
