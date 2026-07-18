# Feature Specification: Managed Swarm Runtime

**Feature Branch**: `[006-managed-swarm-runtime]`
**Created**: 2026-07-14
**Status**: Draft

## Overview

Introduce a first-class `ManagedSwarm` abstraction that represents one configured and potentially running swarm as a coherent application object.

Swarm behavior is currently distributed across several specialized components:

- `SwarmMetadata`
- `AgentMetadata`
- `RuntimeState`
- `AgentRuntimeState`
- `MetadataStore`
- `NateOhaAcpClient`
- `AgentSupervisor`
- `RuntimeScheduler`
- optional Agent Mail integration
- `RuntimeDaemon`

These components have legitimate and distinct responsibilities, but callers must currently coordinate them manually to perform application-level operations such as:

- creating a swarm;
- resuming persisted agents;
- starting all agents;
- persisting ACP session identifiers;
- prompting an agent;
- inspecting agent state;
- subscribing to events;
- stopping all owned resources.

`ManagedSwarm` will become the swarm-level aggregate responsible for coordinating these components and enforcing invariants across them.

The class must remain a relatively thin orchestration layer. It must not reimplement ACP, persistence, scheduler policy, process management, or Agent Mail protocol behavior.

------------------------------------------------------------------------

# Goals

The feature must provide one coherent object through which application code can:

- create a new swarm;
- resume a persisted swarm;
- start and stop the swarm;
- start, stop, restart, prompt, interrupt, and inspect individual agents;
- observe events from one or more agents;
- maintain consistency between persisted metadata and runtime state;
- coordinate optional swarm-level integrations;
- shut down all owned resources deterministically.

The abstraction must make the normal application lifecycle straightforward:

```
swarm = await ManagedSwarm.create(
    config=config,
    agents=agent_definitions,
)

await swarm.start()

await swarm.prompt_agent(
    “navigator”,
    “Inspect the repository and identify the highest-priority task.”,
)

detail = swarm.get_agent_detail(“navigator”)

await swarm.shutdown()
```

Resume must be similarly direct:

```
swarm = await ManagedSwarm.resume(config=config)
await swarm.start()
```

------------------------------------------------------------------------

# Non-Goals

This feature does not:

- replace `NateOhaAcpClient`;
- implement or reinterpret the ACP protocol;
- replace `MetadataStore`;
- introduce a second durable conversation or event store;
- redesign nate-oha session semantics;
- distinguish replayed events from newly generated events;
- implement a new scheduling algorithm;
- introduce distributed swarm execution;
- support multiple independent swarms in one `ManagedSwarm` instance;
- restore fake ACP or fake Agent Mail implementations;
- expose private ACP connection objects as the primary public API.

------------------------------------------------------------------------

# User Scenarios and Testing

## User Story 1 — Create a managed swarm

An operator creates a swarm from runtime configuration and a set of agent definitions.

**Priority**: P1

### Expected behavior

`ManagedSwarm.create()`:

- validates the requested agent definitions;
- creates swarm and agent metadata;
- creates runtime state;
- initializes required dependencies;
- returns a swarm object in a non-running lifecycle state;
- does not launch processes until `start()` is called.

### Independent test

Create a two-agent echo-mode swarm in a temporary project.

Verify:

- one `SwarmMetadata` record is persisted;
- one `AgentMetadata` record exists per definition;
- agent IDs are unique;
- no ACP process is launched before `start()`;
- the returned swarm reports `CREATED`.

------------------------------------------------------------------------

## User Story 2 — Start the entire swarm

An operator starts all configured agents through one swarm-level operation.

**Priority**: P1

### Expected behavior

`ManagedSwarm.start()`:

- transitions the swarm to `STARTING`;
- starts scheduler infrastructure;
- launches or resumes every configured agent;
- updates per-agent runtime state;
- persists ACP-assigned conversation IDs;
- transitions the swarm to `RUNNING` when startup succeeds.

### Independent test

Start a two-agent swarm using real nate-oha echo-mode subprocesses.

Verify:

- both agents are started;
- each agent receives an ACP-owned conversation ID;
- each ID is persisted;
- each agent has one active ACP session;
- swarm status becomes `RUNNING`;
- calling `start()` again is idempotent.

------------------------------------------------------------------------

## User Story 3 — Resume a persisted swarm

An operator reconstructs a swarm from project-local metadata.

**Priority**: P1

### Expected behavior

`ManagedSwarm.resume()`:

- loads swarm metadata;
- loads and validates all agent metadata;
- reconstructs transient runtime state;
- preserves persisted ACP conversation IDs;
- returns an unstarted swarm;
- resumes agents when `start()` is called.

### Independent test

Create a swarm, start it, send identifiable prompts, shut it down, then construct a new `ManagedSwarm` through `resume()`.

Verify:

- membership is restored;
- conversation IDs are unchanged;
- existing nate-oha conversations resume;
- prior conversation history flows through the normal event stream;
- new prompts continue the same conversation;
- no replacement IDs are synthesized.

------------------------------------------------------------------------

## User Story 4 — Operate on one agent

A caller manages one agent without reaching directly into the ACP client, scheduler, supervisor, or metadata store.

**Priority**: P2

### Supported operations

- start;
- stop;
- restart;
- prompt;
- interrupt;
- inspect;
- retrieve recent events.

### Independent test

With a running two-agent swarm:

1. stop agent A;
2. verify agent B remains active;
3. restart agent A;
4. prompt agent A;
5. inspect its status and recent events;
6. verify swarm state remains consistent.

------------------------------------------------------------------------

## User Story 5 — Observe swarm events

A caller subscribes to live events from all agents or a selected subset.

**Priority**: P2

### Expected behavior

Subscriptions:

- are registered before control returns to the caller;
- receive events without polling;
- use independent bounded queues;
- preserve agent identity;
- close when the subscription exits or the swarm shuts down;
- do not interfere with the bounded event history used for inspection.

### Independent test

Subscribe to a two-agent swarm before starting it.

Verify:

- startup and ACP events from both agents arrive;
- filtering by agent ID works;
- multiple subscribers independently receive the same event;
- cancellation removes the subscription;
- swarm shutdown terminates active event streams.

------------------------------------------------------------------------

## User Story 6 — Shut down the swarm

An operator shuts down all active agents and swarm-owned resources through one operation.

**Priority**: P1

### Expected behavior

`ManagedSwarm.shutdown()`:

- is idempotent;
- stops new scheduling;
- attempts to stop every active agent;
- closes active swarm subscriptions;
- persists final metadata updates;
- transitions to `STOPPED`;
- aggregates multiple shutdown failures rather than abandoning later cleanup.

### Independent test

Start three agents and make one agent fail during shutdown.

Verify:

- shutdown is attempted for all three;
- successful agents stop;
- subscriptions close;
- the swarm reports a meaningful aggregate failure;
- a second shutdown call does not repeat completed work or raise unrelated errors.

------------------------------------------------------------------------

## User Story 7 — Run without Agent Mail

An operator runs a complete swarm while Agent Mail is disabled.

**Priority**: P2

### Independent test

Create, start, prompt, shut down, resume, and restart a swarm with no Agent Mail server available.

Verify:

- no Agent Mail connection is attempted;
- Agent Mail project or identity values are not required;
- all ACP lifecycle behavior remains functional.

------------------------------------------------------------------------

## User Story 8 — Run with Agent Mail enabled

An operator creates or resumes a swarm using the real Agent Mail integration.

**Priority**: P3

### Independent test

Against a live `mcp_agent_mail` service:

- create or reuse the project;
- create or reuse per-agent identities;
- persist identity and credential metadata;
- pass Agent Mail settings into nate-oha launch specifications;
- resume without replacing established identities.

------------------------------------------------------------------------

# Proposed Public API

The public abstraction is:

```
class ManagedSwarm:
    …
```

A representative interface follows.

```
from __future__ import annotations

from collections.abc import (
    AsyncIterator,
    Collection,
    Mapping,
    Sequence,
)
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

class ManagedSwarm:
    @classmethod
    async def create(
        cls,
        *,
        config: RuntimeConfig,
        agents: Sequence[AgentDefinition],
        dependencies: SwarmDependencies | None = None,
    ) -> ManagedSwarm:
        “""Create and persist a new swarm without starting its agents.”""
        …

    @classmethod
    async def resume(
        cls,
        *,
        config: RuntimeConfig,
        dependencies: SwarmDependencies | None = None,
    ) -> ManagedSwarm:
        “""Load a persisted swarm without starting its agents.”""
        …

    async def start(self) -> None:
        “""Start or resume every configured agent.”""
        …

    async def shutdown(
        self,
        *,
        timeout: float | None = None,
    ) -> None:
        “""Stop all agents and release swarm-owned resources.”""
        …

    async def start_agent(
        self,
        agent_id: str,
    ) -> AgentRuntimeState:
        “""Start or resume one configured agent.”""
        …

    async def stop_agent(
        self,
        agent_id: str,
        *,
        timeout: float | None = None,
    ) -> None:
        “""Stop one active agent without affecting its peers.”""
        …

    async def restart_agent(
        self,
        agent_id: str,
        *,
        timeout: float | None = None,
    ) -> AgentRuntimeState:
        “""Stop and restart one configured agent.”""
        …

    async def prompt_agent(
        self,
        agent_id: str,
        prompt: str,
    ) -> None:
        “""Send a prompt to one active agent.”""
        …

    async def interrupt_agent(
        self,
        agent_id: str,
    ) -> None:
        “""Interrupt work currently executing on one agent.”""
        …

    def get_agent(
        self,
        agent_id: str,
    ) -> AgentRuntimeState:
        “""Return the transient state for one configured agent.”""
        …

    def get_agent_metadata(
        self,
        agent_id: str,
    ) -> AgentMetadata:
        “""Return persisted metadata for one configured agent.”""
        …

    def get_agent_detail(
        self,
        agent_id: str,
        *,
        max_events: int = 50,
    ) -> AgentDetail:
        “""Return a combined persisted and transient agent view.”""
        …

    def list_agents(self) -> tuple[AgentSummary, …]:
        “""Return summaries for all configured agents.”""
        …

    def get_status(self) -> SwarmStatus:
        “""Return a swarm-level lifecycle and health snapshot.”""
        …

    @asynccontextmanager
    async def subscribe_events(
        self,
        *,
        agent_ids: Collection[str] | None = None,
        include_runtime: bool = True,
    ) -> AsyncIterator[AsyncIterator[AgentEvent]]:
        “""Subscribe to live swarm events.”""
        …
```

The exact return models may follow existing runtime API models where appropriate.

------------------------------------------------------------------------

# Suggested Data Types

## AgentDefinition

Represents requested membership when creating a swarm.

```
@dataclass(frozen=True, slots=True)
class AgentDefinition:
    agent_id: str
    display_name: str
    prompt_soul_content: str | None = None
```

Caller-provided definitions must not assign:

- ACP conversation IDs;
- Agent Mail credentials;
- process IDs;
- runtime status.

Those values are owned by the relevant runtime integrations.

------------------------------------------------------------------------

## SwarmDependencies

Contains replaceable infrastructure dependencies.

```
@dataclass(slots=True)
class SwarmDependencies:
    metadata_store: MetadataStore
    acp_client: NateOhaAcpClient
    agent_mail_client: BaseAgentMailClient | None
    agent_supervisor: AgentSupervisor
    scheduler: RuntimeScheduler
```

Production construction should normally use:

```
dependencies = create_swarm_dependencies(
    config=config,
    metadata=metadata,
    state=state,
)
```

Only genuine external or lifecycle boundaries should be injected.

Do not add interfaces solely to make simple domain objects mockable.

------------------------------------------------------------------------

## SwarmLifecycleStatus

Reuse `RuntimeStatus` if it already accurately represents swarm lifecycle semantics.

Otherwise introduce:

```
class SwarmLifecycleStatus(str, Enum):
    CREATED = “Created"
    STARTING = “Starting"
    RUNNING = “Running"
    DEGRADED = “Degraded"
    SHUTTING_DOWN = “ShuttingDown"
    STOPPED = “Stopped"
    FAILED = “Failed"
```

Do not maintain two overlapping enums indefinitely.

------------------------------------------------------------------------

## SwarmStatus

```
@dataclass(frozen=True, slots=True)
class SwarmStatus:
    swarm_id: str
    lifecycle: SwarmLifecycleStatus
    total_agents: int
    running_agents: int
    failed_agents: int
    stopped_agents: int
    last_error: str | None = None
```

------------------------------------------------------------------------

## AgentSummary

```
@dataclass(frozen=True, slots=True)
class AgentSummary:
    agent_id: str
    display_name: str
    status: AgentStatus
    conversation_id: str | None
    last_error: str | None
```

------------------------------------------------------------------------

## AgentDetail

```
@dataclass(frozen=True, slots=True)
class AgentDetail:
    metadata: AgentMetadata
    runtime: AgentRuntimeState
    recent_events: tuple[AgentEvent, …]
```

If current API contracts require dictionary payloads, conversion should occur at the API boundary:

```
payload = agent_detail.to_dict()
```

The domain object should not be shaped solely around JSON serialization.

------------------------------------------------------------------------

# High-Level Class Scaffolding

The following illustrates the intended structure and delegation boundaries.

It is not a complete implementation.

```
from __future__ import annotations

import asyncio

from collections.abc import AsyncIterator, Collection, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

@dataclass(slots=True)
class ManagedSwarm:
    config: RuntimeConfig
    metadata: SwarmMetadata
    state: RuntimeState
    metadata_store: MetadataStore
    acp_client: NateOhaAcpClient
    agent_supervisor: AgentSupervisor
    scheduler: RuntimeScheduler
    agent_mail_client: BaseAgentMailClient | None = None

    _lifecycle_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
    )

    _started: bool = field(
        default=False,
        init=False,
        repr=False,
    )

    _shutdown_started: bool = field(
        default=False,
        init=False,
        repr=False,
    )

    @classmethod
    async def create(
        cls,
        *,
        config: RuntimeConfig,
        agents: Sequence[AgentDefinition],
        dependencies: SwarmDependencies | None = None,
    ) -> ManagedSwarm:
        cls._validate_agent_definitions(agents)

        metadata_store = (
            dependencies.metadata_store
            if dependencies is not None
            else MetadataStore(config=config)
        )

        metadata = await cls._create_metadata(
            config=config,
            agents=agents,
            metadata_store=metadata_store,
        )

        state = cls._build_initial_state(
            config=config,
            metadata=metadata,
        )

        deps = dependencies or create_swarm_dependencies(
            config=config,
            metadata=metadata,
            state=state,
        )

        return cls(
            config=config,
            metadata=metadata,
            state=state,
            metadata_store=deps.metadata_store,
            acp_client=deps.acp_client,
            agent_supervisor=deps.agent_supervisor,
            scheduler=deps.scheduler,
            agent_mail_client=deps.agent_mail_client,
        )

    @classmethod
    async def resume(
        cls,
        *,
        config: RuntimeConfig,
        dependencies: SwarmDependencies | None = None,
    ) -> ManagedSwarm:
        metadata_store = (
            dependencies.metadata_store
            if dependencies is not None
            else MetadataStore(config=config)
        )

        metadata = metadata_store.load_swarm_metadata()

        cls._validate_persisted_metadata(
            config=config,
            metadata=metadata,
            metadata_store=metadata_store,
        )

        state = cls._build_initial_state(
            config=config,
            metadata=metadata,
        )

        deps = dependencies or create_swarm_dependencies(
            config=config,
            metadata=metadata,
            state=state,
        )

        return cls(
            config=config,
            metadata=metadata,
            state=state,
            metadata_store=deps.metadata_store,
            acp_client=deps.acp_client,
            agent_supervisor=deps.agent_supervisor,
            scheduler=deps.scheduler,
            agent_mail_client=deps.agent_mail_client,
        )

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._started:
                return

            if self._shutdown_started:
                raise SwarmStartupError(
                    “Cannot start a swarm after shutdown has begun"
                )

            self.state.status = RuntimeStatus.STARTING

            try:
                await self._prepare_integrations()
                self.scheduler.start()

                await self._start_all_agents()

            except Exception as exc:
                self.state.status = RuntimeStatus.FAILED
                await self._rollback_failed_startup()
                raise SwarmStartupError(
                    f"Failed to start swarm {self.metadata.swarm_id!r}"
                ) from exc

            self.state.status = RuntimeStatus.RUNNING
            self._started = True

    async def shutdown(
        self,
        *,
        timeout: float | None = None,
    ) -> None:
        async with self._lifecycle_lock:
            if self.state.status is RuntimeStatus.STOPPED:
                return

            self._shutdown_started = True
            self.state.shutdown_requested = True
            self.state.status = RuntimeStatus.SHUTTING_DOWN

            errors: list[BaseException] = []

            try:
                self.scheduler.stop()
            except Exception as exc:
                errors.append(exc)

            agent_ids = tuple(self.metadata.agents)

            results = await asyncio.gather(
                *(
                    self._stop_agent_for_shutdown(
                        agent_id,
                        timeout=timeout,
                    )
                    for agent_id in agent_ids
                ),
                return_exceptions=True,
            )

            errors.extend(
                result
                for result in results
                if isinstance(result, BaseException)
            )

            self._close_swarm_event_subscriptions()
            self._persist_swarm_metadata()

            self.state.status = (
                RuntimeStatus.FAILED
                if errors
                else RuntimeStatus.STOPPED
            )

            self._started = False

            if errors:
                raise SwarmShutdownError.from_errors(
                    swarm_id=self.metadata.swarm_id,
                    errors=errors,
                )

    async def start_agent(
        self,
        agent_id: str,
    ) -> AgentRuntimeState:
        metadata = self._require_agent_metadata(agent_id)

        runtime = self._ensure_agent_runtime_state(agent_id)
        runtime.status = AgentStatus.STARTING
        runtime.last_error = None

        try:
            await self.acp_client.start_agent_async(
                agent_id,
                metadata=metadata,
            )

            persisted = self.metadata_store.load_agent_metadata(
                agent_id
            )

            self.metadata.agents[agent_id] = persisted
            runtime.status = AgentStatus.IDLE

        except Exception as exc:
            runtime.status = AgentStatus.FAILED
            runtime.last_error = str(exc)
            raise AgentStartupError(agent_id) from exc

        return runtime

    async def stop_agent(
        self,
        agent_id: str,
        *,
        timeout: float | None = None,
    ) -> None:
        runtime = self._require_agent_runtime_state(agent_id)

        await self.acp_client.stop_agent_async(
            agent_id,
            timeout=timeout or self.config.shutdown_timeout,
        )

        runtime.status = AgentStatus.STOPPED

    async def restart_agent(
        self,
        agent_id: str,
        *,
        timeout: float | None = None,
    ) -> AgentRuntimeState:
        await self.stop_agent(
            agent_id,
            timeout=timeout,
        )

        return await self.start_agent(agent_id)

    async def prompt_agent(
        self,
        agent_id: str,
        prompt: str,
    ) -> None:
        runtime = self._require_active_agent(agent_id)
        runtime.status = AgentStatus.RUNNING

        try:
            await self.acp_client.prompt(
                agent_id,
                prompt,
            )
        except Exception as exc:
            runtime.status = AgentStatus.FAILED
            runtime.last_error = str(exc)
            raise AgentPromptError(agent_id) from exc

    async def interrupt_agent(
        self,
        agent_id: str,
    ) -> None:
        self._require_active_agent(agent_id)
        await self.acp_client.interrupt(agent_id)

    def get_agent(
        self,
        agent_id: str,
    ) -> AgentRuntimeState:
        return self._require_agent_runtime_state(agent_id)

    def get_agent_metadata(
        self,
        agent_id: str,
    ) -> AgentMetadata:
        return self._require_agent_metadata(agent_id)

    def list_agents(self) -> tuple[AgentSummary, …]:
        return tuple(
            self._build_agent_summary(agent_id)
            for agent_id in self.metadata.agents
        )

    def get_agent_detail(
        self,
        agent_id: str,
        *,
        max_events: int = 50,
    ) -> AgentDetail:
        metadata = self._require_agent_metadata(agent_id)
        runtime = self._require_agent_runtime_state(agent_id)

        events = (
            runtime.event_stream.get_events(max_events)
            if runtime.event_stream is not None
            else []
        )

        return AgentDetail(
            metadata=metadata,
            runtime=runtime,
            recent_events=tuple(events),
        )

    def get_status(self) -> SwarmStatus:
        summaries = self.list_agents()

        return SwarmStatus(
            swarm_id=self.metadata.swarm_id,
            lifecycle=self.state.status,
            total_agents=len(summaries),
            running_agents=sum(
                agent.status
                in {
                    AgentStatus.IDLE,
                    AgentStatus.RUNNING,
                    AgentStatus.WAITING,
                }
                for agent in summaries
            ),
            failed_agents=sum(
                agent.status is AgentStatus.FAILED
                for agent in summaries
            ),
            stopped_agents=sum(
                agent.status is AgentStatus.STOPPED
                for agent in summaries
            ),
        )

    @asynccontextmanager
    async def subscribe_events(
        self,
        *,
        agent_ids: Collection[str] | None = None,
        include_runtime: bool = True,
    ) -> AsyncIterator[AsyncIterator[AgentEvent]]:
        subscription = self._create_swarm_subscription(
            agent_ids=agent_ids,
            include_runtime=include_runtime,
        )

        try:
            yield subscription.iter_events()
        finally:
            await subscription.close()

    async def _start_all_agents(self) -> None:
        agent_ids = tuple(self.metadata.agents)

        results = await asyncio.gather(
            *(self.start_agent(agent_id) for agent_id in agent_ids),
            return_exceptions=True,
        )

        failures = {
            agent_id: result
            for agent_id, result in zip(agent_ids, results)
            if isinstance(result, BaseException)
        }

        if failures:
            raise SwarmAgentStartupError(failures)

    async def _prepare_integrations(self) -> None:
        if self.agent_mail_client is None:
            return

        self._prepare_agent_mail_project()
        self._prepare_agent_mail_identities()

    async def _rollback_failed_startup(self) -> None:
        active_agent_ids = tuple(
            agent_id
            for agent_id, runtime in self.state.agents.items()
            if runtime.status
            in {
                AgentStatus.STARTING,
                AgentStatus.IDLE,
                AgentStatus.RUNNING,
                AgentStatus.WAITING,
            }
        )

        await asyncio.gather(
            *(
                self.acp_client.stop_agent_async(
                    agent_id,
                    timeout=self.config.shutdown_timeout,
                )
                for agent_id in active_agent_ids
            ),
            return_exceptions=True,
        )

    @staticmethod
    def _validate_agent_definitions(
        agents: Sequence[AgentDefinition],
    ) -> None:
        …

    @staticmethod
    def _validate_persisted_metadata(
        *,
        config: RuntimeConfig,
        metadata: SwarmMetadata,
        metadata_store: MetadataStore,
    ) -> None:
        …

    @staticmethod
    def _build_initial_state(
        *,
        config: RuntimeConfig,
        metadata: SwarmMetadata,
    ) -> RuntimeState:
        …

    @classmethod
    async def _create_metadata(
        cls,
        *,
        config: RuntimeConfig,
        agents: Sequence[AgentDefinition],
        metadata_store: MetadataStore,
    ) -> SwarmMetadata:
        …

    def _require_agent_metadata(
        self,
        agent_id: str,
    ) -> AgentMetadata:
        …

    def _require_agent_runtime_state(
        self,
        agent_id: str,
    ) -> AgentRuntimeState:
        …

    def _require_active_agent(
        self,
        agent_id: str,
    ) -> AgentRuntimeState:
        …

    def _ensure_agent_runtime_state(
        self,
        agent_id: str,
    ) -> AgentRuntimeState:
        …

    def _build_agent_summary(
        self,
        agent_id: str,
    ) -> AgentSummary:
        …

    def _persist_swarm_metadata(self) -> None:
        …

    def _prepare_agent_mail_project(self) -> None:
        …

    def _prepare_agent_mail_identities(self) -> None:
        …

    def _create_swarm_subscription(
        self,
        *,
        agent_ids: Collection[str] | None,
        include_runtime: bool,
    ) -> SwarmEventSubscription:
        …

    def _close_swarm_event_subscriptions(self) -> None:
        …
```

------------------------------------------------------------------------

# Core Responsibilities

## Lifecycle coordination

`ManagedSwarm` owns the application-level lifecycle:

```
create or resume
→ validate metadata
→ construct transient state
→ construct dependencies
→ prepare optional integrations
→ start scheduler
→ start or resume agents
→ running
```

Shutdown is similarly coordinated:

```
shutdown requested
→ stop scheduler
→ stop every agent
→ close subscriptions
→ persist final metadata
→ stopped or failed
```

------------------------------------------------------------------------

## Invariant enforcement

`ManagedSwarm` must enforce:

- every `agent_id` is unique;
- every configured agent has one persisted metadata record;
- every configured agent has one transient state record;
- each agent has at most one active ACP session;
- ACP conversation IDs are opaque and ACP-owned;
- newly assigned ACP IDs are persisted before startup succeeds;
- persisted membership and transient membership remain aligned;
- Agent Mail is contacted only when enabled;
- swarm shutdown attempts cleanup for every active agent;
- client-facing operations reject unknown or inactive agents clearly.

------------------------------------------------------------------------

## Event ownership

Two event concepts must remain distinct.

### Bounded inspection history

`AgentEventStream` remains the per-agent in-memory history used by:

- inspection;
- status detail;
- API snapshots;
- debugging.

### Live async delivery

`ManagedSwarm.subscribe_events()` provides live event delivery.

It must:

- use one queue per subscriber;
- support filtering by agent ID;
- preserve event ordering as observed by the swarm;
- use a bounded queue;
- define an overflow policy;
- terminate when the subscription exits;
- terminate when the swarm shuts down.

It must not poll `AgentEventStream`.

------------------------------------------------------------------------

# Component Boundaries

## NateOhaAcpClient

Continues to own:

- nate-oha subprocess creation;
- ACP connection setup;
- ACP session creation and resume;
- prompt and interrupt operations;
- one-agent shutdown;
- per-agent ACP event production.

`ManagedSwarm` delegates to it.

------------------------------------------------------------------------

## MetadataStore

Continues to own:

- filesystem layout;
- serialization;
- atomic writes;
- metadata loading;
- malformed or missing metadata errors.

`ManagedSwarm` determines when metadata must be read or written.

------------------------------------------------------------------------

## AgentSupervisor

Continues to own:

- per-agent state transitions caused by runtime events;
- bounded per-agent event history;
- translation from process/ACP events into `AgentRuntimeState`.

`ManagedSwarm` owns the supervisor and invokes higher-level lifecycle operations through it where appropriate.

------------------------------------------------------------------------

## RuntimeScheduler

Continues to own:

- work eligibility;
- scheduling policy;
- work dispatch decisions;
- restart policy where explicitly assigned.

It does not own creation, persistence, or whole-swarm shutdown.

------------------------------------------------------------------------

## RuntimeDaemon

Becomes the outer process host.

It should own:

- signal handling;
- the runtime control server;
- daemon startup and shutdown;
- one `ManagedSwarm`.

Runtime API handlers should delegate to `ManagedSwarm`.

------------------------------------------------------------------------

# Functional Requirements

## Construction and identity

- **FR-001**: The system must expose a first-class `ManagedSwarm`.
- **FR-002**: One instance must represent exactly one swarm.
- **FR-003**: The class must support new-swarm creation.
- **FR-004**: The class must support persisted-swarm resume.
- **FR-005**: `create()` and `resume()` must not launch agents automatically unless explicitly documented otherwise.
- **FR-006**: Agent membership must be derived from `SwarmMetadata`.
- **FR-007**: Duplicate agent IDs must be rejected before metadata is persisted.

## State and metadata

- **FR-008**: `ManagedSwarm` must own one `RuntimeState`.
- **FR-009**: `ManagedSwarm` must own one `SwarmMetadata` aggregate.
- **FR-010**: Every persisted agent must have a corresponding runtime-state entry.
- **FR-011**: ACP conversation IDs must remain opaque and ACP-owned.
- **FR-012**: Newly returned ACP conversation IDs must be persisted before agent startup is considered successful.

## Swarm lifecycle

- **FR-013**: `start()` must be asynchronous.
- **FR-014**: `start()` must be idempotent after successful startup.
- **FR-015**: `shutdown()` must be asynchronous and idempotent.
- **FR-016**: Swarm status must expose deterministic lifecycle states.
- **FR-017**: Startup failure must transition the swarm to a failed state.
- **FR-018**: Failed startup must attempt to stop agents already started.
- **FR-019**: Shutdown must attempt to stop every active agent.
- **FR-020**: Multiple shutdown errors must be aggregated.

## Agent operations

- **FR-021**: Callers must be able to start one configured agent.
- **FR-022**: Callers must be able to stop one active agent.
- **FR-023**: Callers must be able to restart one configured agent.
- **FR-024**: Callers must be able to prompt one active agent.
- **FR-025**: Callers must be able to interrupt one active agent.
- **FR-026**: Stopping one agent must not stop its peers.
- **FR-027**: Operations on unknown agents must raise an actionable domain error.

## Inspection

- **FR-028**: Callers must be able to list all configured agents.
- **FR-029**: Callers must be able to inspect persisted metadata.
- **FR-030**: Callers must be able to inspect transient runtime state.
- **FR-031**: Callers must be able to retrieve bounded recent events.
- **FR-032**: API serialization must remain outside the core aggregate where practical.

## Events

- **FR-033**: The swarm must expose a live async event subscription.
- **FR-034**: Subscriptions must be registered before control returns to callers.
- **FR-035**: Subscriptions must support all-agent and selected-agent filtering.
- **FR-036**: Multiple subscribers must independently receive the same event.
- **FR-037**: Subscription queues must be bounded.
- **FR-038**: Queue overflow behavior must be explicit and observable.
- **FR-039**: Subscription cancellation must release its queue.
- **FR-040**: Swarm shutdown must terminate active event subscriptions.

## Agent Mail

- **FR-041**: Agent Mail must remain optional.
- **FR-042**: No Agent Mail request may occur when the feature is disabled.
- **FR-043**: When enabled, swarm project initialization must occur before agent startup.
- **FR-044**: When enabled, per-agent identities must be created or restored before nate-oha launch.
- **FR-045**: No fake Agent Mail fallback may be introduced.

## Delegation

- **FR-046**: ACP process and protocol mechanics must be delegated to `NateOhaAcpClient`.
- **FR-047**: Storage mechanics must be delegated to `MetadataStore`.
- **FR-048**: Per-agent event projection must remain delegated to `AgentSupervisor`.
- **FR-049**: Scheduling policy must remain delegated to `RuntimeScheduler`.
- **FR-050**: `RuntimeDaemon` must delegate swarm operations through `ManagedSwarm`.

------------------------------------------------------------------------

# 
# Testing Strategy

## Unit tests

Use focused unit tests for swarm-owned behavior:

- duplicate definition rejection;
- metadata/runtime membership consistency;
- lifecycle state transitions;
- idempotent start and shutdown;
- startup rollback;
- shutdown error aggregation;
- unknown-agent validation;
- swarm event filtering;
- multi-subscriber broadcast;
- cancellation cleanup;
- queue overflow policy;
- Agent Mail-disabled behavior.

Use temporary real filesystem paths for metadata tests.

Avoid mocking simple data structures.

Dependency injection is appropriate for tests of coordination failure paths where using a real subprocess or service would obscure the swarm-owned behavior being tested.

------------------------------------------------------------------------

## Integration tests

Use real nate-oha echo-mode subprocesses for:

- one-agent startup;
- two-agent startup;
- ACP session-ID persistence;
- prompt delivery;
- one-agent stop and restart;
- full-swarm shutdown;
- create → shutdown → resume;
- conversation replay and continuation;
- live event aggregation across multiple agents.

Use the real Agent Mail service for enabled-mode tests.

------------------------------------------------------------------------

## End-to-end acceptance test

One acceptance scenario should demonstrate:

```
ManagedSwarm.create
→ start two agents
→ subscribe to all swarm events
→ prompt both agents
→ inspect both agents
→ stop and restart one agent
→ shut down
→ ManagedSwarm.resume
→ start
→ observe prior conversation history
→ prompt both agents again
→ shut down cleanly
```

The test must use:

- real `ManagedSwarm`;
- real `MetadataStore`;
- real `NateOhaAcpClient`;
- real nate-oha echo-mode subprocesses;
- no arbitrary event-related sleeps;
- event-driven subscriptions.

------------------------------------------------------------------------

# Migration Strategy

## Phase 1 — Introduce ManagedSwarm

Add the class while preserving current daemon behavior.

Compose existing:

- `RuntimeState`;
- `SwarmMetadata`;
- `MetadataStore`;
- `AgentSupervisor`;
- `RuntimeScheduler`;
- `NateOhaAcpClient`;
- optional Agent Mail client.

------------------------------------------------------------------------

## Phase 2 — Move swarm lifecycle coordination

Move from `RuntimeDaemon` into `ManagedSwarm`:

- swarm metadata creation and loading;
- transient state reconstruction;
- Agent Mail project and identity preparation;
- all-agent startup;
- all-agent shutdown;
- conversation-ID persistence coordination;
- agent detail assembly.

------------------------------------------------------------------------

## Phase 3 — Delegate runtime APIs

Update runtime API handlers to call:

- `ManagedSwarm.get_status`;
- `ManagedSwarm.list_agents`;
- `ManagedSwarm.get_agent_detail`;
- `ManagedSwarm.prompt_agent`;
- `ManagedSwarm.interrupt_agent`;
- `ManagedSwarm.shutdown`;
- `ManagedSwarm.subscribe_events`.

------------------------------------------------------------------------

## Phase 4 — Remove duplicate lifecycle paths

Delete daemon and scheduler code that independently:

- constructs swarm membership;
- launches all agents;
- persists ACP identifiers;
- aggregates shutdown;
- coordinates optional integrations;
- assembles agent detail directly from multiple stores.

Do not retain transitional compatibility paths that duplicate the aggregate.

------------------------------------------------------------------------

# Success Criteria

The feature is complete when:

- application code creates or resumes a swarm through `ManagedSwarm`;
- starting the swarm launches every configured nate-oha agent;
- ACP-assigned conversation IDs are persisted correctly;
- agent operations no longer require callers to reach into the ACP client;
- runtime APIs delegate through the swarm abstraction;
- live events are available through one swarm-level async subscription API;
- Agent Mail-disabled operation requires no Agent Mail service;
- shutdown releases all agents and event subscriptions;
- integration tests no longer manually coordinate metadata, daemon, adapters, and ACP clients to exercise a swarm lifecycle;
- duplicated lifecycle logic is removed from `RuntimeDaemon`.
