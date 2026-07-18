# Epic: nate-oha Runtime Integration

## Background

The original ACP integration was designed around the assumption that `nate_ntm` would communicate with a generic “OpenHands ACP” implementation over a small REST/ACP abstraction.

Since then, `nate-oha` has evolved into a configuration-driven runtime with a stable launch interface:

``` overflow-visible!
nate-oha acp \
    --config config.json \
    [--resume CONVERSATION_ID] \
    [--set path=value]…
```

This is now the canonical integration point.

As a result, the previous ACP adapter design should be considered obsolete. **Compatibility with that design is not a goal.** This epic is free to redesign the ACP layer around the capabilities of the current `nate-oha` runtime.

------------------------------------------------------------------------

# Goals

## 1. Replace the ACP adapter architecture

The current `OpenHandsAcpClient` was designed around an older HTTP-oriented ACP abstraction.

Instead, implement a new `NateOhaAcpClient` whose responsibility is to:

- launch `nate-oha acp`
- manage the subprocess lifecycle
- connect to the ACP stream exposed by the process
- consume ACP events
- expose those events to the rest of the runtime
- persist and reuse conversation IDs during resume

The runtime should treat `nate-oha` as the implementation of the agent runtime rather than attempting to emulate OpenHands itself.

The existing ACP implementation may be removed or heavily rewritten. Backwards compatibility is **not** required.

------------------------------------------------------------------------

## 2. Use the same ACP implementation for the fake runtime

The fake ACP adapter has served its purpose by allowing the runtime architecture to be developed independently.

Going forward, there should no longer be two fundamentally different ACP implementations.

Instead:

- the “fake” ACP adapter should simply launch `nate-oha` in

``` overflow-visible!
runtime.mode = “echo"
```

using the existing configuration system.

The runtime should therefore exercise the exact same:

- subprocess launch
- ACP protocol
- event parsing
- shutdown
- resume

code paths as production.

The only behavioral difference should be the configuration supplied to `nate-oha`.

This dramatically reduces duplicate implementations while giving much higher confidence in the runtime architecture.

------------------------------------------------------------------------

## 3. Make Agent Mail the real integration

The previous fake Agent Mail adapter was valuable while the runtime architecture was immature.

It is now unnecessary.

Remove all runtime support for the fake Agent Mail implementation.

The runtime should interact exclusively with a real `mcp_agent_mail` instance.

Tests that require Agent Mail should therefore:

- expect an accessible `mcp_agent_mail` server
- fail if one is not available
- use the real APIs
- stop exercising fake Agent Mail behavior

The objective is to validate the actual integration rather than continue maintaining a simulation.

------------------------------------------------------------------------

## 4. Agent Mail must remain optional

Although the runtime should use the real Agent Mail integration when enabled, **Agent Mail itself must not become a requirement for running a swarm.**

The runtime must continue supporting:

- swarm creation
- swarm resume
- conversation persistence
- ACP lifecycle
- agent supervision

without requiring an Agent Mail server.

When Agent Mail is disabled:

- the runtime should simply omit the Agent Mail configuration passed to `nate-oha`
- no Agent Mail APIs should be contacted
- the swarm should continue to function normally

Agent Mail is therefore an optional capability rather than a prerequisite for operating the runtime.

------------------------------------------------------------------------

## 5. Use the new configuration interface

The runtime should no longer attempt to construct OpenHands configuration internally.

Instead it should launch `nate-oha` from a base JSON configuration and apply only the runtime-specific values using `--set`.

The primary configuration paths currently needed are:

``` overflow-visible!
runtime.mode

llm.model
llm.api_key

prompt.soul_content

features.agent_mail.enabled
features.agent_mail.project
features.agent_mail.agent_identity
features.agent_mail.credentials_ref
features.agent_mail.upstream_url
```

Future work may expose additional configuration paths, but the runtime should remain responsible only for swarm-specific configuration rather than the entirety of the nate-oha configuration.

------------------------------------------------------------------------



# Guidance for Implementation

This epic intentionally supersedes several earlier design decisions.

If existing abstractions, interfaces, or tests make the implementation more complicated than necessary, **prefer simplifying the runtime rather than preserving historical compatibility.**

In particular:

- It is acceptable to redesign the ACP interfaces.
- It is acceptable to delete obsolete implementations.
- It is acceptable to replace rather than incrementally adapt previous code.
- Do not spend significant effort preserving compatibility with the previous HTTP ACP design.

Likewise, **do not optimize for preserving the existing test suite**.

Tests are valuable only insofar as they validate the new architecture. If a substantial portion of the existing tests become obsolete because the runtime architecture has changed, they should be rewritten or removed rather than forcing unnecessary compatibility layers into the production code.

The priority is achieving a clean runtime architecture around the new `nate-oha` integration, not maximizing continuity with earlier implementation details.

# Speckit Adapter Section
## User Stories / Acceptance
### P1: Launch and supervise nate-oha agents through ACP

As an operator, I want `nate_ntm` to launch and supervise `nate-oha` agent subprocesses through the ACP stream so that swarm agents use the current configuration-driven nate-oha runtime rather than the obsolete HTTP-oriented ACP design.

**Independent test**

Start a swarm with one or more agents and verify that each agent is launched through `nate-oha acp`, establishes an ACP session, exposes its ACP events to `nate_ntm`, and can be shut down cleanly.

**Acceptance scenarios**

- Given a valid nate-oha base configuration, when `nate_ntm` starts an agent, then it launches `nate-oha acp --config …` and establishes communication over the ACP stream.
- Given an active nate-oha agent, when ACP events are emitted, then `nate_ntm` receives and exposes those events through its runtime event pipeline.
- Given a running agent, when shutdown is requested, then `nate_ntm` performs a graceful ACP and subprocess shutdown before escalating to forced termination.
- Given obsolete ACP adapter code or interfaces, when the new integration is implemented, then compatibility with the previous HTTP ACP design is not required.

### P2: Create and resume persistent agent conversations

As an operator, I want agent conversation identifiers to be obtained from ACP and persisted so that a swarm can be stopped and resumed without losing conversation continuity.

**Independent test**

Create a swarm, capture the conversation identifier returned by the ACP `session/new` request, stop the swarm, resume it using the persisted identifier, and verify that the resumed agent uses the same conversation.

**Acceptance scenarios**

- Given an agent without a persisted conversation identifier, when `nate_ntm` creates a new ACP session, then it persists the conversation identifier returned by `session/new`.
- Given an agent with a persisted conversation identifier, when the swarm is resumed, then `nate_ntm` launches `nate-oha acp` with `--resume CONVERSATION_ID`.
- Given a resumed conversation, when the agent becomes active, then the persisted conversation identifier remains the canonical identifier for that agent.
- Given the new ACP ownership model, then `nate_ntm` does not generate or infer conversation identifiers itself.

### P3: Exercise production ACP code paths in echo mode

As a developer, I want development and test execution to use `NateOhaAcpClient` with nate-oha configured in echo mode so that tests exercise the same subprocess, ACP, event, shutdown, and resume paths used in production.

**Independent test**

Launch an agent with `runtime.mode=echo`, exchange ACP messages, inspect emitted events, stop the process, and resume the same conversation using the production ACP client implementation.

**Acceptance scenarios**

- Given development or test execution, when an ACP agent is launched, then `nate_ntm` uses `NateOhaAcpClient` rather than a separate fake ACP implementation.
- Given echo-mode execution, when the agent is launched, then `runtime.mode` is overridden to `echo`.
- Given agent-mode execution, when the agent is launched, then `runtime.mode` is overridden to `agent`.
- Given either runtime mode, then subprocess launch, ACP communication, event parsing, shutdown, and resume use the same implementation paths.

### P4: Run swarms with optional real Agent Mail coordination

As an operator, I want to enable real `mcp_agent_mail` coordination for a swarm when needed while retaining the ability to create, supervise, stop, and resume swarms without Agent Mail.

**Independent test**

Run one swarm with Agent Mail disabled and another with Agent Mail enabled against a running `mcp_agent_mail` server. Verify that both swarms can launch and resume, and that only the enabled swarm contacts Agent Mail.

**Acceptance scenarios**

- Given Agent Mail is disabled, when a swarm starts, then no Agent Mail API is contacted and the swarm remains launchable and resumable.
- Given Agent Mail is enabled and a server is reachable, when a swarm starts, then `nate_ntm` uses the real `mcp_agent_mail` integration.
- Given Agent Mail is enabled, when agent configuration is assembled, then the project, identity, credentials reference, and upstream URL are passed to nate-oha.
- Given a test that depends on Agent Mail, when no Agent Mail server is reachable, then the test fails rather than silently substituting a fake implementation.
- Given the new architecture, then no fake Agent Mail adapter remains in runtime code.

### P5: Configure nate-oha from a base JSON file plus runtime overrides

As an operator, I want `nate_ntm` to launch agents from a shared nate-oha JSON configuration and provide only swarm-specific overrides so that nate-oha remains responsible for its own runtime and prompt configuration.

**Independent test**

Launch multiple agents from the same base configuration while supplying different runtime mode, prompt identity, Agent Mail, model, or credential overrides, and verify that each process receives the expected configuration.

**Acceptance scenarios**

- Given a valid base configuration file, when an agent is launched, then `nate_ntm` passes it through `--config`.
- Given a persisted conversation identifier, when an agent is resumed, then `nate_ntm` also passes `--resume`.
- Given swarm-specific values, when an agent is launched, then `nate_ntm` supplies them through repeated `--set path=value` arguments.
- Given configuration owned by nate-oha, then `nate_ntm` does not reconstruct the complete OpenHands or nate-oha configuration internally.
- Given future configuration needs, then additional nate-oha configuration paths may be added without redesigning the subprocess and ACP integration.

## Functional Requirements

- **FR-001**: The runtime shall replace the obsolete HTTP-oriented ACP integration with a `NateOhaAcpClient` that launches and communicates with `nate-oha acp`.
- **FR-002**: `NateOhaAcpClient` shall manage the lifecycle of each nate-oha subprocess, including startup, ACP initialization, event consumption, graceful shutdown, and forced termination when graceful shutdown fails.
- **FR-003**: The runtime shall receive the canonical conversation identifier from the result of the ACP `session/new` request.
- **FR-004**: The runtime shall persist the ACP-provided conversation identifier for each managed agent.
- **FR-005**: When resuming an agent, the runtime shall pass its persisted conversation identifier to `nate-oha acp` through `--resume`.
- **FR-006**: The runtime shall not generate deterministic or synthetic conversation identifiers for nate-oha agents.
- **FR-007**: The runtime shall consume structured ACP events from each nate-oha subprocess and expose them to the existing runtime event and inspection mechanisms.
- **FR-008**: The runtime shall use the same `NateOhaAcpClient` implementation for development, test, and production execution.
- **FR-009**: Development and test execution shall configure nate-oha with `runtime.mode=echo` rather than using a separate fake ACP adapter.
- **FR-010**: Production agent execution shall configure nate-oha with `runtime.mode=agent`.
- **FR-011**: The previous fake ACP adapter and obsolete generic HTTP ACP implementation may be removed, and compatibility with them shall not be required.
- **FR-012**: The runtime shall launch nate-oha from a base JSON configuration supplied through `--config`.
- **FR-013**: The runtime shall supply swarm-specific nate-oha configuration using repeated `--set path=value` arguments.
- **FR-014**: The runtime shall support overrides for `runtime.mode`, `llm.model`, `llm.api_key`, and `prompt.soul_content`.
- **FR-015**: When Agent Mail is enabled, the runtime shall support overrides for `features.agent_mail.enabled`, `features.agent_mail.project`, `features.agent_mail.agent_identity`, `features.agent_mail.credentials_ref`, and `features.agent_mail.upstream_url`.
- **FR-016**: The runtime shall use only a real `mcp_agent_mail` integration when Agent Mail is enabled.
- **FR-017**: The runtime shall remove runtime support for the fake Agent Mail adapter.
- **FR-018**: Tests that exercise Agent Mail behavior shall require a reachable `mcp_agent_mail` instance and shall fail if the required connection cannot be established.
- **FR-019**: Agent Mail shall remain optional for swarm creation, supervision, shutdown, and resume.
- **FR-020**: When Agent Mail is disabled, the runtime shall not contact Agent Mail APIs and shall configure nate-oha with Agent Mail disabled.
- **FR-021**: The runtime shall remain responsible for swarm metadata, process supervision, scheduling, resume semantics, ACP stream management, and optional Agent Mail coordination.
- **FR-022**: nate-oha shall remain responsible for LLM execution, prompt construction, ACP server behavior, OpenHands integration, and Agent Mail feature behavior within the agent runtime.
- **FR-023**: Existing interfaces, implementations, and tests that conflict with the new architecture may be rewritten or removed rather than preserved through compatibility layers.
- **FR-024**: Tests retained or added for this epic shall validate the new nate-oha integration architecture rather than preserve obsolete implementation behavior.

## Key Entities

- **NateOhaAcpClient**: The runtime-owned ACP client responsible for launching `nate-oha acp`, initializing or resuming sessions, consuming ACP events, and managing subprocess lifecycle.
- **nate-oha subprocess**: A managed agent runtime process launched from a base JSON configuration and optional `--resume` and `--set` arguments.
- **ACP session**: The live protocol session between `nate_ntm` and a nate-oha subprocess.
- **Conversation identifier**: The opaque identifier returned by ACP `session/new`, persisted by `nate_ntm`, and supplied through `--resume` on later launches.
- **Base nate-oha configuration**: A JSON configuration file containing stable agent-runtime settings shared across launches.
- **Runtime override**: A swarm- or agent-specific configuration value passed to nate-oha through `--set path=value`.
- **Runtime mode**: The nate-oha mode selected through `runtime.mode`; `echo` is used for development and protocol testing, while `agent` is used for LLM-backed execution.
- **Agent Mail configuration**: The optional nate-oha feature configuration containing project, identity, credentials reference, and upstream URL.
- **mcp_agent_mail instance**: The real coordination service contacted when Agent Mail is enabled and required by Agent Mail-dependent integration tests.
- **Swarm metadata**: Persisted runtime-owned state required to reconstruct and resume a swarm, including each agent's ACP-provided conversation identifier and optional Agent Mail details.

## Success Criteria

- **SC-001**: A new agent can be launched through `nate-oha acp`, complete ACP initialization, and return a conversation identifier from `session/new`.
- **SC-002**: After shutdown, a swarm can be resumed using persisted conversation identifiers, and every resumed agent continues the same nate-oha conversation.
- **SC-003**: Echo-mode tests exercise the same nate-oha subprocess, ACP stream, event parsing, shutdown, and resume implementation used in agent mode.
- **SC-004**: Structured ACP events emitted by nate-oha are visible through the runtime's agent inspection and live event subscription interfaces.
- **SC-005**: A swarm can be created, supervised, stopped, and resumed with Agent Mail disabled and without any Agent Mail server available.
- **SC-006**: When Agent Mail is enabled and a real `mcp_agent_mail` server is available, the runtime supplies the required project and identity configuration and agents can use the real integration.
- **SC-007**: Agent Mail-dependent integration tests fail clearly when the required `mcp_agent_mail` service cannot be reached.
- **SC-008**: No runtime execution path depends on the old fake ACP adapter, fake Agent Mail adapter, or obsolete HTTP ACP implementation.
- **SC-009**: The runtime launches agents using a base nate-oha JSON configuration plus only the required swarm-specific overrides.
- **SC-010**: Obsolete tests and compatibility code can be removed without preventing validation of the new create, resume, supervision, ACP event, and optional Agent Mail behavior.

## Assumptions

- `nate-oha acp` remains the canonical command for launching the nate-oha ACP runtime.
- The ACP `session/new` response contains the conversation identifier that `nate_ntm` must persist.
- nate-oha supports `--resume CONVERSATION_ID` for restoring an existing OpenHands conversation.
- nate-oha supports repeated `--set path=value` overrides for the configuration paths listed in this epic.
- A suitable base nate-oha JSON configuration will be available to `nate_ntm`, including a repository-provided configuration for tests.
- Echo mode provides sufficient deterministic behavior for ACP lifecycle and protocol tests without invoking a real LLM.
- Agent Mail-dependent tests may rely on a separately running `mcp_agent_mail` service.
- Agent Mail is an optional swarm capability and is not required for core ACP lifecycle or resume behavior.
- Backward compatibility with the previous generic HTTP ACP adapter, fake ACP adapter, fake Agent Mail adapter, or their tests is explicitly out of scope.
- The existing test suite may be substantially rewritten or reduced when its expectations conflict with the new architecture.
