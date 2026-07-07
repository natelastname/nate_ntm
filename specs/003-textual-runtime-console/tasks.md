---
description: "Implementation tasks for Feature 003: Textual Runtime Console"
---

# Tasks: Textual Runtime Console (Feature 003)

**Input**: Design documents from `/specs/003-textual-runtime-console/` and runtime API/event contracts from `/specs/001-swarm-runtime-orchestrator/`.

**Prerequisites**: `plan.md` (required), `spec.md` (required for user stories), `checklists/requirements.md`, and the runtime control/event API contracts from Feature 001.

**Tests**: This feature includes unit/integration tests for the Runtime Session abstraction, event handling and degradation, and basic Textual navigation. Where practical, tests should be runnable in headless mode using Textual's testing support or equivalent harnesses.

**Organization**: Tasks are grouped by phase and user story (US1–US3). Each task description includes concrete file paths and respects the architectural constraints from the spec and plan.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (`US1`, `US2`, `US3`)
- All descriptions **MUST** include exact file paths.

## Path Conventions

- Console code lives under `src/nate_ntm/tui/`.
- Runtime API clients and event-stream wiring live in `src/nate_ntm/tui/runtime_session.py` (and any small helpers it uses).
- Textual screens live under `src/nate_ntm/tui/screens/`.
- Shared widgets live under `src/nate_ntm/tui/widgets/`.
- Console tests live under `tests/tui/` with `unit/` and `integration/` subpackages.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Establish the TUI package skeleton, CLI entrypoint, and test layout without yet connecting to a real runtime.

- [x] T300 Create console package skeleton and CLI entrypoint.
  - Add `src/nate_ntm/tui/__init__.py`, `src/nate_ntm/tui/app.py`, `src/nate_ntm/tui/screens/__init__.py`, and `src/nate_ntm/tui/widgets/__init__.py`.
  - Add `src/nate_ntm/cli/console.py` (or extend the existing CLI module if more appropriate) with a `nate-ntm console` command that launches a stub Textual app from `nate_ntm.tui.app:ConsoleApp`.
  - Update `pyproject.toml` `[project.scripts]` (or equivalent) so that the console command is available via the `nate-ntm` entrypoint.

- [x] T301 [P] Add basic TUI test scaffolding.
  - Create `tests/tui/unit/__init__.py` and `tests/tui/integration/__init__.py`.
  - Add placeholder tests `tests/tui/unit/test_app_skeleton.py` and `tests/tui/integration/test_console_entrypoint.py` that import `ConsoleApp` and the CLI entrypoint to verify they can be instantiated without contacting a runtime.

---

## Phase 2: Client Infrastructure (Blocking Prerequisites)

**Purpose**: Implement a reusable `RuntimeClient` for protocol/transport concerns and a single shared `RuntimeSession` abstraction that owns the cached runtime model. **No screen should talk to the runtime or transports directly.**

**⚠️ CRITICAL**: Complete this phase before implementing overview/inspection/event screens.

- [x] T310 Implement the `RuntimeClient` abstraction.
  - In `src/nate_ntm/api/runtime_client.py`, define a `RuntimeClient` class that:
    - Wraps the existing `JsonRpcHttpClient` in `src/nate_ntm/api/client.py` for control-plane methods (e.g., `runtime.get_status`, `swarm.get_overview`, `agent.get_detail`).
    - Owns the `/events` WebSocket subscription, including basic reconnect logic and backoff.
    - Normalizes responses into the typed models in `src/nate_ntm/api/models.py` where appropriate.
    - Exposes an async event subscription interface (e.g., callback or async iterator) that yields high-level `AgentEvent` / runtime events.
  - Ensure `RuntimeClient` is independent of Textual and TUI concerns so it can be reused by other tools.

- [x] T311 [P] Implement the `RuntimeSession` abstraction.
  - In `src/nate_ntm/tui/runtime_session.py`, define a `RuntimeSession` class that depends on `RuntimeClient` and:
    - Is constructed with a `RuntimeClient` instance (or factory) instead of raw connection parameters.
    - Maintains cached state for runtime status, swarm overview, agents, and a bounded event buffer.
    - Exposes async methods such as `connect()`, `disconnect()`, `get_overview()`, `get_agent_detail(agent_id)`, and a way to subscribe screens/widgets to state updates.
    - Does **not** perform raw HTTP/WebSocket operations; all such work is delegated to `RuntimeClient`.

- [x] T312 [P] Implement periodic status polling in `RuntimeSession`.
  - In `runtime_session.py`, add logic to periodically call `RuntimeClient` control methods (e.g., `get_runtime_status()`, `get_swarm_overview()`) on a background task, updating cached state structures for overview and agents.
  - Provide a configuration for polling interval with a sensible default (for example, a few seconds) and ensure clean shutdown of polling tasks when the console exits.

- [x] T313 [P] Wire the runtime event stream from `RuntimeClient` into `RuntimeSession`.
  - In `runtime_session.py`, subscribe to the `RuntimeClient`'s event interface and merge incoming events into the cached state and event buffers.
  - Ensure that loss of the event stream is detected and surfaced via a status flag (e.g., `live_events_degraded=True`), without crashing the session.

- [x] T314 [P] Add unit tests for `RuntimeClient` and `RuntimeSession` behavior.
  - In `tests/tui/unit/test_runtime_session.py` and a new `tests/unit/api/test_runtime_client.py`, add tests that:
    - Verify `RuntimeClient` issues control calls via `JsonRpcHttpClient` and exposes a usable event interface.
    - Verify a single `RuntimeSession` instance uses exactly one `RuntimeClient` instance per runtime connection.
    - Confirm that periodic polling updates cached overview/agent data.
    - Confirm that event-stream loss sets a degraded flag while leaving the last-known state available.

---

## Phase 3: User Story 1 – Overview Screen (Priority: P1)

**Goal**: Provide a default overview screen that summarizes swarm health, powered entirely by `RuntimeSession`.

- [x] T320 Implement the default overview screen.
  - In `src/nate_ntm/tui/screens/overview.py`, implement an `OverviewScreen` (Textual `Screen` or `ModalScreen`) that:
    - Uses a provided, already-connected `RuntimeSession` supplied by the application.
    - Renders overall swarm health, agent counts by state, and recent activity in a compact layout.
    - Reacts to `RuntimeSession` state-change notifications (e.g., via `wait_for_update`) to update without manual refresh.

- [x] T321 [P] Implement basic swarm summary and agent list widgets.
  - In `src/nate_ntm/tui/widgets/swarm_summary.py`, implement a widget that renders aggregate swarm metrics from `RuntimeSession`'s cached overview data.
  - In `src/nate_ntm/tui/widgets/agent_table.py`, implement a widget that lists agents with their latest-known state and supports keyboard selection.

- [x] T322 [P] Wire `ConsoleApp` to use `OverviewScreen` by default.
  - In `src/nate_ntm/tui/app.py`, implement `ConsoleApp` as a Textual `App` subclass that:
    - Accepts a pre-constructed :class:`RuntimeSession` from the caller (typically the Typer CLI entrypoint).
    - Sets `OverviewScreen` as the initial screen and passes the shared `RuntimeSession` into it.

- [x] T323 Add tests for overview behavior.
  - In `tests/tui/unit/test_overview_screen.py`, add tests that:
    - Verify `OverviewScreen` requests overview data from `RuntimeSession`.
    - Verify that a mocked `RuntimeSession` update triggers UI refresh (at least at the level of Textual messages or data binding).
  - In `tests/tui/integration/test_console_against_runtime.py`, add a minimal scenario that launches a `nate_ntm` runtime (or a stub) and verifies that the overview screen populates within the SC-001 timing target under normal conditions.
  - NOTE: The current suite exercises overview wiring and update behaviour using
    fake runtime clients and headless Textual apps; a true live-runtime E2E
    console test remains a future validation step tied to SC-001/SC-003.

---

## Phase 4: User Story 2 – Agent Inspection (Priority: P2)

**Goal**: Allow operators to inspect an individual agent's latest-known state while keeping swarm context visible.

- [x] T330 Implement agent inspection view.
  - In `src/nate_ntm/tui/screens/agent_inspect.py`, implement an `AgentInspectScreen` or detail panel that:
    - Receives an `agent_id` and shared `RuntimeSession`.
    - Calls `RuntimeSession.get_agent_detail(agent_id)` to retrieve the latest-known state and related metadata.
    - Presents details sufficient to support operator inspection and diagnosis.

- [x] T331 [P] Integrate agent selection and inspection from the overview.
  - In `src/nate_ntm/tui/widgets/agent_table.py` and/or `src/nate_ntm/tui/screens/overview.py`, add keybindings or actions so that selecting an agent and invoking an "inspect" command navigates to `AgentInspectScreen` (or opens a detail panel) while keeping the overall swarm summary visible.
  - Ensure that navigation is implemented using Textual's screen management, not by creating new `RuntimeSession` instances.

- [x] T332 Add tests for agent inspection behavior.
  - In `tests/tui/unit/test_agent_inspect_screen.py`, verify that `AgentInspectScreen` uses `RuntimeSession` rather than direct runtime access, and that it renders expected fields from a mocked agent detail response.
  - In `tests/tui/integration/test_agent_inspection_flow.py`, drive a simple flow: start runtime → open console → select agent → open inspection view → confirm that swarm context remains visible.

---

## Phase 5: User Story 3 – Live Event View (Priority: P3)

**Goal**: Present a live event view alongside the overview/inspection context, with graceful degradation under high volume.

- [x] T340 Implement the live event view widget.
  - In `src/nate_ntm/tui/widgets/event_view.py`, implement a widget that subscribes to `RuntimeSession`'s event notifications and renders a scrollable list of recent events.
  - Add basic filtering hooks (e.g., by `agent_id`), implemented in terms of the cached event data in `RuntimeSession`.

- [x] T341 [P] Integrate event view into the overview layout.
  - In `src/nate_ntm/tui/screens/overview.py`, embed `event_view.py` alongside the swarm summary and agent list so operators can correlate state changes with events.
  - Ensure that event-rate spikes do not freeze the UI; rely on `RuntimeSession` to compact/window events and expose a bounded buffer.

- [x] T342 [P] Handle high-volume and degraded event conditions.
  - In `runtime_session.py`, implement event-buffer bounding (e.g., fixed-size ring buffer) and optional summarization/compaction for bursts.
  - Surface flags or summary metrics (e.g., "N events dropped") to the event view so operators understand that the log is a windowed view under high volume.

- [x] T343 Add tests for event view and degradation behavior.
  - In `tests/tui/unit/test_event_handling.py`, verify that:
    - High-volume event input does not grow memory unboundedly.
    - Degraded event-stream conditions set appropriate flags that the UI can display.
  - In `tests/tui/integration/test_event_view_against_runtime.py`, simulate runtime event bursts and confirm that the console remains responsive and that operators can still correlate visible state changes with events (SC-005).
  - NOTE: The current test suite exercises these behaviours using fake runtime
    clients and headless Textual apps; a real-runtime E2E scenario remains
    available as a future enhancement.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Align navigation, error handling, and documentation with the architectural constraints and success criteria.

- [x] T350 Implement connection lifecycle and error UX.
  - In `src/nate_ntm/tui/widgets/swarm_summary.py` and `src/nate_ntm/tui/widgets/event_view.py`, implement clear indicators for:
    - Connected vs. disconnected states.
    - Degraded live-events visibility while periodic snapshots remain available.
  - Ensure these states are driven solely by `RuntimeSession` state (e.g., ``is_connected``, ``control_degraded``, ``events_degraded``), not ad hoc connection checks in screens.

- [x] T351 [P] Add keyboard help and discoverability.
  - In `src/nate_ntm/tui/screens/overview.py`, expand the ``BINDINGS`` list so that Textual's :class:`Footer` can surface navigation and core actions (quit, inspect agent, shutdown runtime) as on-screen key help.

- [x] T352 [P] Align requirements checklist and docs.
  - Update `specs/003-textual-runtime-console/checklists/requirements.md` to reference the concrete implementation elements (files, tests) introduced in this tasks file.
  - Add a short console quickstart/usage note under `specs/003-textual-runtime-console/quickstart.md` describing how to start a runtime with the control API, launch the console via `uv run nate-ntm console`, and use the core keyboard actions (quit, inspect agent, shutdown runtime).

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies – can start immediately.
- **Phase 2 (Runtime Session)**: Depends on Phase 1 skeleton; **blocks** all screen work.
- **Phase 3 (US1 – Overview)**: Depends on Phase 2; delivers the MVP monitoring screen.
- **Phase 4 (US2 – Agent Inspection)**: Depends on Phase 3; builds on overview navigation and `RuntimeSession` detail APIs.
- **Phase 5 (US3 – Event View)**: Depends on Phase 2; can proceed in parallel with Phase 4 once overview is stable.
- **Phase 6 (Polish)**: Depends on Phases 3–5 as desired; can run in parallel after core flows work.

### Parallel Opportunities

- All tasks marked `[P]` can be worked on in parallel once their phase prerequisites are satisfied.
- Within Phase 2, T311–T313 can proceed in parallel after an initial `RuntimeSession` skeleton (T310) exists.
- Phases 4 and 5 can be developed by different contributors in parallel once Phase 3 has established the basic console structure.

### Notes

- Screens and widgets MUST NOT create their own runtime connections; all communication goes through the single shared `RuntimeSession`.
- This feature does **not** implement Agent Mail, ACP, or log/dashboard screens; it only prepares the console architecture and implements the three user stories in `spec.md`.
- Each user story should be independently testable against its acceptance scenarios and success criteria from `specs/003-textual-runtime-console/spec.md`.
