---
description: “Implementation tasks for Feature 009: SwarmACPMux"
---

# Tasks: SwarmACPMux (Epic 009)

**Input**: Design documents in `specs/009-swarm-acp-mux/`

**Prerequisites**:

- `specs/009-swarm-acp-mux/spec.md` — normative behavior
- `specs/009-swarm-acp-mux/plan.md`
- `specs/009-swarm-acp-mux/research.md`
- `specs/009-swarm-acp-mux/data-model.md`
- `specs/009-swarm-acp-mux/contracts/swarm-acp-mux-session.md`
- `specs/009-swarm-acp-mux/quickstart.md`

**Tests**: Add focused unit tests for mux lifecycle behavior and a small number of real-path integration tests through the production Swarm ACP server adapter.

**Organization**: Tasks are grouped into:

1. foundational mux types;
2. attachment and forwarding;
3. production ACP adapter and reserved controls;
4. lifecycle robustness and macro integration;
5. final validation.

## Format: [ID] [P?] [Story] Description

- **[P]**: May run in parallel because it modifies different files and has no incomplete prerequisite.
- **[Story]**: `US1`, `US2`, or `US3`.
- Every task names the exact file or files it changes.
- User-story tasks include the appropriate `[US#]` label.
- Setup and final validation tasks are intentionally untagged.

## Canonical Paths

```
src/nate_ntm/runtime/swarm_acp_mux.py
src/nate_ntm/runtime/swarm_acp_server.py
src/nate_ntm/runtime/__init__.py

tests/unit/runtime/test_swarm_acp_mux.py
tests/unit/runtime/test_swarm_acp_server.py
tests/integration/acp/test_swarm_acp_mux_real_path.py
tests/integration/acp/test_reserved_swarm_controls.py
```

`src/nate_ntm/runtime/swarm_acp_server.py` is the single production adapter implementation for Epic 009. Tests MUST exercise that implementation rather than creating a second test-only adapter.

------------------------------------------------------------------------

## Phase 1: Foundational Types and Boundaries

**Purpose**: Establish the complete mux data model and dependency boundaries required by all user stories.

- [x] T001 Implement the foundational Epic 009 types in `src/nate_ntm/runtime/swarm_acp_mux.py`:

  - `SwarmAgentClient`;
  - `ExternalACPConnection`;
  - `PreparedAttachment`;
  - `_Attachment`;
  - `SwarmACPMux`;
  - `SwarmACPMuxError`;
  - `SwarmACPMuxClosedError`;
  - `UnknownAgentError`;
  - `NoAttachedAgentError`;
  - `StaleAttachmentError`;
  - `UnsupportedReservedUpdateError`.

  Match `specs/009-swarm-acp-mux/spec.md` §§5–6 and §13 and `specs/009-swarm-acp-mux/data-model.md` §1. Include all connection-local state fields, but do not implement attachment behavior in this task.
- [x] T002 Implement mux initialization and shared lifecycle primitives in `src/nate_ntm/runtime/swarm_acp_mux.py`:

  - initialize `_failure` in `__post_init__`;
  - add open-state and current-attachment validation helpers;
  - add identity comparison using the concrete `_Attachment` or its token;
  - add internal subscription-exit cleanup that is safe to call exactly once.

  Follow `specs/009-swarm-acp-mux/spec.md` §§6, 11, and 14.
- [x] T003 Export the public Epic 009 mux types from `src/nate_ntm/runtime/__init__.py`, including `SwarmACPMux`, `PreparedAttachment`, and the public mux error classes.

------------------------------------------------------------------------

## Phase 2: User Story 1 — Attachment, Forwarding, and Agent Operations

**Priority**: P1 — MVP

**Goal**: One external ACP session can attach to one concrete agent ACP session, acknowledge the attachment before replay begins, receive retained and live typed updates in order, issue prompts and interrupts, and detach without stopping the agent or disrupting other subscribers.

**Independent Test**: Through the production adapter in `src/nate_ntm/runtime/swarm_acp_server.py`, an external session can perform:

```
_attach
→ attachment acknowledgment
→ retained SessionUpdate replay
→ live SessionUpdate delivery
→ prompt
→ interrupt
→ _detach
```

No new-agent update may appear before acknowledgment. Detach must leave the agent and independent subscribers active.

### Tests for User Story 1

- [x] T004 [US1] Add attachment transaction tests to `tests/unit/runtime/test_swarm_acp_mux.py` covering:

  - successful first attachment;
  - `AgentSessionNotActive` propagation;
  - unknown durable agent rejection;
  - no active attachment after failed subscription establishment;
  - `PreparedAttachment.newly_prepared=True` for a fresh attachment;
  - `PreparedAttachment.newly_prepared=False` for a healthy same-agent attachment;
  - stale prepared handles cannot activate or remove a newer attachment.

- [x] T005 [US1] Add acknowledgment and rollback tests to `tests/unit/runtime/test_swarm_acp_mux.py` covering:

  - no replay or live forwarding before activation;
  - successful activation begins retained replay before live delivery;
  - acknowledgment failure rolls back a newly prepared attachment;
  - acknowledgment failure leaves a reused healthy same-agent attachment intact;
  - `abort_attachment()` with a stale handle does not alter the current attachment.

- [x] T006 [US1] Add forwarding and switching tests to `tests/unit/runtime/test_swarm_acp_mux.py` covering:

  - forwarding the underlying `SessionUpdate` unchanged;
  - preserving the order yielded by Epic 008;
  - delivering an update published during preparation exactly once;
  - stopping and awaiting the old forwarding task before establishing the new attachment;
  - exiting the old subscription before the new acknowledgment;
  - preventing old-agent output after the new-agent acknowledgment;
  - preventing an obsolete forwarding task from clearing a newer attachment.

- [x] T007 [US1] Add agent-operation and detach tests to `tests/unit/runtime/test_swarm_acp_mux.py` covering:

  - `prompt()` delegates to the attached agent;
  - `interrupt()` delegates to the attached agent;
  - both raise `NoAttachedAgentError` while unattached;
  - `detach()` is idempotent;
  - detach exits only this mux's subscription;
  - detach leaves the underlying agent running;
  - an independent subscriber remains active after mux detachment.

### Implementation for User Story 1

- [x] T008 [US1] Implement `prepare_attach()` in `src/nate_ntm/runtime/swarm_acp_mux.py` with:

  - closed-state rejection;
  - durable membership validation through `RuntimeDaemon`;
  - `_lifecycle_lock` serialization;
  - healthy same-agent reuse with `newly_prepared=False`;
  - complete removal of an obsolete or different attachment;
  - one call to `subscribe_acp_updates(agent_id)`;
  - retention of the entered subscription and concrete iterator;
  - a unique token for the resulting `_Attachment`;
  - `newly_prepared=True` for a fresh attachment.

- [x] T009 [US1] Implement `activate_attachment()` and `abort_attachment()` in `src/nate_ntm/runtime/swarm_acp_mux.py`:

  - activation validates the current token;
  - activation starts at most one forwarding task;
  - activation of a reused healthy attachment is a no-op;
  - abort removes a fresh, still-current prepared attachment;
  - abort preserves a reused healthy attachment;
  - abort with a stale token never changes the current attachment.

- [x] T010 [US1] Implement `_run_forwarding()` and `_attachment_finished()` in `src/nate_ntm/runtime/swarm_acp_mux.py`:

  - consume only the iterator entered by `prepare_attach()`;
  - wait for activation before consuming updates;
  - call `ExternalACPConnection.session_update()` with the underlying `SessionUpdate`;
  - preserve Epic 008 ordering;
  - clean up normal exhaustion without closing the mux;
  - use attachment identity before clearing mux state.

- [x] T011 [US1] Implement mux failure observation in `src/nate_ntm/runtime/swarm_acp_mux.py`:

  - `_report_failure()` records only the first fatal forwarding failure;
  - `wait_failed()` re-raises that failure;
  - iterator and external-write exceptions are fatal;
  - normal stream exhaustion is not fatal;
  - cancellation caused by detach or close is not fatal;
  - clean close cancels a still-pending failure waiter.

- [x] T012 [US1] Implement `prompt()`, `interrupt()`, and `detach()` in `src/nate_ntm/runtime/swarm_acp_mux.py` according to `specs/009-swarm-acp-mux/spec.md` §§8.4–8.6:

  - prompt and interrupt require an open mux and current attachment;
  - detach is idempotent;
  - detach clears current state before awaiting task termination;
  - detach cancels and awaits forwarding;
  - detach exits the retained subscription exactly once;
  - detach does not stop the agent.

- [x] T013 [US1] Implement `close()`, `__aenter__()`, and `__aexit__()` in `src/nate_ntm/runtime/swarm_acp_mux.py`:

  - close becomes effective exactly once;
  - close detaches the current attachment;
  - close cancels pending `wait_failed()` callers without reporting a failure;
  - subsequent public operations raise `SwarmACPMuxClosedError`.

- [x] T014 [US1] Implement the minimal production session and `_attach`/`_detach` flow in `src/nate_ntm/runtime/swarm_acp_server.py`:

  - create exactly one `SwarmACPMux` per external ACP session;
  - execute `_attach` as prepare → acknowledgment → activate;
  - call `abort_attachment()` if acknowledgment fails or is cancelled;
  - dispatch `_detach` to `mux.detach()`;
  - route ordinary prompt and interrupt operations through the mux;
  - serialize `_attach`, `_detach`, and session shutdown for one external session.

- [x] T015 [US1] Add the MVP real-path test in `tests/integration/acp/test_swarm_acp_mux_real_path.py` using the production code in `src/nate_ntm/runtime/swarm_acp_server.py`. Verify:

  - a real Epic 008 subscription is established before acknowledgment;
  - no retained or live update appears before acknowledgment;
  - retained output precedes live output;
  - prompt and interrupt reach the attached agent;
  - detach stops mux delivery without stopping the agent;
  - an independent subscriber continues receiving updates.

------------------------------------------------------------------------

## Phase 3: User Story 2 — Reserved Controls and Runtime Views

**Priority**: P2

**Goal**: The production Swarm ACP session layer exposes `_swarm_status`, `_agent_detail`, `_attach`, and `_detach` with the required logical semantics. Ordinary reserved controls use the logical dispatcher, while `_attach` uses the explicit acknowledgment-aware attachment transaction so that its success response can be written before forwarding begins.

**Independent Test**: Through the production session surface, reserved controls return the contract-defined payloads and errors, `_attach` preserves acknowledgment-before-forwarding ordering, and no underscore-prefixed client control is forwarded to an agent.

### Tests for User Story 2

- [x] T016 [P] [US2] Add mux view tests to `tests/unit/runtime/test_swarm_acp_mux.py` covering:

  - `get_swarm_status()` returns daemon-owned status plus `attached_agent_id`;
  - `get_agent_detail()` returns daemon-owned detail plus the connection-local `attached` flag;
  - unknown agents raise `UnknownAgentError`;
  - `max_events` is passed through unchanged.

- [x] T017 [US2] Add production session routing and error-mapping tests to `tests/unit/runtime/test_swarm_acp_server.py` covering:

  - `_swarm_status`;
  - `_agent_detail`;
  - `_detach`;
  - `_attach` through the explicit acknowledgment-aware attachment API;
  - rejection of attempts to process `_attach` as an ordinary return-value control;
  - malformed reserved requests;
  - unknown reserved operation names;
  - reserved operations never reaching the attached agent;
  - underscore-prefixed output emitted by an agent being forwarded normally;
  - stable mappings for all logical `MUX_*` codes in the contract;
  - acknowledgment completing before any retained or live update is forwarded.

### Implementation for User Story 2

- [x] T018 [US2] Implement `get_swarm_status()` and `get_agent_detail()` in `src/nate_ntm/runtime/swarm_acp_mux.py` using the daemon-owned views and response shapes defined in `specs/009-swarm-acp-mux/contracts/swarm-acp-mux-session.md` §§3.1–3.2.
- [x] T019 [US2] Complete reserved-control parsing and session dispatch in `src/nate_ntm/runtime/swarm_acp_server.py`:

  - validate reserved-control request payloads;
  - dispatch `_swarm_status`, `_agent_detail`, and `_detach` through `handle_reserved_control()`;
  - expose `_attach` through the explicit `attach(…, acknowledge=…)` transaction rather than as an ordinary return-value control;
  - require the concrete ACP adapter to write the `_attach` success response from the acknowledgment callback before activation;
  - reject attempts to process `_attach` through `handle_reserved_control()`;
  - reject unknown underscore-prefixed controls;
  - never route reserved client controls to an agent;
  - leave underscore-prefixed agent output untouched.

- [x] T020 [US2] Implement complete domain-to-logical-error-code mapping in `src/nate_ntm/runtime/swarm_acp_server.py` for:

  - `MUX_NO_ATTACHED_AGENT`;
  - `MUX_CLOSED`;
  - `MUX_UNKNOWN_AGENT`;
  - `MUX_AGENT_SESSION_NOT_ACTIVE`;
  - `MUX_STALE_ATTACHMENT`;
  - `MUX_INVALID_REQUEST`;
  - `MUX_INTERNAL_ERROR`.

  Log unexpected internal failures without exposing internal details to the external client. Construction of the concrete ACP wire-level error envelope remains the responsibility of the concrete ACP adapter.
- [x] T021 [US2] Add reserved-control and attachment-transaction integration coverage in `tests/integration/acp/test_reserved_swarm_controls.py` using `src/nate_ntm/runtime/swarm_acp_server.py`. Verify:

  - contract-defined `_swarm_status` and `_agent_detail` payloads;
  - idempotent `_detach`;
  - same-agent attachment behavior through `SwarmACPServerSession.attach(…)`;
  - acknowledgment-before-forwarding semantics;
  - logical error-code mapping;
  - preservation of independent Epic 008 stream subscribers.

  This test operates at the production session boundary. Concrete ACP wire decoding, response encoding, and transport-level ordering are covered by the later concrete-adapter integration work.

## Phase 4: User Story 3 — Connection Lifetime, Races, and Failure Propagation

**Priority**: P3

**Goal**: The production adapter closes cleanly and deterministically when inbound processing ends, forwarding fails, attachments switch, or shutdown races with lifecycle operations.

**Independent Test**: A real external ACP session can run through attachment, switching, ordinary operations, forwarding failure, and shutdown without stale state, leaked tasks, duplicate subscriptions, or hung connection handlers.

### Tests for User Story 3

- [x] T022 [P] [US3] Add mux lifecycle race tests to `tests/unit/runtime/test_swarm_acp_mux.py` covering:

  - detach racing with attachment;
  - close racing with attachment;
  - old forwarding completion after a new preparation;
  - stale activation after detach;
  - normal agent-stream exhaustion leaving the mux open and unattached;
  - first-failure-only behavior;
  - clean cancellation of `wait_failed()` during close.

- [x] T023 [US3] Add production adapter lifetime tests to `tests/unit/runtime/test_swarm_acp_server.py` covering:

  - normal inbound completion cancels the failure watcher;
  - a forwarding failure cancels inbound processing;
  - inbound failure cancels the failure watcher;
  - the losing task is always awaited;
  - cleanup closes the mux and transport;
  - `_attach`, `_detach`, and shutdown never overlap for one external session.

- [x] T024 [US3] Extend `tests/integration/acp/test_swarm_acp_mux_real_path.py` with one macro scenario that:

  - attaches to agent A;
  - receives retained and live updates;
  - sends a prompt and interrupt;
  - switches to agent B;
  - verifies no A output after B acknowledgment;
  - verifies B replay before B live output;
  - injects or triggers an external forwarding failure;
  - confirms the outer connection handler terminates and cleans up;
  - confirms both agents remain runtime-managed.

### Implementation for User Story 3

- [x] T025 [US3] Implement the first-completion connection lifetime in `src/nate_ntm/runtime/swarm_acp_server.py`:

  - race inbound request processing against `mux.wait_failed()`;
  - cancel and await the loser;
  - propagate the winner's failure;
  - treat normal inbound completion as connection termination;
  - always close the mux and concrete external transport.

- [x] T026 [US3] Enforce the single-threaded per-session control stream in `src/nate_ntm/runtime/swarm_acp_server.py`:

  - no second `_attach` or `_detach` begins while an attachment transaction is in flight;
  - shutdown cannot interleave between `prepare_attach()` and `activate_attachment()`;
  - ordinary prompt and interrupt requests may proceed only according to the adapter concurrency rules established by the ACP SDK integration.

- [x] T027 [US3] Finalize lifecycle logging in `src/nate_ntm/runtime/swarm_acp_mux.py` and `src/nate_ntm/runtime/swarm_acp_server.py`:

  - record attachment preparation, activation, switching, detach, and close at useful levels;
  - log fatal forwarding failures exactly once;
  - include agent and external-session identifiers needed for diagnosis;
  - avoid duplicate tracebacks for expected cancellation and normal shutdown.

### US3: Addendum

- [x] T027.1 [US3] Implement the concrete Swarm ACP request adapter in `src/nate_ntm/runtime/swarm_acp_server.py`:
  - decode inbound ACP requests for `_swarm_status`, `_agent_detail`, `_attach`, and `_detach`;
  - route ordinary reserved controls through `SwarmACPServerSession.handle_reserved_control()`;
  - route `_attach` through `SwarmACPServerSession.attach(…, acknowledge=…)`;
  - write the `_attach` success response from the acknowledgment callback before attachment activation;
  - map logical `MUX_*` codes into concrete ACP protocol errors;
  - route ordinary prompt and interrupt requests to the attached agent;
  - forward agent session updates through the ACP connection;
  - close the session deterministically when inbound processing, forwarding, or the connection fails.

- [x] T027.2 [US3] Add macro-level ACP adapter integration tests in `tests/integration/acp/test_swarm_acp_server_transport.py` using the real ACP transport and production server adapter. Verify:
  - reserved request decoding and response encoding;
  - `_attach` success response is observed before retained or live agent updates;
  - ordinary prompts and interrupts reach only the attached agent;
  - switching and detaching change routing correctly;
  - logical mux failures become the expected ACP protocol errors;
  - connection shutdown leaves no forwarding tasks or subscriptions active.

## Phase 5: Conformance and Final Validation

**Purpose**: Verify that the implementation matches the approved design and that the complete repository remains healthy.

- [x] T028 Verify implementation conformance against:

  - `specs/009-swarm-acp-mux/spec.md`;
  - `specs/009-swarm-acp-mux/data-model.md`;
  - `specs/009-swarm-acp-mux/contracts/swarm-acp-mux-session.md`;
  - `specs/009-swarm-acp-mux/quickstart.md`.

  Correct implementation defects in `src/nate_ntm/runtime/swarm_acp_mux.py` and `src/nate_ntm/runtime/swarm_acp_server.py`. Change normative documents only when an implementation discovery demonstrates a genuine design defect and the design change is explicit.
- [x] T029 Run the focused Epic 009 tests:

  ```
  uv run pytest tests/unit/runtime/test_swarm_acp_mux.py -vv
  uv run pytest tests/unit/runtime/test_swarm_acp_server.py -vv
  uv run pytest tests/integration/acp/test_swarm_acp_mux_real_path.py -vv
  uv run pytest tests/integration/acp/test_reserved_swarm_controls.py -vv
  ```

  Fix all failures in the implementation or tests. Do not weaken assertions to accommodate incorrect behavior.
- [x] T030 Run the complete default test suite with:

  ```
  uv run pytest
  ```

  The default command MUST run the complete repository test suite. Fix any regressions caused by Epic 009.
- [x] T031 Update `specs/009-swarm-acp-mux/quickstart.md` only where final production module names, commands, or verified test paths differ from the approved document. Do not add speculative files or duplicate implementation guidance.

------------------------------------------------------------------------

## Dependencies and Execution Order

### Phase Dependencies

- **Phase 1** has no feature-local prerequisites.
- **US1** depends on Phase 1 and delivers the first complete, externally testable mux path.
- **US2** depends on the production adapter and mux lifecycle delivered by US1.
- **US3** depends on US1 and the adapter dispatch delivered by US2.
- **Final validation** depends on all implemented user stories.

### Within US1

```
T001–T003
    ↓
T004–T007 tests
    ↓
T008–T013 mux implementation
    ↓
T014 production adapter MVP
    ↓
T015 real-path MVP validation
```

Tests may be written before implementation, but tasks that depend on production behavior are not marked `[P]`.

### Parallel Opportunities

- T016 may run in parallel with T017 because they modify separate test files after US1 is complete.
- T022 may run in parallel with early work on T023 after US2 is complete.
- Documentation correction in T031 begins only after final module names and test paths are stable.

------------------------------------------------------------------------

## Implementation Strategy

### MVP

Complete T001–T015.

The MVP is complete only when the real production path demonstrates:

- one mux per external session;
- prepare → acknowledgment → activate ordering;
- token- and flag-aware rollback;
- retained replay before live delivery;
- prompt and interrupt routing;
- idempotent detach;
- independent subscribers remaining active;
- no test-only adapter implementation.

### Full Feature

Complete T016–T027 to add:

- reserved controls;
- runtime views;
- stable error mapping;
- connection lifetime management;
- lifecycle serialization;
- switching and race handling;
- macro failure propagation.

### Completion

Complete T028–T031 and require both focused Epic 009 tests and the full default test suite to pass.

------------------------------------------------------------------------

## Notes

- Prefer one complete implementation over parallel test and production variants.
- Do not create a second ACP adapter under `tests/`.
- Do not create empty source or test skeletons.
- Do not add speculative documentation files.
- Keep lifecycle tests broad enough to prove complete state transitions rather than testing private helpers in isolation.
- Use fake dependencies for focused mux unit tests and the real Epic 008 stream plus production adapter for macro integration tests.
