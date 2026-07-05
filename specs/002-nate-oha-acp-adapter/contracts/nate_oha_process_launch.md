# Contract: nate_OHA Process Launch (NateOhaAcpClient)

This document defines the process-level contract between the `nate_ntm` runtime (via `NateOhaAcpClient`) and the `nate_OHA` executable when running ACP-backed agents with Agent Mail integration.

`NATE_OHA_GUIDE.md` remains the **normative reference** for CLI flags and environment variables. This contract describes how the runtime **must** apply that guide when launching `nate_OHA` on behalf of swarm agents.

## 1. Executable and Arguments

- **Executable name**
  - The runtime **MUST** launch a `nate_OHA` entrypoint available on the host.
  - The default expectation is that `nate_OHA` is installed as a console script on `PATH`.
  - Development environments **MAY** wrap the call in `uv run nate_OHA ...`, but this is an implementation detail; the contract is expressed in terms of the `nate_OHA` CLI.

- **Subcommand and base arguments**
  - For ACP runtime mode, the adapter **MUST** invoke the `acp` subcommand:
    - Example baseline invocation (no Agent Mail):

      ```bash
      nate_OHA acp
      ```

  - For Agent Mail integration, the adapter **MUST** pass `--enable-agent-mail` when Agent Mail is configured for the agent (FR-003):

      ```bash
      nate_OHA acp --enable-agent-mail [other-flags]
      ```

- **Additional arguments (examples)**
  - The adapter **MAY** pass additional flags that are compatible with `NATE_OHA_GUIDE.md`, such as:
    - `--confirmation-mode <mode>`
    - `--streaming`
  - Any such flags **MUST** be chosen so they do not change the fundamental ACP semantics required by the runtime (for example, they must not disable ACP event streaming).

## 2. Environment Variables

The adapter **MUST** populate the following environment variables for each nate_OHA process when Agent Mail is enabled, using values derived from `SwarmMetadata` and `AgentMetadata` (see `data-model.md`):

- `AGENT_MAIL_PROJECT`
  - Value: `SwarmMetadata.agent_mail_project_id`.
- `AGENT_MAIL_AGENT`
  - Value: `AgentMetadata.agent_mail_identity` for the agent.
- `AGENT_MAIL_TOKEN`
  - Value: derived from `AgentMetadata.agent_mail_credentials_ref` and deployment-specific configuration.
  - **MUST NOT** be logged, echoed, or exposed to the model.
- `AGENT_MAIL_UPSTREAM_URL`
  - Value: deployment-specific MCP endpoint for `mcp_agent_mail`.

Security rules (aligned with `NATE_OHA_GUIDE.md`):

- Secrets such as `AGENT_MAIL_TOKEN` **MUST NOT** appear in:
  - Tool argument lists.
  - Prompt text.
  - Logs or error messages emitted by the adapter or runtime.
- The adapter **MUST** treat env var values as opaque and avoid inspecting or serializing them beyond what is needed to start the process.

The adapter **MAY** set additional, runtime-specific environment variables that help with observability and correlation, such as:

- `NATE_NTM_SWARM_ID` 
- `NATE_NTM_AGENT_ID`
- `NATE_NTM_PROJECT_PATH`

These **MUST NOT** conflict with the `AGENT_MAIL_*` variables defined above.

## 3. Working Directory and Filesystem

- The nate_OHA process **MUST** be started with a working directory that has access to the project files needed for the agent.
  - Default: the `nate_ntm` project root, or a project-specific working directory configured in `AgentMetadata.launch_config`.
- The adapter **MUST NOT** assume write access outside the project or designated runtime directories.
- Any per-agent scratch or log directories **SHOULD** live under a project-local root (for example, `.nate_ntm/agents/<agent_id>/`) or another directory agreed in deployment configuration.

## 4. Startup Readiness and Health Checks

To satisfy FR-006 and SC-001, the adapter **MUST** implement a bounded startup and health-check protocol for each launched nate_OHA process:

1. **Spawn**
   - Start the `nate_OHA acp ...` process with the environment described above.
2. **Initial health check**
   - Within a configurable timeout (default **MUST NOT** exceed 15 seconds):
     - The adapter **MUST** verify that the ACP service is accepting connections and is correctly configured with Agent Mail.
     - The specific mechanism (e.g., a lightweight ACP status request or an internal health endpoint) **MUST** conform to nate_OHA's supported interface and may require additional capabilities under FR-010.
3. **Outcome**
   - If the health check passes, the process state transitions to `running` and the agent is marked healthy in runtime metadata/events.
   - If the health check fails or times out, the adapter **MUST** treat startup as failed, emit a clear failure event, and apply the configured restart policy.

## 5. Shutdown and Restart Behavior

The adapter, together with the runtime daemon, **MUST** own the full lifecycle of each nate_OHA subprocess.

### 5.1 Graceful shutdown

- On swarm shutdown or agent removal, the runtime **MUST**:
  - Send a termination signal that allows nate_OHA to exit cleanly (for example, SIGTERM on POSIX systems).
  - Wait up to a configurable timeout for the process to exit.
- If the process does not exit within the timeout:
  - The runtime **MUST** escalate to a hard kill (for example, SIGKILL) and mark the agent as having an unclean shutdown.

### 5.2 Restart policy

- For unexpected exits or startup failures, the runtime **MUST** consult the agent's `restart_policy` (see `data-model.md`).
- The adapter **MUST** surface enough information for policy decisions, including:
  - Exit code.
  - Whether the failure occurred during startup or while running a turn.
  - Any relevant error messages from the process (subject to security constraints).
- The runtime **MUST** avoid unbounded restart loops by enforcing limits from `restart_policy` (for example, maximum retries or exponential backoff).

### 5.3 Identity and conversation continuity

- Across shutdown and restart (whether due to swarm lifecycle or process failure), the adapter **MUST**:
  - Reuse the same Agent Mail identity and persisted OpenHands conversation identifier for each agent (FR-005, SC-002).
  - Ensure that nate_OHA is configured so that it reconnects to the existing conversation rather than creating a new one by default.

## 6. Observability and Event Surfacing

To align with FR-006, FR-007, and FR-009, the adapter **MUST** expose process- and ACP-level events through the existing runtime event pipeline:

- **Process-level events** (from the OS / subprocess layer):
  - `nate_oha_process_started` (includes `agent_id`, PID, timestamp).
  - `nate_oha_process_ready` (health check passed).
  - `nate_oha_process_start_failed` (includes reason, exit code if applicable).
  - `nate_oha_process_exited` (normal exit).
  - `nate_oha_process_crashed` (unexpected exit or repeated unresponsiveness).

- **ACP/Agent-level events** (from nate_OHA ACP event stream):
  - Turn completions, tool calls, and relevant errors mapped into `AgentEventStream`.

All such events **MUST** be mapped into existing runtime event types or clearly defined new event types, so that clients can observe nate_OHA–backed agents through the same APIs used for other ACP adapters.

## 7. Version and Compatibility Checks

- Before launching any nate_OHA process, `NateOhaAcpClient` **MUST** verify that the installed `nate_OHA` implementation satisfies the minimum supported interface or version (FR-013).
  - This verification SHOULD use a documented self-check mechanism such as `nate_OHA --version` or `nate_OHA acp --version`, but MAY also be implemented via a dedicated "capabilities" or "version" request over the ACP interface.
- If the version is incompatible:
  - The adapter **MUST** fail with a clear diagnostic.
  - The runtime **MUST NOT** attempt to launch nate_OHA–backed agents until the issue is resolved.

This contract, together with `data-model.md` and `NATE_OHA_GUIDE.md`, defines the expectations the implementation must meet for process control, identity binding, observability, and compatibility.
