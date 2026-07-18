# Bonus 1

Continue by replacing naïve ACP event observation with a proper event-driven subscription API.

The goal is to eliminate patterns such as:

```
events: list[AgentEvent] = []
client.on_event = events.append

await client.prompt(…)
await asyncio.sleep(0.5)

assert …
```

and any polling loops that repeatedly inspect event lists.

## 1. Introduce an explicit subscription abstraction

Add an async context manager such as:

```
async with client.subscribe_events(agent_id) as events:
    …
```

Entering the context must register the subscriber queue immediately, before control returns to the caller. This avoids the subtle race inherent in creating an async generator and assuming it has already begun executing.

Conceptually:

```
@asynccontextmanager
async def subscribe_events(
    self,
    agent_id: str,
) -> AsyncIterator[AsyncIterator[AgentEvent]]:
    queue = self._register_event_subscriber(agent_id)

    async def iterator() -> AsyncIterator[AgentEvent]:
        while True:
            event = await queue.get()
            yield event

    try:
        yield iterator()
    finally:
        self._unregister_event_subscriber(agent_id, queue)
```

`iter_events()` may remain as a convenience wrapper, but tests and code that must not miss early events should prefer `subscribe_events()`.

## 2. Build matching waits on top of a subscription

Provide a helper that consumes an existing subscription:

```
async def next_matching_event(
    events: AsyncIterator[AgentEvent],
    predicate: Callable[[AgentEvent], bool],
    *,
    timeout: float,
) -> AgentEvent:
    async def wait() -> AgentEvent:
        async for event in events:
            if predicate(event):
                return event

        raise RuntimeError(“ACP event stream closed before an event matched”)

    return await asyncio.wait_for(wait(), timeout=timeout)
```

Do not make every call to `wait_for_event()` silently create a late subscription after the operation has already begun.

A convenience API may instead accept an operation to run after subscription, but the basic ordering must remain explicit:

```
async with client.subscribe_events(agent_id) as events:
    await client.prompt(agent_id, prompt)

    event = await next_matching_event(
        events,
        lambda event: event_contains_text(event, prompt),
        timeout=5.0,
    )
```

## 3. Rewrite the Epic 005 replay test without sleeps or shared event lists

For the first prompt:

```
async with acp_client.subscribe_events(agent_id) as events:
    await acp_client.prompt(agent_id, prompt_text1)

    echo1 = await next_matching_event(
        events,
        lambda event: event_contains_text(event, prompt_text1),
        timeout=5.0,
    )
```

For resume, subscribe **before** starting the resumed session so the replay cannot race ahead of the listener:

```
async with fresh_client.subscribe_events(agent_id) as replay_events:
    await fresh_client.start_agent_async(
        agent_id,
        metadata=resume_meta,
    )

    replayed_echo = await next_matching_event(
        replay_events,
        lambda event: event_contains_text(event, canonical_echo1),
        timeout=5.0,
    )

    await fresh_client.prompt(agent_id, prompt_text2)

    echo2 = await next_matching_event(
        replay_events,
        lambda event: event_contains_text(event, prompt_text2),
        timeout=5.0,
    )
```

This should replace:

```
await asyncio.sleep(0.5)
```

and remove the need to inspect a separately populated `events_run1` or `events_run2` list merely to determine whether an expected event has arrived.

Collect events only when the test genuinely needs the complete sequence:

```
observed: list[AgentEvent] = []

async for event in events:
    observed.append(event)
    …
```

Do not use a list as the synchronization mechanism.

## 4. Search for other naïve event-waiting patterns

Search the repository for:

```
on_event =
events.append
asyncio.sleep(
while … events
iter_events(
wait_for_event(
```

Review each result semantically.

Replace sleeps only when they are being used to wait for an event or state change. Do not remove unrelated sleeps that model deliberate timing behavior.

The `RuntimeClient.iter_events()` used by the TUI is a separate WebSocket-level event stream and should not be rewritten merely because it shares the same method name. Preserve that API unless there is a concrete defect.

## 5. Keep one central broadcast path

All ACP and process events should continue through:

```
NateOhaAcpClient._emit_event(event)
```

That method should:

1. broadcast to every active subscription;
2. forward to the runtime's durable in-process projection through `AgentSupervisor`;
3. avoid allowing one subscriber to consume events intended for another.

Do not use a single queue shared by multiple consumers. Each subscriber needs its own queue.

## 6. Clarify the role of on_event

Keep `on_event` temporarily only as the internal bridge to:

```
AgentSupervisor.append_agent_event
```

New tests and new consumers should not assign arbitrary callbacks to `on_event`.

Once all production wiring uses a clearer event-sink or subscription abstraction, either:

- make the callback private and constructor-injected, or
- replace it with a dedicated runtime event sink.

Do not preserve `on_event` as a general public observation API indefinitely.

## 7. Add lifecycle semantics

A subscriber should not wait forever after an agent stops.

When an ACP session closes or fails, terminate its subscriptions using a private sentinel or an explicit stream-close mechanism. Consumers should then receive a meaningful exception or normal iterator completion.

Ensure subscription cleanup occurs when:

- a matching event is returned;
- a timeout occurs;
- the caller cancels;
- the caller exits the context;
- the agent stops or fails.

## 8. Avoid unbounded production queues

The current unbounded queues are acceptable as an initial implementation but should not become an unnoticed production memory risk.

Use a bounded queue or document and implement an explicit overflow policy. Prefer preserving recent events and logging overflow rather than silently swallowing `QueueFull`.

This does not need to become an elaborate backpressure subsystem during Epic 005, but queue behavior must be deliberate.

## Acceptance criteria

This refactor is complete when:

- the Epic 005 prompt/replay test contains no event-related `asyncio.sleep`;
- the replay subscription is registered before `start_agent_async`;
- prompt subscriptions are registered before `prompt`;
- no test uses `on_event = list.append` as its event synchronization mechanism;
- multiple subscribers receive the same emitted event independently;
- timeout and cancellation remove subscriptions;
- stopping an agent terminates its active event streams;
	- the real nate-oha integration tests continue to pass with:


```
uv run pytest tests/integration/runtime_acp/test_runtime_daemon_acp_async_real_path_epic005.py
```

Also investigate the existing teardown warning:

```
aclose(): asynchronous generator is already running
```

Do not ignore it merely because the tests pass. It may indicate that an ACP transport or subscription generator is being closed concurrently, and the new explicit subscription lifecycle should make ownership easier to reason about.

    

# Event Stream Refactor Checklist

## Phase 1 — Subscription Infrastructure

-
  Introduce a first-class async subscription API (e.g. `subscribe_events(agent_id)`).
-
  Ensure entering the subscription registers the subscriber immediately (before returning control).
-
  Keep `iter_events()` as a convenience wrapper if desired, but make the subscription API the canonical interface.
-
  Ensure each subscriber gets its own queue (broadcast semantics, not work-queue semantics).
-
  Keep `_emit_event()` as the single event emission path.

## Phase 2 — Event Waiting Helpers

-
  Implement a helper such as `next_matching_event(…)` that consumes an existing subscription.
-
  Ensure helpers support bounded timeouts via `asyncio.wait_for()`.
-
  Avoid creating late subscriptions inside helper methods that may race with event production.
-
  Verify helpers clean up subscriptions on success, timeout, cancellation, and exceptions.

## Phase 3 — Refactor Epic 005 Tests

### Prompt flow

-
  Replace `asyncio.sleep()` after `prompt()` with an event-driven wait.
-
  Subscribe before issuing `prompt()`.
-
  Wait for the expected echo event instead of sleeping.

### Resume flow

-
  Subscribe before calling `start_agent_async()` on the resumed client.
-
  Wait for replayed conversation history through the event stream.
-
  Remove replay-related sleeps.
-
  Wait for the second prompt response using the event stream.

### General cleanup

-
  Remove `on_event = events.append` as the synchronization mechanism.
-
  Only accumulate events into lists when validating event history or ordering.

## Phase 4 — Repository-wide Migration

Search for and review usages of:

-
  `on_event =`
-
  `events.append`
-
  `asyncio.sleep(`
-
  polling loops over event lists
-
  `wait_for_event(`
-
  `iter_events(`

For each result:

-
  Replace sleep-based synchronization with event-driven synchronization where appropriate.
-
  Leave unrelated timing sleeps unchanged.
-
  Do **not** rewrite the runtime WebSocket client (`RuntimeClient.iter_events`) simply because it shares the same method name.

## Phase 5 — Event Architecture

-
  Ensure every ACP and lifecycle event flows through `_emit_event()`.
-
  Preserve compatibility with the existing runtime event pipeline (`AgentSupervisor`) during the migration.
-
  Avoid multiple independent event emission paths.

## Phase 6 — on_event Migration

-
  Treat `on_event` as an internal compatibility mechanism only.
-
  Migrate new code to the async subscription API.
-
  Plan removal or encapsulation of `on_event` once no longer required by production code.

## Phase 7 — Stream Lifecycle

-
  Define how event streams terminate.
-
  Ensure subscribers exit cleanly when an agent stops.
-
  Ensure subscribers exit cleanly on agent failure.
-
  Ensure cancellation removes subscriptions.
-
  Ensure timeout removes subscriptions.
-
  Ensure exiting the subscription context unregisters subscribers.

## Phase 8 — Queue Behavior

-
  Decide on a deliberate queue policy (bounded vs. unbounded).
-
  If bounded, define overflow behavior.
-
  Avoid silent event loss.
-
  Document the intended semantics.

## Phase 9 — Validation

-
  Epic 005 replay test contains no event-related `asyncio.sleep()`.
-
  Replay listener subscribes before `start_agent_async()`.
-
  Prompt listener subscribes before `prompt()`.
-
  Multiple simultaneous subscribers each receive the same emitted event.
-
  Timeouts and cancellations clean up correctly.
-
  Stopping an agent terminates active event streams.
-
  Real integration tests continue to pass.

## Phase 10 — Cleanup

-
  Investigate the `aclose(): asynchronous generator is already running` warning.
-
  Verify it is resolved rather than merely ignored.
-
  Remove obsolete helper code made unnecessary by the new event-stream architecture.

### Current Status (2026-07-14)

- [x] Phase 1 
  — Subscription infrastructure implemented in `NateOhaAcpClient` (`subscribe_events`, per-subscriber queues, `_emit_event` as single emission path).
- [x] Phase 2 
  — `next_matching_event(...)` helper implemented in `nate_ntm.runtime.acp_client` and used by Epic 005 async tests.
- [x] Phase 3 
  — Epic 005 REAL-path async ACP tests refactored to use `subscribe_events` + `next_matching_event` with no event-related `asyncio.sleep`.
- [ ] Phase 4 
  — Repository-wide migration of other event-waiting patterns still pending; some usages have been reviewed but not all are migrated yet.
- [x] Phase 5 
  — All ACP events continue to flow through `_emit_event`, which broadcasts to subscribers and forwards to `AgentSupervisor`.
- [ ] Phase 6 
  — `on_event` is treated as an internal bridge (daemon → `AgentSupervisor`), but full encapsulation/removal is not yet complete.
- [x] Phase 7 
  — Stream lifecycle semantics implemented (`_close_event_subscribers`, sentinel-based termination, and tests for timeout/cancellation/agent stop).
- [x] Phase 8 
  — Bounded per-subscriber queues with a deliberate overflow policy (drop-oldest + warnings) are in place.
- [x] Phase 9 
  — Validation criteria satisfied for Epic 005: prompt/replay tests are event-driven, multiple subscribers see the same events, and real ACP integration tests are passing.
- [x] Phase 10 
  — `aclose(): asynchronous generator is already running` warning investigated; current `RuntimeSession.disconnect()` scheme (cancel tasks before `aclose()`) shows no such warnings under `-W error::RuntimeWarning`. Obsolete test-local helpers have been removed where they were superseded by the shared event-stream API.

