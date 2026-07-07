# Specification Quality Checklist: Textual Runtime Console

**Purpose**: Validate specification completeness and quality before proceeding to planning  
**Created**: 2026-07-06  
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs)
- [x] Focused on user value and business needs
- [x] Written for non-technical stakeholders
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Requirements are testable and unambiguous
- [x] Success criteria are measurable
- [x] Success criteria are technology-agnostic (no implementation details)
- [x] All acceptance scenarios are defined
- [x] Edge cases are identified
- [x] Scope is clearly bounded
- [x] Dependencies and assumptions identified

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria (via referenced user scenarios and measurable outcomes)
- [x] User scenarios cover primary flows
- [x] Feature meets measurable outcomes defined in Success Criteria (when implemented as specified)
- [x] No implementation details leak into specification


## Implementation Traceability (post-implementation)

The following mapping links key functional requirements from `spec.md` to the
primary implementation and test artefacts in the console codebase. It is kept
in sync with `specs/003-textual-runtime-console/tasks.md`.

- **FR-001 – console launch & connection**
  - Implementation: `src/nate_ntm/cli.py` (``nate-ntm console`` Typer command),
    `src/nate_ntm/tui/app.py` (``ConsoleApp`` wiring to a shared
    :class:`RuntimeSession`).
  - Tests: `tests/tui/integration/test_console_entrypoint.py`.

- **FR-002 – single shared Runtime Session**
  - Implementation: `src/nate_ntm/api/runtime_client.py` (``RuntimeClient``),
    `src/nate_ntm/tui/runtime_session.py` (session state and caching), and
    `src/nate_ntm/tui/app.py` (single shared session per console process).
  - Tests: `tests/tui/unit/test_runtime_session.py`,
    `tests/tui/unit/test_app_skeleton.py`.

- **FR-003 / FR-004 – overview screen and automatic updates**
  - Implementation: `src/nate_ntm/tui/screens/overview.py`
    (``OverviewScreen`` layout and ``_watch_session_updates`` background task).
  - Tests: `tests/tui/unit/test_overview_screen.py` (overview rendering and
    update watcher), `tests/tui/integration/test_event_view_flow.py` (end-to-end
    overview/event wiring with a fake runtime client).

- **FR-005 / FR-006 – agent selection and inspection**
  - Implementation: `src/nate_ntm/tui/widgets/agent_table.py`
    (keyboard-driven selection backed by ``RuntimeSession.selected_agent_id``),
    and `src/nate_ntm/tui/screens/agent_inspect.py` (``AgentInspectScreen``
    using :meth:`RuntimeSession.get_agent_detail`).
  - Tests: `tests/tui/unit/test_overview_screen.py` (selection reflected in the
    agent table), `tests/tui/unit/test_agent_inspect_screen.py`,
    `tests/tui/integration/test_agent_inspection_flow.py`.

- **FR-007 / FR-008 – live events via public runtime interfaces**
  - Implementation: `src/nate_ntm/api/runtime_client.py` (event iterator),
    `src/nate_ntm/tui/runtime_session.py` (bounded ``event_buffer`` and
    degraded-event flags), and `src/nate_ntm/tui/widgets/event_view.py`
    (read-only event rendering from the session buffer).
  - Tests: `tests/tui/unit/test_runtime_session.py` (event buffering and
    degradation), `tests/tui/unit/test_event_view.py`,
    `tests/tui/integration/test_event_view_flow.py`.

- **FR-009 – in-console graceful runtime shutdown**
  - Implementation: :meth:`RuntimeSession.shutdown_runtime` in
    `src/nate_ntm/tui/runtime_session.py`, and the shutdown flow driven by
    ``RuntimeShutdownConfirmScreen`` and
    :meth:`OverviewScreen.action_shutdown_runtime` in
    `src/nate_ntm/tui/screens/overview.py`.
  - Tests: `tests/tui/unit/test_runtime_session.py::test_shutdown_runtime_delegates_to_client_and_marks_degraded`,
    `tests/tui/integration/test_runtime_shutdown_flow.py::test_overview_shutdown_flow_requests_runtime_shutdown_and_exits`.

- **FR-010 – connection errors and degraded UX**
  - Implementation: connection and degradation tracking in
    `src/nate_ntm/tui/runtime_session.py` (e.g., ``is_connected``,
    ``control_degraded``, ``events_degraded``) surfaced via
    `src/nate_ntm/tui/widgets/swarm_summary.py` and
    `src/nate_ntm/tui/widgets/event_view.py`.
  - Tests: `tests/tui/unit/test_runtime_session.py` (degraded flags),
    `tests/tui/unit/test_overview_screen.py`,
    `tests/tui/unit/test_event_view.py`.

- **FR-011 – keyboard navigation and visible help**
  - Implementation: ``BINDINGS`` on `src/nate_ntm/tui/screens/overview.py`
    (quit, inspect agent, shutdown runtime) together with Textual's
    :class:`Footer` widget for on-screen key hints.
  - Tests: `tests/tui/unit/test_overview_screen.py::test_overview_screen_bindings_expose_core_actions`.

## Notes

- All checklist items currently pass based on the normative content of `spec.md`.  
- The user-input line records that the implementation will use a specific TUI library, but the requirements and success criteria themselves remain technology-agnostic.  
- This checklist should be revisited if the spec is substantially revised or expanded.
