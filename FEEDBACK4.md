For this feature, I would **grow the ACP adapter abstraction**, but deliberately and narrowly.

Not all at once. Start by replacing the old HTTP `OpenHandsAcpClient` with a `NateOhaAcpClient` that exposes a few production-relevant methods:

``` overflow-visible!
class BaseAcpClient:
    def ensure_conversation(self, agent_id: str) -> str: …
    def start_agent(self, agent_id: str, *, metadata: AgentMetadata) -> None: …
    def start_turn(self, agent_id: str, prompt: str | None = None) -> str: …
    def stop_agent(self, agent_id: str, *, timeout: float) -> None: …
    def get_status(self, agent_id: str) -> AcpAgentStatus: …
```

Then add event delivery either as:

``` overflow-visible!
on_event: Callable[[AgentEvent], None]
```

or:

``` overflow-visible!
def drain_events(agent_id: str) -> list[AgentEvent]
```

For your current codebase, a callback fits the existing pattern: `AgentSupervisor` already has `on_agent_event`, and the runner bridges that to WebSocket notifications. The ACP adapter can emit events upward, and the supervisor/runtime appends them to `AgentEventStream`.

## Why not keep the current interface?

Because it hides the important work.

If `BaseAcpClient` stays as only:

``` overflow-visible!
ensure_conversation()
start_turn()
```

then the `nate_OHA` subprocess lifecycle has to go somewhere else. It will probably leak into:

- `AgentSupervisor`
- `RuntimeScheduler`
- `RuntimeDaemon`
- ad-hoc helper functions

That would violate the spec's clean boundary:

``` overflow-visible!
Runtime -> NateOhaAcpClient -> nate_OHA process
```

The old `OpenHandsAcpClient` is exactly the warning sign. It satisfied the two-method interface, but it did not solve the actual production problem. It created threads/runs over HTTP, but it did not manage the agent runtime.

## The practical implementation path

I'd tell OpenHands to do this in the plan/research phase:

``` overflow-visible!

1. Retire OpenHandsAcpClient from adapter selection.
2. Rename/reframe BaseAcpClient as the production ACP runtime adapter contract.
3. Extend BaseAcpClient with the minimum lifecycle operations required by NateOhaAcpClient.
4. Update FakeAcpClient to implement those methods in-memory.
5. Implement NateOhaAcpClient against the same interface.

```

The key is to avoid this trap:

``` overflow-visible!
BaseAcpClient = ID allocator
NateOhaAcpClient = huge sidecar object with hidden lifecycle
```

Instead, make the interface honest:

``` overflow-visible!
BaseAcpClient = runtime-owned ACP execution adapter
```

That matches what `nate_ntm` actually needs now.
