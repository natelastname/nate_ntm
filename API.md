# The app currently exposes these JSON-RPC methods:

## runtime.get_status

Returns a high-level runtime status snapshot.

It tells you whether the runtime is running/stopped/etc., which project it is managing, the swarm ID, and aggregate agent counts.

Use when you want a quick “is the daemon alive and what is it doing?” check.

------------------------------------------------------------------------

## swarm.get_overview

Returns a broader swarm overview.

It includes runtime status, project path, swarm ID, agent counts, and a list of agents with summary fields like display name, status, unread-mail flag, and last error.

Use when building a dashboard or TUI overview.

------------------------------------------------------------------------

## agent.get_detail

Returns detailed information for one agent.

Inputs:

```
{
  “agent_id”: “agent-1”,
  “max_events”: 100
}
```

Returns:

- agent metadata
- status
- Agent Mail identity
- conversation ID
- last error
- recent events

Use when inspecting/debugging a specific agent.

If the agent does not exist, it returns JSON-RPC error code `1001`.

------------------------------------------------------------------------

## runtime.shutdown

Requests runtime shutdown.

Inputs usually include:

```
{
  “timeout_seconds”: 30
}
```

It asks the runtime to stop cleanly and returns a JSON-RPC result describing the shutdown request/result.

Use when stopping the daemon through the control API.

------------------------------------------------------------------------

## events.subscribe

Creates an event subscription.

Inputs may include:

```
{
  “agent_ids”: ["agent-1”],
  “include_runtime”: true
}
```

Returns a `subscription_id`. Then the client connects to:

```
WS /events
```

and sends:

```
{
  “subscription_id”: “sub-001"
}
```

After that, matching events are delivered as `events.notify` messages over the WebSocket.

------------------------------------------------------------------------

## events.unsubscribe

Removes an event subscription.

Input:

```
{
  “subscription_id”: “sub-001"
}
```

Use this when the client no longer wants event notifications.

------------------------------------------------------------------------

## events.notify

This is not usually called by clients. It is the server-to-client notification method sent over `/events` WebSocket when a subscribed runtime or agent event occurs. 

Example shape:

```
{
  “jsonrpc”: “2.0”,
  “method”: “events.notify”,
  “params”: {
    “subscription_id”: “sub-001”,
    “event”: {
      “agent_id”: “agent-1”,
      “type”: “AgentFailed”,
      “payload”: {}
    }
  }
}
```

So the control model is:

```
POST /jsonrpc -> commands and queries
WS /events -> event notifications
```









