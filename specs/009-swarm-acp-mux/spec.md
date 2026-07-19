# SwarmACPMux

## Purpose

`SwarmACPMux` is the connection-scoped routing layer between an external ACP client and a running swarm.

It presents one external ACP session while allowing the client to:

- issue swarm-level control operations;
- attach to one agent at a time;
- send ordinary ACP requests to the attached agent;
- receive the attached agent's retained and live ACP updates.

Ownership is divided as follows:

```
RuntimeDaemon
    owns swarm state and swarm-level status

NateOhaAcpClient
    owns per-agent ACP sessions and request forwarding

Per-agent event stream
    owns retained history and subscriber delivery

SwarmACPMux
    owns external-session attachment and routing state

Swarm ACP server adapter
    owns ACP protocol decoding and reserved control dispatch
```

`SwarmACPMux` remains deliberately small. It coordinates existing runtime services rather than introducing another runtime, event registry, or agent lifecycle manager.

------------------------------------------------------------------------

## Location

The class should live in:

```
src/nate_ntm/runtime/swarm_acp_mux.py
```

This places it beside the runtime components it coordinates:

```
src/nate_ntm/runtime/
├── acp_client.py
├── acp_protocol_client.py
├── agents.py
├── daemon.py
├── events.py
├── state.py
├── swarm_acp_mux.py
└── swarm_state.py
```

`NateOhaAcpClient` remains responsible for ACP connections to individual agents. `SwarmACPMux` represents the external connection into the swarm as a whole.

------------------------------------------------------------------------

## Architecture

The mux participates in two distinct paths.

### External control and request path

```
Customized external ACP client

        |
        | ACP requests and reserved sessionUpdate controls
        v
Swarm ACP server adapter

        |
        | explicit SwarmACPMux method calls
        v
SwarmACPMux

        |
        | ordinary attached-agent requests
        v
NateOhaAcpClient

        |
        v
nate-oha ACP process
```

The customized external client may be an `agent-shell` implementation that understands swarm-specific reserved updates.

Examples include:

```
_attach
_detach
_swarm_status
_agent_detail
```

These are incoming client-to-swarm control operations.

### Agent event path

```
nate-oha ACP process

        |
        v
NateNtmAcpProtocolClient

        |
        v
NateOhaAcpClient event publication

        |
        v
Per-agent replay-capable event stream

        |
        | one independent subscription
        v
SwarmACPMux

        |
        v
External ACP connection
```

The mux subscribes to one agent at a time and forwards that agent's ACP updates to the external client.

------------------------------------------------------------------------

## Connection scope

One `SwarmACPMux` instance should be created for each external ACP session.

Its state is connection-local:

```
@dataclass(slots=True)
class SwarmACPMux:
    daemon: RuntimeDaemon
    agent_client: SwarmAgentClient
    external_connection: ExternalACPConnection
    external_session_id: str

    attached_agent_id: str | None = None
    _forwarding_task: asyncio.Task[None] | None = None
    _closed: bool = False
```

The mux owns:

- the currently attached agent ID;
- the forwarding task for that attachment;
- the external ACP session ID;
- connection-local closed/open state.

The runtime services supplied to the mux retain ownership of swarm state, agent sessions, retained history, and subscriber queues.

------------------------------------------------------------------------

## Interfaces

### SwarmAgentClient

The mux should depend on the narrow agent-facing interface it actually uses:

```
class SwarmAgentClient(Protocol):
    def subscribe_events(
        self,
        agent_id: str,
    ) -> AbstractAsyncContextManager[AsyncIterator[AgentEvent]]:
        “""Yield retained events, followed by live events.”""

    async def prompt(
        self,
        agent_id: str,
        prompt: str,
    ) -> str | None:
        …

    async def interrupt(self, agent_id: str) -> None:
        …
```

This interface can be implemented by `NateOhaAcpClient`.

The default `subscribe_events()` contract should be replay-then-live delivery. A separate live-only API may be added only if another concrete runtime use case requires it.

### ExternalACPConnection

The outbound connection should accept the ACP session-update type expected by the outer server integration:

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

`SessionUpdate` represents the ACP SDK session-update union or the project's equivalent typed abstraction.

The event-delivery layer should retain enough ACP information for the mux to forward the update without reconstructing protocol meaning from `AgentEvent.type`.

A suitable `AgentEvent` representation may include:

```
@dataclass(slots=True)
class AgentEvent:
    agent_id: str
    type: str
    payload: Mapping[str, object]
    acp_update: SessionUpdate | None = None
```

If storing the typed model directly is inappropriate for persistence, the ACP integration should expose one authoritative conversion function from the stored normalized payload to `SessionUpdate`.

------------------------------------------------------------------------

## Constructor

```
def __init__(
    self,
    *,
    daemon: RuntimeDaemon,
    agent_client: SwarmAgentClient,
    external_connection: ExternalACPConnection,
    external_session_id: str,
) -> None:
    …
```

Construction initializes connection-local state and stores the runtime services.

The initial state is:

```
attached_agent_id = None
_forwarding_task = None
_closed = False
```

Agent attachment occurs explicitly through `attach()`.

------------------------------------------------------------------------

## Public methods

### attach

```
async def attach(self, agent_id: str) -> None:
    …
```

Attaches the external ACP session to an existing swarm agent.

Behavior:

1. verify that the mux is open;
2. validate the agent against durable swarm membership;
3. return immediately when the same healthy attachment already exists;
4. detach the previous event subscription;
5. establish a replay-capable subscription to the new agent;
6. record the new attachment.

Conceptually:

```
async def attach(self, agent_id: str) -> None:
    self._ensure_open()
    self._require_known_agent(agent_id)

    if (
        self.attached_agent_id == agent_id
        and self._forwarding_task is not None
        and not self._forwarding_task.done()
    ):
        return

    await self.detach()

    self.attached_agent_id = agent_id
    self._forwarding_task = asyncio.create_task(
        self._forward_agent_events(agent_id),
        name=f"swarm-acp-mux:{agent_id}”,
    )
```

The attachment acknowledgment returned by the outer ACP server should clearly identify the attached agent. This acknowledgment forms the visible boundary between events from the previous and new attachments.

Switching agents is a supported operation.

------------------------------------------------------------------------

### detach

```
async def detach(self) -> None:
    …
```

Detaches the external ACP session from its current agent.

Behavior:

- clear the connection-local attachment;
- cancel and await the forwarding task;
- leave the agent process and ACP session running.

```
async def detach(self) -> None:
    task = self._forwarding_task

    self._forwarding_task = None
    self.attached_agent_id = None

    if task is None:
        return

    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass
```

Agent lifecycle remains under runtime ownership.

------------------------------------------------------------------------

### prompt

```
async def prompt(self, text: str) -> str | None:
    …
```

Forwards an ordinary prompt to the attached agent:

```
async def prompt(self, text: str) -> str | None:
    self._ensure_open()
    agent_id = self._require_attached_agent()
    return await self.agent_client.prompt(agent_id, text)
```

------------------------------------------------------------------------

### interrupt

```
async def interrupt(self) -> None:
    …
```

Forwards an interrupt to the attached agent:

```
async def interrupt(self) -> None:
    self._ensure_open()
    agent_id = self._require_attached_agent()
    await self.agent_client.interrupt(agent_id)
```

------------------------------------------------------------------------

### get_swarm_status

```
def get_swarm_status(self) -> dict[str, object]:
    …
```

Returns runtime-level swarm status plus connection-local attachment state.

`RuntimeDaemon` should expose a reusable status API:

```
def get_swarm_status(self) -> dict[str, object]:
    …
```

A representative daemon-level payload is:

```
{
  “swarm_id”: “swarm-123”,
  “status”: “running”,
  “agents”: [
    {
      “agent_id”: “agent-1”,
      “display_name”: “Planner”,
      “status”: “running”,
      “conversation_id”: “conversation-1”,
      “last_error”: null
    }
  ]
}
```

The mux adds only connection-local information:

```
def get_swarm_status(self) -> dict[str, object]:
    self._ensure_open()

    return {
        “attached_agent_id”: self.attached_agent_id,
        “swarm”: self.daemon.get_swarm_status(),
    }
```

------------------------------------------------------------------------

### get_agent_detail

```
def get_agent_detail(
    self,
    agent_id: str,
    *,
    max_events: int = 100,
) -> dict[str, object]:
    …
```

Returns the existing daemon-level agent detail plus mux-local attachment status:

```
def get_agent_detail(
    self,
    agent_id: str,
    *,
    max_events: int = 100,
) -> dict[str, object]:
    self._ensure_open()
    self._require_known_agent(agent_id)

    return {
        “attached”: agent_id == self.attached_agent_id,
        “agent”: self.daemon.get_agent_detail(
            agent_id=agent_id,
            max_events=max_events,
        ),
    }
```

The daemon remains the authoritative source for agent metadata and retained event history.

------------------------------------------------------------------------

### close

```
async def close(self) -> None:
    …
```

Closes the connection-scoped mux:

```
async def close(self) -> None:
    if self._closed:
        return

    self._closed = True
    await self.detach()
```

The mux should support async context-manager use:

```
async def __aenter__(self) -> “SwarmACPMux”:
    self._ensure_open()
    return self

async def __aexit__(
    self,
    exc_type,
    exc,
    traceback,
) -> None:
    await self.close()
```

------------------------------------------------------------------------

## Internal methods

### \_forward_agent_events

```
async def _forward_agent_events(self, agent_id: str) -> None:
    …
```

This coroutine implements the attached-agent output path.

The subscription yields retained history first, then live events:

```
async def _forward_agent_events(self, agent_id: str) -> None:
    current_task = asyncio.current_task()

    try:
        async with self.agent_client.subscribe_events(agent_id) as events:
            async for event in events:
                await self._forward_external_event(event)
    finally:
        if (
            self.attached_agent_id == agent_id
            and self._forwarding_task is current_task
        ):
            self.attached_agent_id = None
            self._forwarding_task = None
```

When the agent stream closes normally, the mux remains open and becomes unattached.

A subsequent reserved `_attach` operation may attach it to another agent.

An exception while writing to the external ACP connection should terminate this forwarding task and propagate to the outer connection handler. The outer handler then closes the connection-scoped mux and transport.

------------------------------------------------------------------------

### \_forward_external_event

```
async def _forward_external_event(self, event: AgentEvent) -> None:
    …
```

Forwards one typed ACP update to the external session:

```
async def _forward_external_event(self, event: AgentEvent) -> None:
    update = require_session_update(event)

    await self.external_connection.session_update(
        session_id=self.external_session_id,
        update=update,
    )
```

`require_session_update()` is the single ACP integration boundary for obtaining a typed `SessionUpdate` from an `AgentEvent`.

Its implementation should either:

- return `event.acp_update`; or
- validate the normalized stored payload into the ACP SDK type.

The mux forwards the resulting update without interpreting ordinary agent output.

------------------------------------------------------------------------

### \_require_attached_agent

```
def _require_attached_agent(self) -> str:
    if self.attached_agent_id is None:
        raise NoAttachedAgentError(
            “No agent is attached to this ACP connection"
        )

    return self.attached_agent_id
```

------------------------------------------------------------------------

### \_require_known_agent

```
def _require_known_agent(self, agent_id: str) -> AgentState:
    try:
        return self.daemon.swarm_state.agents[agent_id]
    except KeyError as exc:
        raise UnknownAgentError(agent_id) from exc
```

Durable `SwarmState.agents` is the authority for swarm membership.

------------------------------------------------------------------------

### \_ensure_open

```
def _ensure_open(self) -> None:
    if self._closed:
        raise SwarmACPMuxClosedError()
```

------------------------------------------------------------------------

## Reserved swarm-control protocol

Reserved swarm-control operations are incoming ACP updates from the customized external client whose protocol-level `sessionUpdate` name begins with `_`.

Examples:

```
_attach
_detach
_swarm_status
_agent_detail
```

The outer swarm ACP server adapter owns detection and dispatch.

Conceptually:

```
async def handle_external_update(
    mux: SwarmACPMux,
    update: SessionUpdate,
) -> None:
    name = update.session_update

    if name.startswith(“_”):
        await dispatch_reserved_update(mux, update)
        return

    await proxy_ordinary_update(mux, update)
```

A representative dispatch table is:

|                          |                                  |
|--------------------------|----------------------------------|
| Reserved `sessionUpdate` | Mux operation                    |
| `_attach`                | `await mux.attach(agent_id)`     |
| `_detach`                | `await mux.detach()`             |
| `_swarm_status`          | `mux.get_swarm_status()`         |
| `_agent_detail`          | `mux.get_agent_detail(agent_id)` |

Unknown underscore-prefixed control updates produce a structured unsupported-operation error:

```
class UnsupportedReservedUpdateError(SwarmACPMuxError):
    pass
```

The outer ACP server adapter converts domain errors into the appropriate ACP error response.

### Agent-emitted custom updates

An attached agent may also emit a custom underscore-prefixed ACP update.

Such updates travel through the ordinary per-agent publication path:

```
agent
    -> NateOhaAcpClient
    -> retained event stream
    -> all internal subscribers
```

Their visibility to the external client is defined separately by the swarm ACP protocol.

The default mux behavior is transparent forwarding unless the protocol explicitly defines an agent-output filtering rule.

This preserves one simple attached-agent forwarding path.

------------------------------------------------------------------------

## Replay-capable event delivery

Attaching to an agent should produce one continuous ordered stream containing:

1. retained per-agent events;
2. all later live events.

The mux consumes this through the normal subscription API:

```
async with self.agent_client.subscribe_events(agent_id) as events:
    async for event in events:
        …
```

The event-delivery implementation owns the replay boundary and ordering guarantees.

A correct subscription must ensure that an event published during subscription establishment is delivered exactly once.

A suitable sequence is:

1. register the subscriber;
2. capture the replay boundary;
3. enqueue retained events through that boundary;
4. continue live delivery through the same queue.

The current code may require a small upstream refactor because retained history and live subscriber queues are owned by different components.

A clean target is:

```
NateOhaAcpClient
    publishes normalized per-agent events

Replay-capable AgentEventStream
    retains bounded history
    owns subscriber queues
    provides subscribe_events(agent_id)

SwarmACPMux
    consumes subscribe_events(agent_id)
```

Alternatively, `NateOhaAcpClient.subscribe_events()` may remain the public API while delegating internally to the replay-capable event stream.

The public contract should expose one ordered replay-then-live stream.

------------------------------------------------------------------------

## Upstream changes

### AgentEventStream

Extend the per-agent event stream so it owns both:

- bounded retained history;
- independent subscriber delivery.

A possible interface is:

```
class AgentEventStream:
    def publish(self, event: AgentEvent) -> None:
        …

    @asynccontextmanager
    async def subscribe(
        self,
    ) -> AsyncIterator[AsyncIterator[AgentEvent]]:
        …
```

Each subscription receives:

```
retained history -> live events -> closure sentinel
```

This centralizes ordering and removes the need to coordinate two separate event sources.

### NateOhaAcpClient

Keep `subscribe_events(agent_id)` as the public agent-client API:

```
def subscribe_events(
    self,
    agent_id: str,
) -> AbstractAsyncContextManager[AsyncIterator[AgentEvent]]:
    return self._event_streams[agent_id].subscribe()
```

Event publication should use the same per-agent stream:

```
def _emit_event(self, event: AgentEvent) -> None:
    self._event_streams[event.agent_id].publish(event)
```

Existing non-mux subscribers continue to use the same API and receive independent queues.

### RuntimeDaemon

Add:

```
def get_swarm_status(self) -> dict[str, object]:
    …
```

This method should serialize swarm-level runtime state once for reuse by:

- `SwarmACPMux`;
- CLI/status endpoints;
- diagnostics;
- future dashboards.

### ACP event representation

Preserve a typed or reliably reconstructable `SessionUpdate` in each ACP-derived `AgentEvent`.

Preferred shape:

```
AgentEvent(
    agent_id=agent_id,
    type=f"acp.{kind}”,
    payload=serialized_update,
    acp_update=update,
)
```

If typed ACP objects cannot be retained, provide one conversion function in the ACP integration module:

```
def require_session_update(event: AgentEvent) -> SessionUpdate:
    …
```

### Swarm ACP server adapter

Add or extend the external ACP server integration so it:

- creates one `SwarmACPMux` per external session;
- detects underscore-prefixed incoming `sessionUpdate` names;
- dispatches reserved controls to mux methods;
- proxies ordinary ACP requests through the mux;
- translates mux domain errors into ACP errors;
- closes the mux when the external connection ends.

------------------------------------------------------------------------

## Error model

```
class SwarmACPMuxError(RuntimeError):
    pass

class SwarmACPMuxClosedError(SwarmACPMuxError):
    pass

class UnknownAgentError(SwarmACPMuxError):
    pass

class NoAttachedAgentError(SwarmACPMuxError):
    pass

class UnsupportedReservedUpdateError(SwarmACPMuxError):
    pass
```

The mux raises domain errors.

The outer ACP server adapter maps them to protocol responses.

External connection write failures propagate out of the forwarding task and terminate the connection-scoped mux.

------------------------------------------------------------------------

## Logging

Log mux lifecycle and failure boundaries:

- mux creation;
- attachment;
- detachment;
- normal agent-stream closure;
- unexpected forwarding-task termination;
- unsupported reserved update;
- external write failure;
- mux closure.

Useful fields include:

```
swarm_id
external_session_id
agent_id
update_name
event_id
```

Ordinary forwarded events may remain at debug or trace level.

------------------------------------------------------------------------

## Minimal class outline

```
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol

from nate_ntm.runtime.daemon import RuntimeDaemon
from nate_ntm.runtime.events import AgentEvent
from nate_ntm.runtime.state import AgentState
from nate_ntm.runtime.acp_types import SessionUpdate

class SwarmAgentClient(Protocol):
    def subscribe_events(
        self,
        agent_id: str,
    ):
        …

    async def prompt(
        self,
        agent_id: str,
        prompt: str,
    ) -> str | None:
        …

    async def interrupt(self, agent_id: str) -> None:
        …

class ExternalACPConnection(Protocol):
    async def session_update(
        self,
        *,
        session_id: str,
        update: SessionUpdate,
    ) -> None:
        …

@dataclass(slots=True)
class SwarmACPMux:
    daemon: RuntimeDaemon
    agent_client: SwarmAgentClient
    external_connection: ExternalACPConnection
    external_session_id: str

    attached_agent_id: str | None = None
    _forwarding_task: asyncio.Task[None] | None = field(
        default=None,
        init=False,
        repr=False,
    )
    _closed: bool = field(
        default=False,
        init=False,
        repr=False,
    )

    async def attach(self, agent_id: str) -> None:
        self._ensure_open()
        self._require_known_agent(agent_id)

        if (
            self.attached_agent_id == agent_id
            and self._forwarding_task is not None
            and not self._forwarding_task.done()
        ):
            return

        await self.detach()

        self.attached_agent_id = agent_id
        self._forwarding_task = asyncio.create_task(
            self._forward_agent_events(agent_id),
            name=f"swarm-acp-mux:{agent_id}”,
        )

    async def detach(self) -> None:
        task = self._forwarding_task

        self._forwarding_task = None
        self.attached_agent_id = None

        if task is None:
            return

        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

    async def prompt(self, text: str) -> str | None:
        self._ensure_open()
        return await self.agent_client.prompt(
            self._require_attached_agent(),
            text,
        )

    async def interrupt(self) -> None:
        self._ensure_open()
        await self.agent_client.interrupt(
            self._require_attached_agent()
        )

    def get_swarm_status(self) -> dict[str, object]:
        self._ensure_open()

        return {
            “attached_agent_id”: self.attached_agent_id,
            “swarm”: self.daemon.get_swarm_status(),
        }

    def get_agent_detail(
        self,
        agent_id: str,
        *,
        max_events: int = 100,
    ) -> dict[str, object]:
        self._ensure_open()
        self._require_known_agent(agent_id)

        return {
            “attached”: agent_id == self.attached_agent_id,
            “agent”: self.daemon.get_agent_detail(
                agent_id=agent_id,
                max_events=max_events,
            ),
        }

    async def close(self) -> None:
        if self._closed:
            return

        self._closed = True
        await self.detach()

    async def _forward_agent_events(self, agent_id: str) -> None:
        current_task = asyncio.current_task()

        try:
            async with self.agent_client.subscribe_events(
                agent_id
            ) as events:
                async for event in events:
                    await self._forward_external_event(event)
        finally:
            if (
                self.attached_agent_id == agent_id
                and self._forwarding_task is current_task
            ):
                self.attached_agent_id = None
                self._forwarding_task = None

    async def _forward_external_event(
        self,
        event: AgentEvent,
    ) -> None:
        await self.external_connection.session_update(
            session_id=self.external_session_id,
            update=require_session_update(event),
        )

    def _require_attached_agent(self) -> str:
        if self.attached_agent_id is None:
            raise NoAttachedAgentError(
                “No agent is attached to this ACP connection"
            )

        return self.attached_agent_id

    def _require_known_agent(self, agent_id: str) -> AgentState:
        try:
            return self.daemon.swarm_state.agents[agent_id]
        except KeyError as exc:
            raise UnknownAgentError(agent_id) from exc

    def _ensure_open(self) -> None:
        if self._closed:
            raise SwarmACPMuxClosedError()

    async def __aenter__(self) -> “SwarmACPMux”:
        self._ensure_open()
        return self

    async def __aexit__(
        self,
        exc_type,
        exc,
        traceback,
    ) -> None:
        await self.close()
```

------------------------------------------------------------------------

## Tests

Tests should focus on complete routing and lifecycle behavior.

### Attachment and replay

- attaching subscribes to the selected agent;
- retained events are forwarded before later live events;
- an event published during attachment is delivered exactly once;
- switching attachment cancels the old subscription and replays the new agent's retained stream;
- the attachment acknowledgment identifies the new agent.

### Agent request forwarding

- `prompt()` delegates to the attached agent;
- `interrupt()` delegates to the attached agent;
- either operation without an attachment raises `NoAttachedAgentError`.

### External control routing

Test the swarm ACP server adapter together with the mux:

- `_attach` calls `mux.attach(agent_id)`;
- `_detach` calls `mux.detach()`;
- `_swarm_status` returns `mux.get_swarm_status()`;
- `_agent_detail` returns `mux.get_agent_detail(agent_id)`;
- an unknown underscore-prefixed update returns a structured error;
- reserved control updates are not proxied to `NateOhaAcpClient`.

### Multiple subscribers

Using the real event-stream implementation:

- the mux and an independent subscriber receive the same agent event;
- each receives retained history followed by live events;
- detaching the mux leaves the independent subscription active.

### Lifecycle

- `close()` is idempotent;
- detach removes only the mux subscription;
- normal agent-stream closure leaves the mux open and unattached;
- external write failure terminates the forwarding task;
- operations after close raise `SwarmACPMuxClosedError`.

### Macro integration test

Add one real-path async integration test that:

1. starts a real agent;
2. creates an external swarm ACP session and `SwarmACPMux`;
3. sends external `_attach`;
4. verifies that the mux attaches and the control update is not sent to the agent;
5. confirms that retained agent events are replayed;
6. sends an ordinary prompt;
7. verifies that agent output reaches the external ACP connection;
8. confirms that an independent subscriber also receives the agent output;
9. sends external `_detach`;
10. verifies that the mux becomes unattached while the agent remains running.

------------------------------------------------------------------------

## Summary

`SwarmACPMux` is a small connection-scoped router with two clear paths:

```
# External client -> swarm or attached agent
if update.session_update.startswith(“_”):
    await dispatch_reserved_update(mux, update)
else:
    await proxy_ordinary_request(mux, update)

# Attached agent -> external client
async with agent_client.subscribe_events(agent_id) as events:
    async for event in events:
        await mux.forward_external_event(event)
```

The runtime supplies replay-capable per-agent subscriptions. The outer ACP server supplies reserved-update decoding. The mux owns only attachment and routing.

This yields one event stream, one agent client, one mux implementation, and one authoritative place for each responsibility.
