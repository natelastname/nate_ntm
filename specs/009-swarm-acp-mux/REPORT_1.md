# Async ACP Event Path Architecture Report (Epic005 Real-Path Tests)

**Repository:** `nate-ntm`  
**Focus:** Async ACP event path exercised by `tests/integration/runtime_acp/test_runtime_daemon_acp_async_real_path_epic005.py`  
**Scope:** Factual description only — no proposed changes.

---

## 0. Test under analysis and scenario overview

File:

```text
tests/integration/runtime_acp/test_runtime_daemon_acp_async_real_path_epic005.py
```

This file defines three async integration tests using the real `nate-oha` ACP implementation and, in one test, real Agent Mail:

1. `test_runtime_daemon_acp_async_persists_session_id_and_exposes_via_detail`
2. `test_runtime_daemon_acp_async_with_agent_mail_real_path_epic005`
3. `test_runtime_daemon_acp_async_prompt_echo_and_replay_real_path`

Common pattern:

- Construct **REAL** runtime adapters:
  - `NateOhaAcpClient` as `adapters.acp`
  - `McpAgentMailClient` as `adapters.agent_mail` (test 2 only)
- Persist an `AgentState` into a `SwarmState` via `MetadataStore`.
- Resume a `RuntimeDaemon` from that stored swarm.
- Use `acp_client.subscribe_events(agent_id)` to subscribe to async ACP events for that agent.
- Call `acp_client.start_agent_async(agent_id, metadata=AgentState)` to create or resume an ACP session.
- In test 3, additionally call `acp_client.prompt(agent_id, text)` and observe echoed text via the event stream, then resume and rely on ACP-level replay.

The rest of this report traces and characterizes the async ACP path exercised by these tests.

---

## 1. Per-agent concrete objects

This section lists concrete objects created per agent and how they relate to:

- ACP client / session / connection
- Reader coroutine or task
- Event queues
- Replay buffer(s)
- Subscriber registry
- Runtime state objects

### 1.1 Durable per-agent metadata: `AgentState`

**File:** `src/nate_ntm/runtime/swarm_state.py`

```python
# src/nate_ntm/runtime/swarm_state.py
class AgentState(BaseModel):
    agent_id: str
    display_name: str
    ...
    # ACP-owned conversation identifier used for ``--resume``.
    conversation_id: Optional[str] = None
    ...
    nate_oha_config: NateOHAConfig
```

In the tests, `AgentState` instances are created explicitly and stored into `SwarmState` via `MetadataStore.save_swarm_state`.

Example (test 1):

```python
# tests/.../test_runtime_daemon_acp_async_real_path_epic005.py
meta = AgentState(
    agent_id="nav-async-1",
    display_name="Navigator Async 1",
    conversation_id="",  # Force the "session/new" path.
    nate_oha_config=nate_oha_cfg,
)
```

Role in ACP path:

- Provides the **initial** `conversation_id` (empty for new sessions, non-empty for resume).
- Provides effective `NateOhaConfig` for building the `nate-oha acp` command.
- After `start_agent_async`, the ACP `session_id` is persisted back into this `conversation_id` via `MetadataStore`.

### 1.2 Durable swarm metadata: `SwarmState`

**File:** `src/nate_ntm/runtime/swarm_state.py`

```python
class SwarmState(BaseModel):
    swarm_id: str
    project_path: Path
    agent_mail_project_id: str = ""
    ...
    agents: Dict[str, AgentState] = Field(default_factory=dict)
```

In the tests, `SwarmState` is constructed and saved before the daemon resumes:

```python
swarm = SwarmState(
    swarm_id=config.swarm_id,
    project_path=config.project_path,
    agent_mail_project_id=str(config.project_path),
    ...,
    agents={meta.agent_id: meta},
)
store.save_swarm_state(swarm)
```

Role in ACP path:

- Provides persistent per-agent `AgentState` for `RuntimeDaemon.resume`.
- Supplies the set of agents known to the runtime and their stored `conversation_id` and `NateOhaConfig`.

### 1.3 In-memory runtime state: `RuntimeState` and `AgentRuntimeState`

**File:** `src/nate_ntm/runtime/state.py`

```python
@dataclass(slots=True)
class AgentRuntimeState:
    agent_id: str
    status: AgentStatus = AgentStatus.STARTING
    current_turn_id: Optional[str] = None
    last_error: Optional[str] = None
    subprocess_handle: Optional[object] = None
    acp_connection: Optional[object] = None
    event_stream: Optional["AgentEventStream"] = None

@dataclass(slots=True)
class RuntimeState:
    config: RuntimeConfig
    agents: Dict[str, AgentRuntimeState] = field(default_factory=dict)
    status: RuntimeStatus = RuntimeStatus.STARTING
```

In the tests:

- `RuntimeDaemon.resume(config)` creates a `RuntimeState` instance with an empty `agents` dict; test 1 asserts this:

  ```python
  assert daemon.state.agents == {}
  ```

- An `AgentRuntimeState` is created lazily, on first event, in `AgentSupervisor.ensure_agent_runtime_state` and attached to `RuntimeState.agents`.

Role in ACP path:

- Tracks live per-agent process/connection handles and attaches the in-memory event buffer (`AgentEventStream`).

### 1.4 In-memory per-agent event buffer: `AgentEventStream`

**File:** `src/nate_ntm/runtime/events.py`

```python
@dataclass(slots=True)
class AgentEventStream:
    agent_id: str
    max_events: int = _DEFAULT_MAX_EVENTS
    _events: List[AgentEvent] = field(default_factory=list, init=False, repr=False)

    def append(self, event: AgentEvent) -> None:
        if event.agent_id != self.agent_id:
            raise ValueError(...)
        self._events.append(event)
        overflow = len(self._events) - self.max_events
        if overflow > 0:
            del self._events[0:overflow]

    def get_events(self, limit: Optional[int] = None) -> List[AgentEvent]:
        ...
```

Created via `AgentSupervisor._get_or_create_event_stream(runtime_state)` and used by `AgentSupervisor.append_agent_event` to buffer recent events per agent.

Role in ACP path:

- Acts as the **runtime-level replay buffer** for `RuntimeDaemon.get_agent_detail` (but **not** for `subscribe_events`).
- Stores a bounded history of all `AgentEvent`s (ACP + Agent Mail + runtime) per agent.

### 1.5 ACP client and per-agent session: `NateOhaAcpClient` and `AcpAgentSession`

**File:** `src/nate_ntm/runtime/acp_client.py`

Core structure:

```python
@dataclass(slots=True)
class NateOhaAcpClient(BaseAcpClient):
    config: RuntimeConfig
    executable: str = "nate-oha"
    startup_timeout: float = 15.0
    shutdown_timeout: float = 10.0

    _processes: Dict[str, NateOhaProcessRecord] = field(default_factory=dict, init=False)
    _process_handles: Dict[str, subprocess.Popen] = field(default_factory=dict, init=False)
    _sessions: Dict[str, AcpAgentSession] = field(default_factory=dict, init=False)
    _session_contexts: Dict[str, Any] = field(default_factory=dict, init=False)
    _temp_config_dirs: Dict[str, str] = field(default_factory=dict, init=False)

    # Per-agent subscribers for async event streaming.
    _event_subscribers: Dict[str, Set[asyncio.Queue[Any]]] = field(
        default_factory=dict,
        init=False,
    )
```

Per-agent session record:

```python
@dataclass(slots=True)
class AcpAgentSession:
    agent_id: str
    conversation_id: str
    process: Any
    connection: Any
    protocol_client: Any
    status: str = "starting"
    stderr_task: Any | None = None
    exit_monitor_task: Any | None = None
```

Role in ACP path:

- One `AcpAgentSession` per agent tracks:
  - `conversation_id` (ACP `session_id`),
  - the ACP `ClientSideConnection`,
  - the `NateNtmAcpProtocolClient` instance,
  - the underlying `nate-oha` subprocess.
- `NateOhaAcpClient.start_agent_async` creates/updates this session based on `AgentState.conversation_id`.

### 1.6 ACP protocol client: `NateNtmAcpProtocolClient`

**File:** `src/nate_ntm/runtime/acp_protocol_client.py`

```python
class NateNtmAcpProtocolClient(Client):
    def __init__(..., agent_id: str, event_sink: EventSink, clock: Callable[[], datetime] | None = None) -> None:
        self._agent_id = agent_id
        self._event_sink = event_sink
        self._clock = clock or datetime.utcnow
        self._session_id: str | None = None
        self._sequence: int = 0

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self._session_id = session_id
        self._sequence += 1
        event = translate_acp_update(
            agent_id=self._agent_id,
            session_id=session_id,
            update=update,
            sequence=self._sequence,
            timestamp=self._clock(),
        )
        self._event_sink(event)
```

Role in ACP path:

- Implements the ACP SDK `Client` interface.
- Normalizes ACP `session/update` notifications into `AgentEvent` via `translate_acp_update` and forwards them through `event_sink` (wired to `NateOhaAcpClient._emit_event`).

### 1.7 Event queues and subscriber registry

**File:** `src/nate_ntm/runtime/acp_client.py`

Subscriber registry and queue creation:

```python
_event_subscribers: Dict[str, Set[asyncio.Queue[Any]]] = field(
    default_factory=dict,
    init=False,
)

def _register_event_subscriber(self, agent_id: str) -> asyncio.Queue[Any]:
    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=_EVENT_QUEUE_MAXSIZE)
    subscribers = self._event_subscribers.setdefault(agent_id, set())
    subscribers.add(queue)
    return queue

def _unregister_event_subscriber(self, agent_id: str, queue: asyncio.Queue[Any]) -> None:
    subscribers = self._event_subscribers.get(agent_id)
    if subscribers is not None:
        subscribers.discard(queue)
        if not subscribers:
            self._event_subscribers.pop(agent_id, None)
```

Role in ACP path:

- Maintains **one bounded `asyncio.Queue` per subscriber per agent**.
- `subscribe_events(agent_id)` registers a new queue and returns an async iterator that reads from that queue.

### 1.8 Runtime event sink: `AgentSupervisor` and `AgentEventStream`

**File:** `src/nate_ntm/runtime/agents.py`

```python
@dataclass(slots=True)
class AgentSupervisor:
    config: RuntimeConfig
    state: RuntimeState
    swarm_state: SwarmState
    on_agent_event: Callable[[AgentEvent], None] | None = None

    def append_agent_event(self, event: AgentEvent) -> None:
        agent_state = self.swarm_state.agents.get(event.agent_id)
        if agent_state is not None:
            runtime_state = self.ensure_agent_runtime_state(agent_state)
        else:
            runtime_state = self.state.agents.get(event.agent_id)
            if runtime_state is None:
                return

        stream = self._get_or_create_event_stream(runtime_state)
        stream.append(event)
        if self.on_agent_event is not None:
            self.on_agent_event(event)
```

Wiring from `RuntimeDaemon.resume`:

```python
# src/nate_ntm/runtime/daemon.py
acp_client.on_event = agent_supervisor.append_agent_event
```

Role in ACP path:

- Receives **all** events delivered by `NateOhaAcpClient._emit_event` via the `on_event` callback.
- Ensures there is an `AgentRuntimeState` and `AgentEventStream` for the agent.
- Appends events into `AgentEventStream` to support `RuntimeDaemon.get_agent_detail`.

---

## 2. Event flow for one ordinary ACP event

This section traces a single ACP `session/update` event from the agent subprocess up to every consumer involved in these tests.

### 2.1 Agent process and ACP connection creation

From the tests (pattern in all three):

```python
async with acp_client.subscribe_events(agent_id) as events:
    await acp_client.start_agent_async(agent_id, metadata=meta)
    ...
```

**`NateOhaAcpClient.start_agent_async`**:

```python
async def start_agent_async(self, agent_id: str, *, metadata: AgentState) -> None:
    session = self._sessions.get(agent_id)
    if session is not None and session.status in {"starting", "running"}:
        return

    cmd = self._build_command(agent_id, metadata)
    env = self._build_env(agent_id, metadata)

    cm = open_nate_oha_acp_client(
        command=cmd,
        env=env,
        cwd=self.config.project_path,
        agent_id=agent_id,
        event_sink=self._emit_event,
        capabilities=NATE_NTM_CLIENT_CAPABILITIES,
    )

    connection, process, protocol_client = await cm.__aenter__()
    self._session_contexts[agent_id] = cm
    ...
```

`open_nate_oha_acp_client` creates the connection and protocol client:

```python
async with spawn_stdio_transport(...) as (reader, writer, process):
    protocol_client = NateNtmAcpProtocolClient(
        agent_id=agent_id,
        event_sink=event_sink,
    )
    connection = ClientSideConnection(
        protocol_client,
        writer,
        reader,
        use_unstable_protocol=use_unstable_protocol,
    )
    yield connection, process, protocol_client
```

Then `start_agent_async` initializes ACP and creates or loads a session:

```python
await connection.initialize(...)

conversation_id = (metadata.conversation_id or "").strip()
if conversation_id:
    await connection.load_session(cwd=str(self.config.project_path), session_id=conversation_id)
else:
    new_session = await connection.new_session(cwd=str(self.config.project_path))
    conversation_id = new_session.session_id
    ...
    updated_state = existing_state.model_copy(update={"conversation_id": conversation_id})
    store.save_agent_state(updated_state)

self._sessions[agent_id] = AcpAgentSession(
    agent_id=agent_id,
    conversation_id=conversation_id,
    process=process,
    connection=connection,
    protocol_client=protocol_client,
    status="running",
)
```

At this point we have, **per agent**:

- A live `ClientSideConnection` to the `nate-oha` process.
- A `NateNtmAcpProtocolClient` attached as the ACP client implementation.
- An `AcpAgentSession` tracked in `_sessions[agent_id]`.

### 2.2 ACP notification to `AgentEvent` translation

When the ACP library receives a `session/update` message from the subprocess, it calls the protocol client:

```python
async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
    self._session_id = session_id
    self._sequence += 1
    event = translate_acp_update(
        agent_id=self._agent_id,
        session_id=session_id,
        update=update,
        sequence=self._sequence,
        timestamp=self._clock(),
    )
    self._event_sink(event)  # -> NateOhaAcpClient._emit_event
```

`translate_acp_update` normalizes the update:

```python
def translate_acp_update(*, agent_id: str, session_id: str, update: Any,
                         sequence: int, timestamp: datetime | None = None) -> AgentEvent:
    if sequence <= 0:
        raise ValueError("sequence must be positive")
    if timestamp is None:
        timestamp = datetime.utcnow()

    kind = _update_kind(update)  # e.g. "user_message_chunk"
    payload: Dict[str, Any] = {
        "session_id": session_id,
        "update": _model_to_payload(update),
    }
    event_type = f"acp.{kind}"
    event_id = f"{agent_id}:{session_id}:{sequence}"

    return AgentEvent(
        event_id=event_id,
        timestamp=timestamp,
        agent_id=agent_id,
        source=AgentEventSource.ACP,
        type=event_type,
        payload=payload,
    )
```

**Normalization step:**

- All ACP `session/update` notifications become `AgentEvent` with:
  - `source = AgentEventSource.ACP`
  - `type = "acp.<kind>"`
  - `payload["session_id"] = session_id`
  - `payload["update"] = JSON-serializable representation of the ACP update
- There is no distinction between “reserved” vs “ordinary” ACP events at this layer.

### 2.3 `_emit_event`: fan-out to subscribers and runtime

Single emission point in `NateOhaAcpClient`:

```python
def _emit_event(self, event: AgentEvent) -> None:
    # 1. Broadcast to per-agent subscriber queues.
    subscribers = self._event_subscribers.get(event.agent_id)
    if subscribers:
        for queue in list(subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    _dropped = queue.get_nowait()
                except asyncio.QueueEmpty:
                    _dropped = None
                logger.warning("acp_event_queue_overflow_drop_oldest", extra={...})
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    logger.error("acp_event_queue_overflow_unresolved", extra={...})

    # 2. Forward to runtime-level callback.
    if self.on_event is not None:
        self.on_event(event)
```

Consumers of this call:

1. **Per-subscriber queues** (async subscriptions via `subscribe_events`).
2. The **runtime** via `acp_client.on_event = agent_supervisor.append_agent_event`.

### 2.4 Async subscribers: queues and iterators

Public subscription API:

```python
@asynccontextmanager
async def subscribe_events(self, agent_id: str) -> AsyncIterator[AsyncIterator[AgentEvent]]:
    queue = self._register_event_subscriber(agent_id)

    async def _iterator() -> AsyncIterator[AgentEvent]:
        try:
            while True:
                item = await queue.get()
                if item is _EVENT_STREAM_CLOSED:
                    break
                event = item  # AgentEvent
                yield event
        finally:
            self._unregister_event_subscriber(agent_id, queue)

    try:
        yield _iterator()
    finally:
        self._unregister_event_subscriber(agent_id, queue)
        try:
            queue.put_nowait(_EVENT_STREAM_CLOSED)
        except asyncio.QueueFull:
            ...
```

In the tests this is used as:

```python
async with acp_client.subscribe_events(agent_id) as events:
    await acp_client.start_agent_async(agent_id, metadata=meta)
    await collect_events_for(events, events_run1, duration=0.5)
```

and similarly for the replay and prompt tests.

**Reader coroutine:**

- The `_iterator` async generator is the **per-subscriber reader** that calls `queue.get()` in a loop.
- When `_EVENT_STREAM_CLOSED` is received, the iterator exits.
- On `finally`, it unregisters its queue from `_event_subscribers`.

### 2.5 Runtime sink and replay buffer (`AgentEventStream`)

The runtime sees every event through the `on_event` callback:

```python
# wired in RuntimeDaemon.resume
acp_client.on_event = agent_supervisor.append_agent_event
```

The handler:

```python
def append_agent_event(self, event: AgentEvent) -> None:
    agent_state = self.swarm_state.agents.get(event.agent_id)
    if agent_state is not None:
        runtime_state = self.ensure_agent_runtime_state(agent_state)
    else:
        runtime_state = self.state.agents.get(event.agent_id)
        if runtime_state is None:
            return

    stream = self._get_or_create_event_stream(runtime_state)
    stream.append(event)
    if self.on_agent_event is not None:
        self.on_agent_event(event)
```

The per-agent replay buffer is the `AgentEventStream` referenced from `runtime_state.event_stream`. `RuntimeDaemon.get_agent_detail` reads from it:

```python
def get_agent_detail(self, agent_id: str, max_events: int) -> dict[str, object]:
    runtime_state = self.state.agents.get(agent_id)
    stream = runtime_state.event_stream if runtime_state is not None else None

    events_payload: list[dict[str, object]] = []
    if stream is not None:
        events = stream.get_events(limit=max_events)
        events_payload = [event.to_dict() for event in events]

    return {"agent": agent_payload, "events": events_payload}
```

This API is asserted in tests 1 and 2 to surface metadata fields (`conversation_id`, `agent_mail_identity`), but they do **not** assert on the event list itself.

---

## 3. Subscription model

### 3.1 Queue topology and fan-out

- `_event_subscribers: Dict[str, Set[asyncio.Queue[Any]]]`
  - Keyed by `agent_id`.
  - Each value is a **set of queues**, one per active subscriber.
- `subscribe_events(agent_id)`:
  - Creates a new `asyncio.Queue(maxsize=_EVENT_QUEUE_MAXSIZE)` per call.
  - Registers that queue in `_event_subscribers[agent_id]`.
  - Returns an async iterator linked to that queue.

**Fan-out:**

- `_emit_event` iterates over all queues in `_event_subscribers[agent_id]` and does `put_nowait(event)`.
- Therefore, **multiple subscribers can receive the same `AgentEvent` simultaneously**, each via its own queue.
- The Epic005 tests use only **one subscription at a time per agent**, so multi-subscriber fan-out is supported by design but not exercised.

### 3.2 Interface shape

- `subscribe_events` is an **async context manager** returning an **async iterator of `AgentEvent`**.
- The intended usage pattern is:

  ```python
  async with acp_client.subscribe_events(agent_id) as events:
      async for event in events:
          ...
  ```

- There is also a convenience method `iter_events(agent_id)` that internally uses `subscribe_events` to return just the iterator.

### 3.3 Registration and removal

- **Registration:** `_register_event_subscriber(agent_id)` allocates a bounded queue and adds it to the set.
- **Removal:**
  - On iterator exit (`finally` of `_iterator`): `_unregister_event_subscriber(agent_id, queue)`.
  - On context manager exit (`finally` of `subscribe_events`): `_unregister_event_subscriber` is called again (idempotently) and `_EVENT_STREAM_CLOSED` is enqueued to terminate the iterator if it is still waiting.

This double cleanup makes subscriber removal robust in the face of partial consumption or cancellation.

### 3.4 Slow subscribers and backpressure

Queues are bounded: `asyncio.Queue(maxsize=_EVENT_QUEUE_MAXSIZE)`.

In `_emit_event`:

- If `put_nowait(event)` raises `QueueFull`:
  - The implementation attempts `queue.get_nowait()` to drop the oldest event for that subscriber.
  - Logs a warning `acp_event_queue_overflow_drop_oldest`.
  - Tries `put_nowait(event)` again.
  - If still full, logs an error `acp_event_queue_overflow_unresolved` and continues.

Effect:

- A slow subscriber **does not block the ACP path**.
- The oldest events for that specific subscriber are dropped when over capacity; other subscribers and the runtime callback are unaffected.

### 3.5 Cancellation and stream closure

**Subscriber cancellation:**

- If caller cancels or exits the `async for` early, `_iterator`’s `finally` block runs and unregisters the queue.

**Agent stop / termination:**

- When `NateOhaAcpClient.stop_agent_async` or its sync counterpart runs, it calls `_close_event_subscribers(agent_id)`:

  ```python
  def _close_event_subscribers(self, agent_id: str) -> None:
      subscribers = self._event_subscribers.pop(agent_id, None)
      if not subscribers:
          return
      for queue in list(subscribers):
          try:
              queue.put_nowait(_EVENT_STREAM_CLOSED)
          except asyncio.QueueFull:
              ...  # drop oldest then retry; log if still full
  ```

- The sentinel `_EVENT_STREAM_CLOSED` makes iterators exit their read loop.
- The Epic005 tests do call `stop_agent_async` in `finally` blocks but do not assert on iterator closure behavior.

### 3.6 Replay behavior and races

There are **two distinct notions of “replay”**:

1. **Runtime-level replay buffer**: `AgentEventStream`
   - Maintains a bounded list of events per agent.
   - Only used by APIs such as `RuntimeDaemon.get_agent_detail`.
   - **Not connected** to `subscribe_events`; new subscribers do **not** see historical events from this buffer.

2. **ACP-level conversation replay** (what test 3 uses):
   - When `start_agent_async` is called with a non-empty `conversation_id`, it uses `connection.load_session(session_id=...)`.
   - The ACP server (nate-oha) re-emits prior `session/update` events for that conversation.
   - From the runtime’s perspective, these look like ordinary live updates; they arrive through `session_update` → `translate_acp_update` → `_emit_event`.

**Replay/live race prevention:**

- There is **no explicit race handling logic** in this repo:
  - `subscribe_events` registers the queue **before** returning, so the subscriber is active before the subsequent `start_agent_async` call.
  - All events—whether replayed by ACP or newly generated—are delivered in the order ACP emits them.
  - There is no special flow to merge buffered and live events; replay is purely server-side.

---

## 4. “Reserved events” in this path

The codebase does **not** define a formal “reserved event” set in the sense of:

- a specific type enum or constant list,
- a predicate like `is_reserved_event(event)`, or
- filters that withhold certain events from external consumers.

However, multiple **families of events** exist. This section catalogues them and notes any special handling that might resemble “reserved” semantics.

### 4.1 ACP-translated events (`type = "acp.<kind>"`)

Produced by `translate_acp_update` as shown above.

- **Recognition:** `event.source == AgentEventSource.ACP` and `event.type.startswith("acp.")`.
- **Meaning:** direct translations of ACP `session/update` notifications.
- **Interception/withholding:**
  - No filtering or reservation:
    - `_emit_event` forwards all such events to **both** subscriber queues and `on_event`.
    - `AgentSupervisor.append_agent_event` buffers all of them.

The tests explicitly assert their presence:

```python
all_events = events_run1 + events_run2
assert any(ev.type.startswith("acp.") for ev in all_events)
```

### 4.2 Process lifecycle events (`nate_oha_process_*`)

These are constructed only in the **synchronous** start/stop path, not the async path used by the Epic005 tests.

```python
def _make_process_event(..., event_type: str, payload: Mapping[str, Any]) -> AgentEvent:
    return AgentEvent(
        event_id=f"{agent_id}:{event_type}:{uuid.uuid4()}",
        timestamp=datetime.utcnow(),
        agent_id=agent_id,
        source=AgentEventSource.ACP,
        type=event_type,
        payload=payload,
    )
```

Used in `start_agent` / `stop_agent` to report events like:

- `"nate_oha_process_started"`
- `"nate_oha_process_ready"`
- `"nate_oha_process_exited"`
- `"nate_oha_process_crashed"`

**In the Epic005 async path:**

- `start_agent_async` and `stop_agent_async` do **not** emit these events.
- Thus they are **not present** in the event streams observed by these tests.

### 4.3 Runtime and Agent Mail events

`AgentSupervisor` can also create runtime/Agent Mail events (e.g. `AgentFailed`, `AgentRestarted`, `MailReceived`) and append them to `AgentEventStream`. These are not specific to ACP and are not explicitly exercised or asserted in the Epic005 tests.

### 4.4 Unsupported ACP request handlers

`NateNtmAcpProtocolClient` implements several ACP “device” methods by throwing `RequestError.invalid_request(...)` instead of emitting events:

```python
async def request_permission(...): raise RequestError.invalid_request(...)
async def read_text_file(...):    raise RequestError.invalid_request(...)
async def write_text_file(...):   raise RequestError.invalid_request(...)
async def create_terminal(...):   raise RequestError.invalid_request(...)
async def terminal_output(...):   raise RequestError.invalid_request(...)
async def release_terminal(...):  raise RequestError.invalid_request(...)
async def wait_for_terminal_exit(...): raise RequestError.invalid_request(...)
async def kill_terminal(...):     raise RequestError.invalid_request(...)
async def create_elicitation(...):raise RequestError.invalid_request(...)
async def complete_elicitation(...): raise RequestError.invalid_request(...)
```

These methods can be seen as “reserved” ACP operations that are **not supported** in this runtime:

- They are rejected synchronously via ACP errors.
- They **do not** produce `AgentEvent`s, so nothing about them appears on the async ACP event path.

### 4.5 Withheld events / filtering

Within the async ACP path exercised by the tests:

- No events are filtered out before reaching subscriber queues.
- No events are filtered out before reaching `AgentSupervisor.append_agent_event`.

Therefore:

- **There are currently no reserved events that are withheld from ordinary external consumers**.
- All events that are created (both ACP and runtime/Agent Mail) follow the standard pipeline.

---

## 5. What the real-path tests prove

This section summarizes each test’s assertions and maps them to behaviors.

### 5.1 Test 1

**Name:** `test_runtime_daemon_acp_async_persists_session_id_and_exposes_via_detail`

Key assertions and what they prove:

1. **Daemon resume does not pre-populate `RuntimeState.agents`:**

   ```python
   assert daemon.state.agents == {}
   ```

   - Confirms that `RuntimeDaemon.resume` alone does not create `AgentRuntimeState` entries.

2. **REAL ACP adapter is used:**

   ```python
   assert isinstance(adapters.acp, NateOhaAcpClient)
   assert isinstance(acp_client, NateOhaAcpClient)
   ```

3. **`conversation_id` is persisted after async session start:**

   ```python
   reloaded_meta = store.load_agent_state(meta.agent_id)
   session_id = reloaded_meta.conversation_id
   assert isinstance(session_id, str) and session_id
   ```

   - Proves `start_agent_async` calls `new_session` and then writes `session_id` back to `AgentState.conversation_id` via `MetadataStore`.

4. **Event payload `session_id` matches persisted `conversation_id` (run 1):**

   ```python
   for event in events_run1:
       assert event.agent_id == meta.agent_id
       payload_session = event.payload.get("session_id")
       if payload_session is not None:
           assert payload_session == session_id
   ```

   - Proves `translate_acp_update` correctly copies the ACP session ID into `payload["session_id"]` and that this matches the persisted conversation ID.

5. **Second run resumes same session:**

   - They reload metadata, assert `conversation_id` unchanged, start again, collect `events_run2`, and re-assert the same `session_id` invariant.

6. **At least one ACP event is observed across both runs:**

   ```python
   all_events = events_run1 + events_run2
   assert any(ev.type.startswith("acp.") for ev in all_events)
   ```

   - Proves that ACP `session/update` notifications are reaching the async subscription as `type="acp.*"` events.

7. **`get_agent_detail` exposes metadata-level `conversation_id`:**

   ```python
   detail = daemon.get_agent_detail(agent_id=meta.agent_id, max_events=10)
   agent_payload = detail["agent"]
   assert agent_payload["conversation_id"] == session_id
   ```

   - Confirms that `RuntimeDaemon.get_agent_detail` reads persisted metadata (via `MetadataStore`) and surfaces `conversation_id` regardless of `RuntimeState.agents` initial emptiness.

**Behavior categories:**

- **Multiple-subscriber fan-out:** not tested.
- **Replay behavior:** not tested; two runs share a session but no assertion on replay.
- **Live delivery:** tested at a basic level (ACP events arrive during a live session).
- **Reserved-event interception:** not tested; only a prefix `"acp."` is asserted.
- **Cancellation/cleanup:** not tested; `stop_agent_async` is called in `finally` but not inspected.

### 5.2 Test 2

**Name:** `test_runtime_daemon_acp_async_with_agent_mail_real_path_epic005`

Key assertions:

1. **REAL adapters:**

   ```python
   adapters = create_runtime_adapters(config)
   assert isinstance(adapters.agent_mail, McpAgentMailClient)
   assert isinstance(adapters.acp, NateOhaAcpClient)
   ```

2. **Agent Mail project and identity/credentials allocated:**

   ```python
   agent_mail_project_id = agent_mail_client.ensure_project()
   identity, token = agent_mail_client.ensure_agent_identity_with_credentials(agent_id)
   assert identity
   assert token
   ```

3. **Agent Mail settings embedded and persisted in `NateOhaConfig`:**

   ```python
   nate_oha_cfg = build_effective_nate_oha_config(...)
   meta = AgentState(..., nate_oha_config=nate_oha_cfg)
   ...
   reloaded_meta = store.load_agent_state(agent_id)
   cfg = getattr(reloaded_meta, "nate_oha_config", None)
   features = getattr(cfg, "features", None) if cfg is not None else None
   agent_mail_cfg = getattr(features, "agent_mail", None) if features is not None else None
   assert agent_mail_cfg is not None
   assert (agent_mail_cfg.agent_identity or "").strip() == identity
   assert (agent_mail_cfg.credentials_ref or "") == (token or "")
   ```

4. **Same `session_id` and `payload["session_id"]` invariants as test 1** across two runs, plus the `acp.*` prefix assertion.

5. **`get_agent_detail` exposes Agent Mail identity and `conversation_id`:**

   ```python
   detail = daemon.get_agent_detail(agent_id=agent_id, max_events=10)
   agent_payload = detail["agent"]
   assert agent_payload["conversation_id"] == session_id
   assert agent_payload["agent_mail_identity"] == identity
   ```

**Behavior categories:**

- **Multiple-subscriber fan-out:** not tested.
- **Replay behavior:** not tested.
- **Live delivery:** same level as test 1 (events with `acp.*` types observed).
- **Reserved-event interception:** not tested.
- **Cancellation/cleanup:** not tested.

What this adds beyond test 1:

- Demonstrates that the **REAL** Agent Mail adapter is used and that its identity/credentials configuration are propagated into Nate OHA config and later surfaced via `get_agent_detail`.

### 5.3 Test 3

**Name:** `test_runtime_daemon_acp_async_prompt_echo_and_replay_real_path`

Key phases:

1. **First run: start, prompt, echo:**

   ```python
   async with acp_client.subscribe_events(agent_id) as events1:
       await acp_client.start_agent_async(agent_id, metadata=meta)

       await acp_client.prompt(agent_id, prompt_text1)
       await _wait_for_text(events1, prompt_text1, timeout=5.0, sink=events_run1)
   ```

   `_wait_for_text` uses `next_matching_event(events, predicate, timeout, sink)` to find an event whose textual payload contains `prompt_text1`.

2. **Establish canonical echo and `session_id` invariants:**

   ```python
   reloaded_meta = store.load_agent_state(agent_id)
   session_id = reloaded_meta.conversation_id
   assert isinstance(session_id, str) and session_id

   for ev in events_run1:
       assert ev.agent_id == agent_id
       payload_session = ev.payload.get("session_id")
       if payload_session is not None:
           assert payload_session == session_id

   texts_run1 = _extract_text_payloads(events_run1)
   assert any(prompt_text1 in text for text in texts_run1)
   canonical_echo1 = next(text for text in texts_run1 if prompt_text1 in text)
   ```

3. **Stop and resume with replay:**

   ```python
   await acp_client.stop_agent_async(agent_id, timeout=5.0)

   fresh_client = NateOhaAcpClient(config=config, executable="nate-oha")
   resume_meta = store.load_agent_state(agent_id)
   assert resume_meta.conversation_id == session_id

   async with fresh_client.subscribe_events(agent_id) as events2:
       await fresh_client.start_agent_async(agent_id, metadata=resume_meta)

       await next_matching_event(
           events2,
           lambda event: canonical_echo1 in _extract_text_payloads([event]),
           timeout=5.0,
           sink=events_run2,
       )

   texts_run2_before = _extract_text_payloads(events_run2)
   assert canonical_echo1 in texts_run2_before
   ```

   - This establishes that when resuming with `load_session`, ACP re-emits prior history including `canonical_echo1`, and those events flow through the same async subscription interface.

4. **Second prompt after replay:**

   ```python
   async with fresh_client.subscribe_events(agent_id) as events3:
       await fresh_client.prompt(agent_id, prompt_text2)
       await _wait_for_text(events3, prompt_text2, timeout=5.0, sink=events_run2)
   ```

5. **`session_id` invariants across all runs:**

   ```python
   for ev in events_run1 + events_run2:
       assert ev.agent_id == agent_id
       payload_session = ev.payload.get("session_id")
       if payload_session is not None:
           assert payload_session == session_id
   ```

**Behavior categories:**

- **Multiple-subscriber fan-out:**
  - Not tested. Each `async with subscribe_events(...)` uses one subscription at a time.

- **Replay behavior:**
  - **Proved at ACP level:**
    - After stopping and resuming with `load_session`, ACP replays previous conversation history.
    - That replay is observed through a fresh subscription as ordinary `AgentEvent`s whose text includes `canonical_echo1`.
  - Note: this is server-side ACP replay, not replay from `AgentEventStream`.

- **Live delivery:**
  - **Proved:**
    - Initial prompt → new events with echoed text.
    - Post-replay prompt → new events with second echoed text over a separate subscription.

- **Reserved-event interception:**
  - Not tested.

- **Cancellation/cleanup:**
  - Not explicitly tested. Subscriptions scope correctly to `async with` blocks; iterators terminate once the awaited matching events have been consumed.

---

## 6. Narrowest integration point for a future `SwarmACPMux`

This section identifies, based on existing code only, the minimal surfaces where a future `SwarmACPMux` could logically attach to orchestrate ACP events and requests.

### 6.1 Subscribing to an agent’s ACP events

The central subscription APIs are on `NateOhaAcpClient`:

- `subscribe_events(agent_id: str)` – async context manager returning an async iterator of `AgentEvent`.
- `iter_events(agent_id: str)` – convenience method returning an iterator using `subscribe_events`.

Given the tests’ usage and the centralized fan-out in `_emit_event`, the **narrowest integration point** for consuming per-agent ACP events is:

- For each agent of interest, call **one** of:
  - `async with acp_client.subscribe_events(agent_id) as events: ...`, or
  - `events = await acp_client.iter_events(agent_id)` and then iterate it.

These methods already:

- Ensure subscription registration occurs before control returns to the caller (avoiding early-miss races between subscribe and start).
- Provide per-subscriber buffering and backpressure logic.

### 6.2 Sending ACP requests to an agent

Request APIs live on `BaseAcpClient` and are implemented by `NateOhaAcpClient`:

- `start_agent_async(agent_id: str, metadata: AgentState) -> None`
- `prompt(agent_id: str, prompt: str | None = None) -> str | None`
- `interrupt(agent_id: str) -> None`
- `stop_agent_async(agent_id: str, timeout: float) -> None`

A future `SwarmACPMux` that wants to coordinate ACP sessions should:

- Use these existing methods for lifecycle and interaction, rather than introducing alternative wrappers.

### 6.3 Layer at which to consume reserved events

Today, there is **no reserved-event filtering**. All `AgentEvent`s go through:

1. `NateNtmAcpProtocolClient.session_update` → `translate_acp_update` → `AgentEvent`.
2. `NateOhaAcpClient._emit_event(event)`.
3. `_emit_event`:
   - Fans out to subscriber queues.
   - Calls `on_event` (→ `AgentSupervisor.append_agent_event`).

If a future design were to treat some event types as “reserved” and not forward them to external consumers, the **logically narrowest choke point** in the existing path is **`NateOhaAcpClient._emit_event`**:

- All ACP-derived `AgentEvent`s pass through this method once.
- It sits immediately between translation (`translate_acp_update`) and both consumption paths (subscribers and runtime supervisor).

Alternatively, if the classification were to be done earlier, `translate_acp_update` is the only place that maps raw ACP updates to `AgentEvent.type = "acp.<kind>"`. That would be the place to tag events, while `_emit_event` would be the place to decide routing.

### 6.4 Directly interacting classes

Without introducing any new event hub or queueing abstraction, a future `SwarmACPMux` would naturally interact with:

1. **`NateOhaAcpClient`** (`src/nate_ntm/runtime/acp_client.py`)
   - For subscribing to events (`subscribe_events` / `iter_events`).
   - For sending ACP requests (`start_agent_async`, `prompt`, `interrupt`, `stop_agent_async`).
   - Potentially as the location of any reserved-event handling via `_emit_event`.

2. **`RuntimeDaemon`** (`src/nate_ntm/runtime/daemon.py`)
   - Owns the primary `NateOhaAcpClient` instance in production code.
   - Wires the runtime-level event sink (`acp_client.on_event = agent_supervisor.append_agent_event`).

3. **`AgentSupervisor`** (`src/nate_ntm/runtime/agents.py`) and **`AgentEventStream`** (`src/nate_ntm/runtime/events.py`)
   - If the mux needs awareness of the runtime’s per-agent buffered events (for API surfaces like `get_agent_detail`).

4. **`NateNtmAcpProtocolClient` / `translate_acp_update`**
   - If the mux cares about the raw ACP `update` payloads or event classification, this translation step is the explicit boundary where ACP models become `AgentEvent`s.

No additional listener registry or queue type is required: the existing `_event_subscribers` and per-subscriber `asyncio.Queue`s already implement the shared event distribution mechanism.

---

## 7. Sequence diagram (logical event flow)

The following diagram summarizes the async ACP path for one event, including where reserved-event handling **would** sit (currently a no-op), the runtime replay buffer, subscriber queue, and a prospective `SwarmACPMux` sitting as an external consumer.

```text
nate-oha agent subprocess
    |
    |  (ACP JSON-RPC over stdio)
    v
ClientSideConnection (ACP SDK)
    |
    |  calls
    v
NateNtmAcpProtocolClient.session_update(session_id, update)
    |
    |  translate_acp_update(...)
    v
AgentEvent(type="acp.<kind>", payload={"session_id", "update", ...})
    |
    |  event_sink(event)
    v
NateOhaAcpClient._emit_event(event)
    |
    |--[potential reserved-event handling here (currently none)]
    |
    |-- fan-out to per-agent subscriber queues
    |      |
    |      |  put_nowait(event) into asyncio.Queue per subscriber
    |      v
    |   Subscriber queue (per agent, per subscriber)
    |      |
    |      |  async iterator from subscribe_events() / iter_events()
    |      v
    |   SwarmACPMux (future)
    |      |
    |      |  forwards events via chosen external protocol
    |      v
    |   External ACP client / UI
    |
    |-- on_event(event)
           |
           v
    AgentSupervisor.append_agent_event(event)
           |
           |  ensure AgentRuntimeState & AgentEventStream
           v
    AgentEventStream (per-agent replay buffer)
           |
           |  get_events(limit) for API
           v
    RuntimeDaemon.get_agent_detail(agent_id)
           |
           v
    Runtime API consumer (e.g. REST/gRPC client)
```

Notes:

- The **reserved-event handling** box is purely a conceptual slot: no such filtering exists today.
- The **replay buffer** in this diagram is `AgentEventStream`, used only by runtime APIs like `get_agent_detail`, not by `subscribe_events`.
- The **replay tested in Epic005** is ACP-side conversation replay via `load_session`; it feeds back into the same `session_update → translate_acp_update → _emit_event` path as live events.

---

This report is based exclusively on the current code and tests in the `nate-ntm` repository and describes only behavior that is demonstrably present today.