# Epic: Nate OHA Runtime Integration

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

Future work may expose additional configuration paths, but the runtime should remain responsible only for swarm-specific configuration rather than the entirety of the Nate OHA configuration.

------------------------------------------------------------------------

# Desired Architecture

The runtime should become responsible for:

- swarm metadata
- process supervision
- scheduling
- resume semantics
- ACP stream management
- Agent Mail coordination (when enabled)

while `nate-oha` becomes responsible for:

- LLM execution
- prompt construction
- ACP server behavior
- OpenHands integration
- Agent Mail feature implementation

This creates a much cleaner separation of concerns.

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
