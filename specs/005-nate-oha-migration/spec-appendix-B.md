# Appendix B: ACP Client Architecture

This appendix defines the architectural boundary between `nate_ntm`, `nate-oha`, and the official Agent Client Protocol (ACP) Python SDK.

Its purpose is to ensure that `nate_ntm` builds on the maintained ACP implementation rather than reimplementing protocol mechanics, while clearly defining which responsibilities belong to the runtime versus the ACP library.

## Use the Official ACP Python SDK

`NateOhaAcpClient` **shall** be implemented on top of the official `agent-client-protocol` Python SDK.

The runtime shall not implement its own:

- JSON-RPC framing
- request/response correlation
- protocol schema models
- stdio transport
- message serialization
- capability negotiation
- protocol version negotiation

These responsibilities already exist within the ACP SDK and should be treated as infrastructure rather than application logic.

The project dependency is:

```
uv add agent-client-protocol
```

# Architectural Boundary

There are two distinct “client” layers in the system.

The ACP SDK provides the **protocol client**.

`NateOhaAcpClient` provides the **runtime adapter**.

They have different responsibilities.

```
Scheduler
      │
      ▼
NateOhaAcpClient
      │
      ▼
ACP SDK Client
      │
      ▼
nate-oha acp
```

The ACP SDK owns protocol mechanics.

`NateOhaAcpClient` owns runtime integration.

`nate_ntm` owns orchestration.

The intended responsibility split is:

| ACP SDK                | NateOhaAcpClient      | `nate_ntm` Runtime |
|------------------------|-----------------------|--------------------|
| JSON-RPC framing       | Process lifecycle     | Scheduling         |
| Schema models          | Launch specification  | Runtime state      |
| Capability negotiation | Session management    | Agent metadata     |
| Request routing        | ACP event translation | Persistence        |
| Callback dispatch      | Process supervision   | Restart policy     |
| Stdio transport        | Agent status          | Swarm coordination |

This separation minimizes maintenance burden while ensuring runtime behavior remains entirely under the control of `nate_ntm`.

# Launch Sequence

`NateOhaAcpClient` should launch Nate OHA using the official ACP SDK helpers rather than constructing protocol transports manually.

Conceptually:

```
process = await spawn_agent_process(
    “nate-oha”,
    “acp”,
    “--config”,
    str(config_path),
    *arguments,
)

client = NateNtmAcpProtocolClient(…)
connection = process.connection

await connection.initialize(
    ClientCapabilities(…)
)

session = await connection.new_session(…)
conversation_id = session.session_id
```

The exact helper names may evolve with the ACP SDK, but the architecture should remain the same:

- `NateOhaAcpClient` owns process creation.
- The ACP SDK owns stdio transport.
- The ACP SDK owns protocol initialization.
- `NateOhaAcpClient` owns runtime integration.

The runtime should prefer SDK-provided helpers such as `spawn_agent_process()` or equivalent rather than manually wiring subprocess pipes whenever practical.

# Capability Negotiation

During initialization, `NateOhaAcpClient` should negotiate capabilities using the ACP SDK.

Runtime behavior should primarily depend on the negotiated capability set rather than probing protocol methods and reacting to `"method not found"` responses.

Unsupported capabilities should therefore be considered an expected part of protocol negotiation rather than exceptional behavior.

# Conversation Ownership

The runtime shall never generate conversation identifiers.

Conversation identifiers are owned entirely by the ACP runtime.

The lifecycle is:

```
ACP Runtime
      │
      │ session/new
      ▼
conversation_id
      │
      ▼
persist into AgentMetadata
      │
      ▼
reuse via --resume
```

`nate_ntm` persists the identifier and later supplies it back to Nate OHA during resume.

The identifier remains opaque throughout its lifetime.

# Session Lifecycle

### New Session

```

1. Launch:
   nate-oha acp
     --config BASE
     --set …

2. Initialize ACP.
3. Negotiate capabilities.
4. Create a session.
5. Receive conversation_id.
6. Persist conversation_id.
7. Keep the process and ACP connection alive.

```

### Resume Session

```

1. Read persisted conversation_id.
2. Launch:
   nate-oha acp
     --config BASE
     --resume CONVERSATION_ID
     --set …

3. Initialize ACP.
4. Negotiate capabilities.
5. Perform the Nate OHA-defined resume flow.
6. Verify the returned conversation_id matches the persisted value.
7. Fail clearly if the identifiers disagree.

```

The exact sequence of ACP requests required after launching with `--resume` is defined by Nate OHA.

The runtime should follow the behavior expected by Nate OHA rather than assuming a particular ACP request sequence.

This interaction should be locked down with dedicated integration tests.

# Runtime Interface

The runtime-facing ACP abstraction should remain agent-oriented.

```
class BaseAcpClient(Protocol):

    async def start_agent(…)

    async def stop_agent(…)

    async def interrupt(…)

    async def get_status(…)

    async def stream_events(…)
```

Operations such as conversation creation and turn management are internal protocol concerns and should not appear in the runtime-facing interface.

# Active Session State

Each running agent should retain the runtime state necessary to supervise the ACP connection.

```
@dataclass(slots=True)
class AcpAgentSession:
    agent_id: str
    conversation_id: str
    process: AgentProcess
    connection: ClientSideConnection
    protocol_client: NateNtmAcpProtocolClient
```

This object represents the runtime's view of a live Nate OHA agent.

# ACP Event Translation

The runtime should isolate ACP-specific types behind a dedicated callback implementation.

```
class NateNtmAcpProtocolClient(Client):

    async def session_update(…):
        emit(
            translate_acp_update(…)
        )
```

Its sole responsibility is translating ACP protocol events into runtime `AgentEvent` instances.

The remainder of the runtime should not depend directly on ACP protocol models.

# Process Supervision

Although the ACP SDK manages communication, process supervision remains entirely the responsibility of `nate_ntm`.

`NateOhaAcpClient` is responsible for:

- launching agent processes;
- supervising their lifetime;
- graceful shutdown;
- escalation to forced termination when necessary;
- restart policy;
- surfacing failures to the scheduler.

The ACP SDK should not be treated as a process supervisor.

# Stream Ownership

The subprocess streams have fixed responsibilities.

- stdin carries ACP protocol traffic.
- stdout carries ACP protocol traffic.
- stderr is reserved for diagnostics.

The runtime shall never parse human-readable stdout.

Agent readiness is determined exclusively through successful ACP initialization and session establishment.

```
process launched
        │
        ▼
ACP initialized
        │
        ▼
session established
        │
        ▼
agent ready
```

# Testing Strategy

Testing should validate runtime orchestration rather than re-testing the ACP SDK.

### Integration Tests

Echo mode should exercise the complete production pipeline:

- launch Nate OHA;
- initialize ACP;
- negotiate capabilities;
- create or resume a session;
- exchange prompts;
- consume streamed events;
- interrupt;
- graceful shutdown;
- resume.

These should be real subprocess tests using the official ACP SDK.

### Agent Mode Tests

A smaller set of integration tests should execute the same lifecycle using `runtime.mode=agent` and real LLM credentials.

### Unit Tests

Unit tests should focus exclusively on runtime-owned behavior:

- launch specification construction;
- process supervision;
- timeout handling;
- restart policy;
- runtime state transitions;
- ACP event translation.

The runtime should not duplicate protocol testing already provided by the ACP SDK.

# Design Principle

The guiding principle is:

> **The ACP SDK owns the protocol. NateOhaAcpClient owns runtime integration. `nate_ntm` owns orchestration.**

Whenever functionality already exists within the maintained ACP SDK, the runtime should compose with it rather than reimplement it. This keeps `nate_ntm` focused on scheduling, supervision, persistence, and event handling while delegating protocol mechanics to the upstream library.
