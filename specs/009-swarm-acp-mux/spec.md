# SwarmACPMux

## 1. Purpose

`SwarmACPMux` is the connection-scoped routing layer between one external swarm ACP session and the agents managed by the runtime.

For each external swarm ACP session, the mux:

- exposes swarm-level control operations such as `_attach`, `_detach`, `_swarm_status`, and `_agent_detail`;
- attaches the external session to at most one agent at a time;
- consumes that agent's typed ACP updates through `subscribe_acp_updates()`;
- forwards the underlying `SessionUpdate` objects to the external ACP connection;
- routes ordinary prompt and interrupt requests to the attached agent.

The mux may switch attachments over its lifetime, thereby multiplexing multiple independent agent ACP sessions into one external swarm-facing session.

`SwarmACPMux` is intentionally small. It does not implement ACP transport, retained history, replay, subscriber queues, overflow behavior, or per-agent update ordering.

Those responsibilities belong to Epic 008.

------------------------------------------------------------------------

## 2. Ownership

```
RuntimeDaemon
    owns durable swarm membership, agent metadata, and swarm-level status

NateOhaAcpClient / AcpAgentSession
    own individual ACP agent sessions

AcpSessionUpdateStream
    owns typed SessionUpdate publication, replay, ordering, overflow,
    subscriber management, and closure semantics

SwarmACPMux
    owns one external session's attachment and routing state

Swarm ACP server adapter
    owns ACP protocol handling, reserved-control dispatch,
    response encoding, and external connection lifetime
```

One `SwarmACPMux` instance exists per external swarm ACP session.

The mux must not be shared between external sessions.

------------------------------------------------------------------------

## 3. Epic 008 dependency

Epic 009 assumes Epic 008 provides:

- `SessionUpdate`;
- `ReceivedSessionUpdate`;
- `AcpSessionUpdateStream`;
- one update stream owned by each concrete `AcpAgentSession`;
- `subscribe_acp_updates(agent_id)`;
- typed publication of ACP callbacks into the owning session stream.

The canonical internal update path is:

```
ACP SDK callback
    ↓
NateOhaAcpClient
    ↓
AcpAgentSession.update_stream
    ↓
subscribe_acp_updates()
    ↓
SwarmACPMux
    ↓
external ACP connection
```

`SwarmACPMux` must consume ACP updates only through `subscribe_acp_updates()`.

It must not introduce:

- another ACP subscriber registry;
- another replay buffer;
- another queue or overflow policy;
- another ACP subscription API;
- another internal update representation.

The existing `AgentEvent` pipeline may continue to receive projections of ACP activity for logging or observability. It is not an input to the mux.

The mux does not emit `AgentEvent` telemetry; any such projections remain the responsibility of the runtime daemon or surrounding observability pipeline.


------------------------------------------------------------------------

## 4. Location

The implementation should live at:

```
src/nate_ntm/runtime/swarm_acp_mux.py
```

------------------------------------------------------------------------

## 5. Interfaces

### 5.1 SwarmAgentClient

The mux depends only on the agent operations it uses:

```
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Protocol

class SwarmAgentClient(Protocol):
    def subscribe_acp_updates(
        self,
        agent_id: str,
    ) -> AbstractAsyncContextManager[
        AsyncIterator[ReceivedSessionUpdate]
    ]:
        “""Yield retained updates followed by live updates.”""

    async def prompt(
        self,
        agent_id: str,
        prompt: str,
    ) -> str | None:
        …

    async def interrupt(self, agent_id: str) -> None:
        …
```

`NateOhaAcpClient` implements this protocol.

### 5.2 ExternalACPConnection

```
class ExternalACPConnection(Protocol):
    async def session_update(
        self,
        *,
        session_id: str,
        update: SessionUpdate,
    ) -> None:
        …
```

The mux forwards `ReceivedSessionUpdate.update` unchanged.

The runtime-only metadata on `ReceivedSessionUpdate`, including `sequence` and `received_at`, is not forwarded over ACP.

### 5.3 RuntimeDaemon

The daemon remains authoritative for swarm and agent state.

The mux depends on reusable daemon-level queries such as:

```
def get_swarm_status(self) -> dict[str, object]:
    …

def get_agent_detail(
    self,
    agent_id: str,
    *,
    max_events: int = 100,
) -> dict[str, object]:
    …
```

------------------------------------------------------------------------

## 6. Connection-local state

The mux owns only state associated with one external connection:

```
@dataclass(slots=True)
class SwarmACPMux:
    daemon: RuntimeDaemon
    agent_client: SwarmAgentClient
    external_connection: ExternalACPConnection
    external_session_id: str

    attached_agent_id: str | None = None

    _attachment: _Attachment | None = None
    _lifecycle_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
    )
    _failure: asyncio.Future[None] = field(
        init=False,
        repr=False,
    )
    _closed: bool = field(
        default=False,
        init=False,
        repr=False,
    )
```

In `__post_init__`, `_failure` is initialized as a pending `asyncio.Future[None]` that is completed exactly once by `_report_failure()` on a fatal forwarding error or cancelled by `close()`. The `wait_failed()` method awaits this future.

An internal attachment record retains the concrete subscription:

```
@dataclass(slots=True)
class _Attachment:
    agent_id: str
    subscription: AbstractAsyncContextManager[
        AsyncIterator[ReceivedSessionUpdate]
    ]
    updates: AsyncIterator[ReceivedSessionUpdate]
    forwarding_enabled: asyncio.Event
    task: asyncio.Task[None] | None = None
```

Retaining the entered subscription is important. A successful attachment must refer to one concrete ACP session and must not automatically follow a replacement session.

------------------------------------------------------------------------

## 7. Attachment transaction

Attachment is a transaction with three distinct stages:

```

1. Establish the internal ACP subscription.
2. Send the external attachment acknowledgment.
3. Begin forwarding retained and live updates.

```

This ordering is required.

A successful `_attach` acknowledgment must never be sent before the mux has successfully entered `subscribe_acp_updates()`.

No retained or live update from the newly attached agent may be forwarded before the attachment acknowledgment has been written.

The visible ordering is therefore:

```
previous attachment output
_attach request
_attach acknowledgment
new attachment retained replay
new attachment live output
```

When switching agents, the old forwarding task must be completely stopped before the new attachment is established.

No old-agent update may be forwarded after the new-agent acknowledgment.

------------------------------------------------------------------------

## 8. Public API

### 8.1 prepare_attach

```
async def prepare_attach(self, agent_id: str) -> PreparedAttachment:
    …
```

`prepare_attach()` establishes the internal side of an attachment but does not begin forwarding.

Behavior:

1. verify that the mux is open;
2. validate the agent against durable swarm membership;
3. serialize the lifecycle transition with `_lifecycle_lock`;
4. return the existing attachment when the same healthy agent is already attached;
5. completely detach the previous attachment;
6. call `subscribe_acp_updates(agent_id)`;
7. enter the returned async context manager;
8. retain the concrete subscription and iterator;
9. record the new attachment;
10. return a handle representing the prepared attachment.

If subscription establishment fails, the method raises and leaves the mux unattached.

In particular, `AgentSessionNotActive` must propagate as an attachment failure. The outer adapter must not send a success acknowledgment in that case.

A representative handle is:

```
@dataclass(frozen=True, slots=True)
class PreparedAttachment:
    agent_id: str
    token: object
```

The token prevents a stale acknowledgment from activating an attachment that has already been replaced or detached.

### 8.2 activate_attachment

```
async def activate_attachment(
    self,
    prepared: PreparedAttachment,
) -> None:
    …
```

The outer server adapter calls this only after successfully writing the `_attach` acknowledgment.

Behavior:

1. verify that the mux remains open;
2. verify that `prepared` still identifies the current attachment;
3. start the forwarding task;
4. release its forwarding gate.

Calling this method with a stale or replaced handle must fail without activating anything.

The normal adapter flow is:

```
prepared = await mux.prepare_attach(agent_id)

await external_connection.send_attach_acknowledgment(
    session_id=external_session_id,
    agent_id=agent_id,
)

await mux.activate_attachment(prepared)
```

This split makes acknowledgment-before-replay an implementable guarantee rather than a scheduling assumption.

### 8.3 attach

A convenience `attach()` method may exist only when its caller supplies the acknowledgment operation:

```
async def attach(
    self,
    agent_id: str,
    *,
    acknowledge: Callable[[str], Awaitable[None]],
) -> None:
    prepared = await self.prepare_attach(agent_id)
    await acknowledge(agent_id)
    await self.activate_attachment(prepared)
```

There must not be a convenience method that launches forwarding before acknowledgment.

### 8.4 detach

```
async def detach(self) -> None:
    …
```

Behavior:

1. serialize the lifecycle transition;
2. clear the active attachment;
3. cancel and await the forwarding task;
4. exit the retained subscription context manager;
5. leave the underlying agent process and ACP session running.

`detach()` is idempotent.

Detaching the mux must remove only the mux's subscription. Other subscribers to the same `AcpSessionUpdateStream` remain active.

### 8.5 prompt

```
async def prompt(self, text: str) -> str | None:
    …
```

Behavior:

- verify that the mux is open;
- require an active attachment;
- delegate to `agent_client.prompt(attached_agent_id, text)`.

Calling `prompt()` without an attachment raises `NoAttachedAgentError`.

### 8.6 interrupt

```
async def interrupt(self) -> None:
    …
```

Behavior:

- verify that the mux is open;
- require an active attachment;
- delegate to `agent_client.interrupt(attached_agent_id)`.

Calling `interrupt()` without an attachment raises `NoAttachedAgentError`.

### 8.7 get_swarm_status

```
def get_swarm_status(self) -> dict[str, object]:
    …
```

Returns daemon-owned swarm status together with connection-local attachment state:

```
{
    “attached_agent_id”: self.attached_agent_id,
    “swarm”: self.daemon.get_swarm_status(),
}
```

### 8.8 get_agent_detail

```
def get_agent_detail(
    self,
    agent_id: str,
    *,
    max_events: int = 100,
) -> dict[str, object]:
    …
```

Returns daemon-owned agent detail together with whether that agent is attached to this mux:

```
{
    “attached”: agent_id == self.attached_agent_id,
    “agent”: self.daemon.get_agent_detail(
        agent_id=agent_id,
        max_events=max_events,
    ),
}
```

### 8.9 wait_failed

```
async def wait_failed(self) -> None:
    …
```

Waits until the mux encounters a fatal forwarding failure.

This gives the outer connection handler an explicit way to observe errors from the forwarding task.

A detached background task exception must not be treated as propagation.

### 8.10 close

```
async def close(self) -> None:
    …
```

Behavior:

1. become closed exactly once;
2. detach the current attachment;
3. resolve or cancel internal waiters;
4. reject subsequent operations with `SwarmACPMuxClosedError`.

`close()` is idempotent.

The mux should support async context-manager use.

------------------------------------------------------------------------

## 9. Forwarding task

The forwarding task consumes the iterator that was already established by `prepare_attach()`.

It must not call `subscribe_acp_updates()` itself.

Conceptually:

```
async def _run_forwarding(
    self,
    attachment: _Attachment,
) -> None:
    try:
        await attachment.forwarding_enabled.wait()

        async for received in attachment.updates:
            await self.external_connection.session_update(
                session_id=self.external_session_id,
                update=received.update,
            )

    except asyncio.CancelledError:
        raise

    except Exception as exc:
        self._report_failure(exc)
        raise

    finally:
        await self._attachment_finished(attachment)
```

When the underlying ACP stream closes normally:

- the forwarding task terminates;
- the mux remains open;
- the mux becomes unattached;
- the external session may later attach to another agent.

When forwarding to the external connection fails:

- the failure is recorded;
- `wait_failed()` raises or completes exceptionally;
- the outer connection handler closes the mux and external transport.

------------------------------------------------------------------------

## 10. Structured connection lifetime

The outer swarm ACP server adapter owns the external connection lifetime.

It must run inbound request processing and mux failure monitoring under structured concurrency.

Conceptually:

```
async with SwarmACPMux(…) as mux:
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(
            serve_external_requests(mux),
            name="swarm-acp-inbound”,
        )
        tasks.create_task(
            mux.wait_failed(),
            name="swarm-acp-forwarding-watch”,
        )
```

If inbound processing fails, forwarding is cancelled.

If outbound forwarding fails, inbound processing is cancelled.

The adapter then closes the mux and external connection.

This is the required meaning of “forwarding failures propagate to the outer connection handler.”

------------------------------------------------------------------------

## 11. Lifecycle serialization

`prepare_attach()`, `activate_attachment()`, `detach()`, and `close()` mutate shared connection-local state.

These transitions must be serialized by `_lifecycle_lock`.

The implementation must behave deterministically when:

- two attachments are requested concurrently;
- detach races with attachment;
- close races with attachment;
- an old forwarding task finishes after a new attachment has been prepared.

Completion of an old task must never clear or mutate a newer attachment.

Identity must be checked using the concrete `_Attachment` object or its unique token, not only `agent_id`.

------------------------------------------------------------------------

## 12. Reserved swarm-control protocol

Reserved swarm controls are incoming custom ACP updates whose protocol-level name begins with `_`.

Initial operations:

|                 |                                           |
|-----------------|-------------------------------------------|
| Reserved update | Mux operation                             |
| `_attach`       | Prepare attachment, acknowledge, activate |
| `_detach`       | `await mux.detach()`                      |
| `_swarm_status` | `mux.get_swarm_status()`                  |
| `_agent_detail` | `mux.get_agent_detail(agent_id)`          |

The outer swarm ACP server adapter owns:

- parsing reserved update payloads;
- validating required arguments;
- dispatching to mux methods;
- writing success acknowledgments;
- translating mux domain errors into ACP errors.

The swarm ACP server adapter raises `UnsupportedReservedUpdateError` for unknown underscore-prefixed control operations.

Reserved client-to-swarm controls must never be forwarded to an attached agent.

An underscore-prefixed update emitted by an attached agent is ordinary agent output. It follows the typed ACP stream and is forwarded unchanged unless a later protocol specification defines an explicit filtering rule.

------------------------------------------------------------------------

## 13. Error model

```
class SwarmACPMuxError(RuntimeError):
    pass

class SwarmACPMuxClosedError(SwarmACPMuxError):
    pass

class UnknownAgentError(SwarmACPMuxError):
    pass

class NoAttachedAgentError(SwarmACPMuxError):
    pass

class StaleAttachmentError(SwarmACPMuxError):
    pass

class UnsupportedReservedUpdateError(SwarmACPMuxError):
    pass
```

Primary use sites:

- `SwarmACPMuxClosedError`: raised by public methods when `_closed` is `True`.
- `UnknownAgentError`: raised by `prepare_attach()` and `get_agent_detail()` when the daemon has no such agent.
- `NoAttachedAgentError`: raised by `prompt()` and `interrupt()` when there is no active attachment.
- `StaleAttachmentError`: raised by `activate_attachment()` when a `PreparedAttachment` token no longer matches the current `_Attachment`.
- `UnsupportedReservedUpdateError`: raised by the swarm ACP server adapter when it receives an unknown reserved control operation.

Errors from `subscribe_acp_updates()`, including `AgentSessionNotActive`, propagate through `prepare_attach()`.

The outer adapter translates domain errors into protocol-level responses.

External connection write errors are fatal to the external swarm ACP session.

-----------------------------------------------------------------------
## 14. Internal lifecycle methods

### `_run_forwarding`

Waits for attachment activation, forwards retained and live typed ACP updates,
reports fatal failures, and performs identity-safe attachment cleanup.

Cancellation caused by `detach()` or `close()` is normal lifecycle behavior and
MUST NOT be reported through `wait_failed()`.

### `_attachment_finished`

Under `_lifecycle_lock`:

1. verifies that the completed attachment is still current;
2. clears `attached_agent_id` and `_attachment`;
3. exits the retained subscription context manager exactly once.

Completion of an obsolete attachment MUST NOT modify a newer attachment.

### `_report_failure`

Records the first fatal forwarding failure only.

A fatal failure includes:

- an exception raised while consuming the ACP subscription;
- an exception writing an update to the external ACP connection.

Normal subscription exhaustion and task cancellation are not failures.

### `wait_failed`

Waits for the first fatal forwarding failure and re-raises it.

Clean mux closure cancels the pending failure waiter. Normal agent-stream
closure leaves the mux open and unattached and does not complete the failure
waiter.

## 15. Required invariants

The implementation must preserve these invariants:

1. One mux exists per external ACP session.
2. A mux is attached to at most one agent.
3. A successful attachment refers to one concrete agent ACP session.
4. A mux never follows automatically to a replacement ACP session.
5. The internal subscription exists before attachment success is acknowledged.
6. New-agent replay begins only after attachment acknowledgment.
7. The old forwarding task has stopped before a new attachment is acknowledged.
8. Per-agent ordering is preserved exactly as yielded by Epic 008.
9. The mux forwards the underlying typed `SessionUpdate` unchanged.
10. Detaching the mux does not stop the agent.
11. Detaching the mux does not affect independent subscribers.
12. Forwarding failures are observed by the outer connection handler.
13. Lifecycle transitions are serialized.
14. No second ACP buffer or subscription system exists.

------------------------------------------------------------------------

## 16. Tests

Tests should focus on complete lifecycle and routing behavior rather than isolated implementation details.

### 16.1 Attachment establishment

- a known agent with an active ACP session can be prepared;
- a durable agent without an active ACP session fails attachment;
- no success acknowledgment is sent after subscription establishment fails;
- the mux remains unattached after a failed preparation;
- preparing an attachment enters exactly one ACP subscription.

### 16.2 Acknowledgment ordering

- no retained update is forwarded before the `_attach` acknowledgment;
- retained replay begins after successful activation;
- live updates follow retained replay;
- an update published during preparation is delivered exactly once;
- acknowledgment failure prevents activation and cleans up the prepared subscription.

### 16.3 Switching agents

- switching completely stops the old forwarding task;
- the old subscription is exited before the new attachment is acknowledged;
- no old-agent update appears after the new-agent acknowledgment;
- the new agent's retained updates replay before its live updates;
- completion of the old task cannot clear the new attachment.

### 16.4 Request forwarding

- `prompt()` delegates to the attached agent;
- `interrupt()` delegates to the attached agent;
- both raise `NoAttachedAgentError` without an attachment.

### 16.5 Multiple subscribers

Using the real `AcpSessionUpdateStream`:

- the mux and an independent subscriber receive the same update;
- both receive retained history followed by live updates;
- detaching the mux leaves the independent subscriber active.

### 16.6 Lifecycle and concurrency

- `detach()` is idempotent;
- `close()` is idempotent;
- normal agent-stream closure leaves the mux open and unattached;
- operations after close raise `SwarmACPMuxClosedError`;
- simultaneous attach requests produce one deterministic final attachment;
- detach racing with attach leaves valid state;
- close racing with attach leaves the mux closed and unattached.

### 16.7 Failure propagation

- an external write failure terminates forwarding;
- `wait_failed()` exposes that failure;
- the outer connection task group cancels inbound processing;
- the mux and external transport are closed.

### 16.8 Reserved controls

- `_attach` is not forwarded to the agent;
- `_detach` is not forwarded to the agent;
- `_swarm_status` returns daemon-owned status;
- `_agent_detail` returns daemon-owned detail;
- unknown reserved operations return a structured error;
- underscore-prefixed agent output is forwarded normally.

### 16.9 Macro integration test

Add one real-path asynchronous integration test that:

1. starts a real agent;
2. creates an external swarm ACP session and mux;
3. sends `_attach`;
4. confirms that the internal subscription is established before acknowledgment;
5. confirms that retained output begins only after acknowledgment;
6. sends an ordinary prompt;
7. verifies that typed agent output reaches the external connection;
8. verifies that an independent subscriber receives the same output;
9. switches to another agent and verifies the ordering boundary;
10. sends `_detach`;
11. verifies that the mux becomes unattached while both agents remain runtime-managed.

------------------------------------------------------------------------

## 17. Non-goals

Epic 009 does not implement:

- ACP transport framing;
- `AcpSessionUpdateStream`;
- replay-buffer policy;
- subscriber overflow policy;
- generic `AgentEvent` removal;
- swarm-wide persistence of external attachment state;
- automatic attachment migration when an agent session is replaced;
- multiple simultaneous agent attachments for one external session.

------------------------------------------------------------------------

## 18. Summary

`SwarmACPMux` is a connection-scoped router over the typed ACP update infrastructure from Epic 008.

Its central lifecycle is:

```
prepare internal subscription
    ↓
send external attachment acknowledgment
    ↓
activate retained replay and live forwarding
```

Its output path is:

```
AcpAgentSession
    ↓
subscribe_acp_updates()
    ↓
SwarmACPMux
    ↓
ExternalACPConnection.session_update()
```

Its control path is:

```
external reserved control
    ↓
swarm ACP server adapter
    ↓
SwarmACPMux
    ↓
RuntimeDaemon or attached agent
```

This leaves one authoritative implementation for typed ACP delivery, one canonical subscription API, one mux implementation, and one clear owner for each lifecycle boundary.
