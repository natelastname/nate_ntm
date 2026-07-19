# Question

“Let's say I want to throw out the whole idea of producing telemetry events and make AgentEvents tied to ACP events for a single agent. What do you think I should do?”

# Answer

I think that is a cleaner direction, but I would go all the way and make the abstraction honest.

Do not keep a generic `AgentEvent` envelope with `source`, arbitrary `type`, and untyped `payload`. Make it a **single-agent ACP event record**.

The model should represent exactly one ACP update received for one agent session:

```
@dataclass(frozen=True, slots=True)
class AgentEvent:
    sequence: int
    received_at: datetime
    agent_id: AgentId
    session_id: str
    update: SessionUpdate
```

Then the invariant is simple:

> Every `AgentEvent` corresponds to exactly one ACP `SessionUpdate` received from exactly one agent session.

No lifecycle events. No restart telemetry. No Agent Mail events. No synthetic status events. No arbitrary JSON payload.

## The architecture I would use

Each running agent owns one bounded event stream:

```
class AgentEventStream:
    def publish(self, event: AgentEvent) -> None:
        …

    def subscribe(self) -> AsyncContextManager[AsyncIterator[AgentEvent]]:
        …
```

Its responsibilities are only:

- retain recent ACP events for that agent;
- create subscriber queues;
- yield retained history, then live events;
- close subscribers when the agent session ends.

The ACP callback becomes:

```
async def session_update(
    self,
    session_id: str,
    update: SessionUpdate,
    **_: Any,
) -> None:
    self._sequence += 1

    self._event_stream.publish(
        AgentEvent(
            sequence=self._sequence,
            received_at=self._clock(),
            agent_id=self._agent_id,
            session_id=session_id,
            update=update,
        )
    )
```

The mux becomes a very thin consumer:

```
async with agent.event_stream.subscribe() as events:
    async for event in events:
        await downstream.session_update(
            session_id=mux_session_id,
            update=event.update,
        )
```

That preserves the exact typed update. The mux owns no history.

## Remove the generic event machinery

I would delete:

- `AgentEventSource`;
- string event types such as `"acp.agent_message_chunk"`;
- `translate_acp_update()`;
- `_model_to_payload()`;
- support for publishing lifecycle or telemetry records into `AgentEventStream`;
- any code that reconstructs `SessionUpdate` from `payload`;
- generic `dict[str, Any]` event payloads.

That is compatibility baggage around an abstraction you no longer want.

For runtime state such as stopped, failed, or restarting, use the runtime's actual state model:

```
class AgentStatus(Enum):
    STARTING = auto()
    RUNNING = auto()
    STOPPED = auto()
    FAILED = auto()
```

For diagnostics, use normal logging. Do not recreate a telemetry event system under another name.

## Consider renaming it anyway

Even with the cleaner model, `AgentEvent` remains slightly ambiguous because ACP itself uses the language of session updates.

I would probably prefer one of:

```
AgentSessionEvent
AgentACPEvent
ReceivedAgentUpdate
```

My preference is **`AgentSessionEvent`**. It describes the ownership boundary without tying your whole runtime API to the ACP SDK's exact naming.

```
@dataclass(frozen=True, slots=True)
class AgentSessionEvent:
    sequence: int
    received_at: datetime
    session_id: str
    update: SessionUpdate
```

You may not even need `agent_id` inside it if the stream itself belongs to one agent. Avoid duplicating information already established by the containing object:

```
class AgentSession:
    agent_id: AgentId
    events: AgentEventStream
```

Then:

```
@dataclass(frozen=True, slots=True)
class AgentSessionEvent:
    sequence: int
    received_at: datetime
    update: SessionUpdate
```

That is probably the best final shape.

## One important choice

Decide whether the stream belongs to the logical agent or the concrete ACP session.

I strongly recommend the **concrete ACP session**.

If an agent process restarts and receives a new ACP session, create a new stream. Do not silently mix updates from multiple sessions into one history. Otherwise sequence numbers, ordering, and replay meaning become unclear.

The ownership should be:

```
Logical agent
    └── current ACP session
            └── bounded AgentSessionEvent stream
```

A restart replaces the session and its stream.


## Migration sequence (breaking refactor)

- [ ] Change `AgentEvent` to hold a typed `SessionUpdate`.
- [ ] Remove `AgentEventSource`, `type`, and `payload`.
- [ ] Make the stream session-owned and single-agent.
- [ ] Publish only from `session_update()`.
- [ ] Delete all non-ACP publishers.
- [ ] Change subscribers to consume `event.update`.
- [ ] Delete translation and reconstruction code.
- [ ] Add macro-level tests for:
  - [ ] recent history followed by live updates;
  - [ ] exact typed updates preserved;
  - [ ] independent streams for two agents;
  - [ ] session restart closes the old stream;
  - [ ] mux attachment forwards history and then live updates exactly once.

In this end-state we would not preserve the old generic event API.
This is exactly the kind of abstraction where compatibility layers
would leave you with two ways to do the same thing and undermine the
cleanup.
