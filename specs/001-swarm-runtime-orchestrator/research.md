# Research & Design Decisions: nate_ntm Swarm Runtime Orchestrator

This document captures key technical decisions and rationale made during `/speckit.plan` for the nate_ntm Swarm Runtime Orchestrator. It serves as a reference for why particular technologies and patterns were chosen, and what alternatives were considered.

## 1. Language and Runtime Model

- **Decision**: Implement the runtime as a Python 3.11 asyncio-based daemon.
- **Rationale**:
  - Aligns with OpenHands and related tooling, which are primarily Python-based.
  - Python 3.11 provides mature `asyncio` support and good ergonomics for event-driven daemons.
  - Easier to prototype and iterate on orchestration logic, especially around subprocess management and I/O-bound operations.
- **Alternatives Considered**:
  - **Rust**: Strong safety and performance, but higher initial implementation overhead and slower iteration for early-stage experimentation.
  - **Go**: Good for concurrency and daemons, but would increase integration friction with existing Python-based OpenHands and Agent Mail tooling.

## 2. Runtime Control API Transport

- **Decision**: Expose a localhost-only, bidirectional control API using a JSON-RPC-style request/response protocol over a local TCP socket.
- **Rationale**:
  - JSON-RPC is simple, widely understood, and maps naturally to CLI/TUI/web clients.
  - Local TCP socket keeps the transport decoupled from stdio and from any particular HTTP framework while still being easy to work with.
  - Restricting to `localhost` aligns with the MVP security/trust boundary and is consistent with the spec.
- **Alternatives Considered**:
  - **HTTP/REST**: More tooling support, but tends to encourage heavier frameworks and more surface area than needed for a local control API.
  - **gRPC**: Strong contracts and streaming support, but higher complexity and less convenient for quick CLI/TUI integrations.
  - **stdio-only control**: Simpler wiring for a single client, but awkward for multiple concurrent clients and long-lived daemons.

## 3. Swarm Metadata Persistence

- **Decision**: Store swarm metadata in project-local files under a dedicated directory (for example `.nate_ntm/`) using human-readable structured formats (JSON or YAML/TOML).
- **Rationale**:
  - Matches the spec requirement that metadata be project-local and file-based in the MVP.
  - Easy to inspect, edit, and reset during development.
  - Keeps runtime deployment simple (no external DB) while still allowing structured data and future migrations.
- **Alternatives Considered**:
  - **SQLite or embedded DB**: Would provide stronger querying and transactional guarantees but adds operational and dependency overhead not needed for the MVP.
  - **External DB/service**: Overkill for single-host MVP; complicates setup and contradicts the spec constraint of project-local file storage.

## 4. Event Model and Scheduler

- **Decision**: Model the core of the runtime as an event-driven loop (Runtime Event Loop / Scheduler) that processes heterogeneous runtime events and decides when to initiate new ACP turns.
- **Rationale**:
  - Accurately reflects the spec: the runtime reacts to Agent Mail changes, ACP events, subprocess lifecycle changes, user commands, timers, and shutdown signals.
  - Provides a clean place to centralize scheduling policies and backoff strategies without tying them directly to any one event source.
  - Maps naturally onto Python's `asyncio` event loop and task primitives.
- **Alternatives Considered**:
  - **Poll-only scheduler** (mail-centric loop): Simpler conceptually, but misrepresents the real responsibilities and makes it harder to add new event sources.
  - **Multiple loosely coordinated loops**: Could improve modularity but risks subtle race conditions and more complex state management for an early MVP.

## 5. Agent Event Stream Abstraction

- **Decision**: Represent recent control-protocol events per agent as an in-memory "Agent Event Stream" abstraction rather than baking in a specific data structure such as a ring buffer.
- **Rationale**:
  - The spec requires a transient, bounded view of recent events for inspection and debugging, not a particular container.
  - An abstract event stream allows implementation flexibility (ring buffer, deque, mmap-backed buffer, etc.) without affecting APIs.
  - Keeps options open for future experimentation with more advanced buffering or shared-memory strategies.
- **Alternatives Considered**:
  - **Fixed ring buffer as a hard requirement**: Simple and effective, but needlessly constrains future implementation choices.
  - **Unbounded event history in memory**: Simplifies implementation but risks unbounded memory growth and blurs the line between transient buffer and durable history.

## 6. Integration Surfaces

- **Decision**: Treat the following as primary integration boundaries:
  - Runtime ↔ Agent Mail (mailbox polling and message acknowledgements)
  - Runtime ↔ OpenHands ACP (turn execution, tool calls, error reporting)
  - Runtime ↔ Runtime API Clients (CLI/TUI/web over the local control API)
- **Rationale**:
  - Clear separation of concerns allows swapping or mocking each integration in tests.
  - Supports building thin adapters around `mcp_agent_mail` and ACP clients while keeping the runtime core independent of specific HTTP or MCP libraries.
- **Alternatives Considered**:
  - **Hardwiring the runtime directly to a specific Agent Mail or ACP implementation**: Faster to start, but would make future migrations or alternative integrations more painful.

## 7. Open Questions / To Revisit Later

These items are intentionally left flexible for future iterations and are **not** required to proceed with the MVP implementation:

- Exact JSON-RPC schema details (error codes, subscription mechanics, and batching support).
- Concrete file formats and schemas under `.nate_ntm/` (JSON vs YAML/TOML, naming conventions, and migration strategy).
- Specific logging and tracing framework selection beyond the Python standard library's `logging` module.

These will be refined during `/speckit.tasks` and `/speckit.implement` as the codebase takes shape and early usage provides feedback.
