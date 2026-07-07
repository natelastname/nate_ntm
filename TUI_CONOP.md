# Feature Specification: Textual Runtime Console

## Purpose

Provide an interactive terminal interface for observing and interacting with a running `nate_ntm` runtime.

The console should become the primary operator interface for `nate_ntm`, providing a live view of swarm activity while establishing a foundation for future runtime management capabilities.

Rather than being a single-purpose monitor, the console should be designed as a multi-screen application that can grow alongside the runtime.

------------------------------------------------------------------------

# Vision

The console should feel similar to tools such as:

- `htop`
- `btop`
- `k9s`
- `lazygit`

Users should be able to launch the console, immediately understand the current state of the swarm, inspect individual agents, observe activity in real time, and eventually perform more advanced runtime operations from a unified interface.

The initial implementation focuses on monitoring, but the architecture should naturally accommodate additional capabilities over time.

------------------------------------------------------------------------

# Relationship to the Runtime API

The console is a client of the runtime control API.

It should communicate exclusively through the public API rather than interacting directly with runtime internals.

This keeps the console loosely coupled to the runtime and ensures that it exercises the same interfaces used by any future external clients.

The runtime currently exposes two complementary communication mechanisms:

- a request/response API for querying and controlling the runtime
- a live event stream for receiving asynchronous updates

The console should make use of both.

------------------------------------------------------------------------

# Architectural Direction

The console should be structured as a single application composed of multiple screens that share a common runtime session.

A single runtime session is responsible for maintaining communication with the runtime, caching state, and distributing updates throughout the application.

Individual screens should focus on presenting different views of that shared state rather than establishing their own independent connections.

This allows new screens to be introduced without changing the overall application architecture.

------------------------------------------------------------------------

# Initial User Experience

The first screen should present an htop-style overview of the swarm.

At a glance, an operator should be able to understand:

- overall runtime health
- swarm status
- the state of every agent
- recent activity occurring within the runtime

The monitor should update continuously as the runtime changes.

Selecting an individual agent should provide additional information about that agent without leaving the overall monitoring workflow.

------------------------------------------------------------------------

# Future Growth

The monitor should be viewed as the first screen of a larger runtime console rather than the complete application.

Examples of future screens include:

- detailed agent inspection
- event exploration
- Agent Mail
- runtime logs
- ACP interaction
- runtime configuration
- operational dashboards

The application structure should make the addition of new screens straightforward without requiring significant changes to existing ones.

------------------------------------------------------------------------

# Runtime API Usage

The initial monitor should exercise the existing runtime control API in a way that reflects realistic operator workflows.

In particular, it should make use of:

- periodic runtime status updates
- periodic swarm overview updates
- agent inspection
- runtime shutdown
- event subscription
- event unsubscription

The console should also consume the runtime event stream so that changes are reflected immediately rather than relying solely on polling.

------------------------------------------------------------------------

# Success Criteria

The feature is considered successful when:

- operators can launch a terminal console and immediately observe a running swarm
- runtime state updates automatically as work progresses
- individual agents can be inspected from within the console
- live runtime events appear without requiring manual refresh
- the console is organized around reusable screens sharing a common runtime session
- additional screens can be added without redesigning the application's architecture
