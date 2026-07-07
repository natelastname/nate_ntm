# Runtime Console Quickstart

This short guide describes how to start a development runtime and connect the
Textual runtime console to it using the `uv` workflow.

The console is designed to be run **against a single runtime instance** and to
present an at-a-glance overview of swarm health, agent state, and recent
runtime events.

## 1. Start a runtime with the control API enabled

In one terminal, from the project root, start a runtime with the JSON‑RPC
control API exposed:

```bash
uv run nate-ntm runtime start \
  --project /path/to/your/project \
  --adapter-mode fake \
  --with-control-api
```

Key points:

- `--project` points at a project directory that has (or will have)
  `.nate_ntm/` metadata.
- `--adapter-mode fake` is suitable for local development and tests; adjust if
  you have other adapters configured.
- `--with-control-api` ensures the runtime exposes the control API on the
  default host/port (`127.0.0.1:8765`), which the console uses.

Leave this terminal running; the runtime will stay up until you request a
shutdown (for example, from the console itself).

## 2. Launch the Textual console

In a second terminal, also from the project root, launch the console connected
to the running runtime:

```bash
uv run nate-ntm console
```

By default this connects to `127.0.0.1:8765`. To target a different host or
port (for example, a remote runtime), pass `--host` / `--port`:

```bash
uv run nate-ntm console --host 192.0.2.10 --port 9000
```

If the console cannot establish an initial connection, it exits with a clear
error message instead of starting Textual and failing immediately.

## 3. Reading the overview layout

Once connected, the console opens on the **overview screen**, which is fed
entirely by a shared `RuntimeSession` instance:

- **Header** – standard Textual header with the application title and clock.
- **Swarm summary** (top pane) – derived from the runtime status and swarm
  overview:
  - connection state: `Connection: connected` / `Connection: disconnected`
  - runtime status, project path, swarm id
  - aggregate agent counts by state
  - inline degraded indicators such as `[control degraded: …]` or
    `[events degraded: …]` when the control API or event stream is unhealthy
- **Agent table** (middle‑left) – a simple list of agents showing id,
  display name, and status. The currently selected agent is marked with `>`.
- **Agent detail panel** (middle‑right) – a focused summary for the selected
  agent (status and key metadata) driven by `RuntimeSession`.
- **Event view** (bottom) – a small window of recent runtime/agent events with a
  header like `Events (most recent last)` and optional degradation hints when
  events or control state are degraded.
- **Footer** – Textual footer showing key bindings for common actions.

All of these components read from the shared `RuntimeSession` and never talk to
JSON‑RPC or `/events` directly.

## 4. Basic navigation and actions

The console is keyboard‑driven. The most important keys are:

- **Up / Down** – move the selection in the agent table (updates
  `RuntimeSession.selected_agent_id`).
- **Enter** – inspect the currently selected agent. This opens the agent
  inspection screen, which provides more detail while keeping swarm context
  available.
- **q** – quit the console and return to the shell.
- **x** – request a graceful runtime shutdown:
  - Press `x` on the overview screen to open the **runtime shutdown
    confirmation** screen.
  - Press `y` to confirm the shutdown request.
  - Press `n` or `Esc` to cancel and return to the overview.

The shutdown confirmation flow uses `RuntimeSession.shutdown_runtime()` under
the hood and then disconnects the session before exiting the app, satisfying
FR‑009's requirement for an in‑console, confirmed shutdown.

## 5. Interpreting degraded states

The console distinguishes between connection state and degraded control/event
state:

- When `RuntimeSession.is_connected` is `False`, the overview reports
  `Connection: disconnected` and cached snapshots may be absent.
- When the control API is unhealthy, `RuntimeSession.control_degraded` is set
  and `SwarmSummary` renders an inline `[control degraded: …]` marker and any
  available error string.
- When the event stream is unhealthy or lagging, `RuntimeSession.events_degraded`
  is set and both the swarm summary and event view indicate that live events are
  degraded while still showing the last‑known snapshots from polling.

These indicators are intentionally minimal for this feature slice but sufficient
for operators to see when they are looking at fully live data versus a
stale‑but‑useful snapshot.
