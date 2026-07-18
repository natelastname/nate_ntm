# Appendix A: Feedback round 1
## 1. Conversation IDs are owned by ACP

Conversation (session) identifiers are **not** invented by `nate_ntm`.

Instead, the ACP runtime (implemented by `nate-oha`) owns conversation IDs and returns them as part of its protocol (for example, from a `session/new` request). The runtime’s responsibility is to **persist and reuse** those IDs; it must not derive its own identifiers from swarm metadata.

**Create flow**

``` overflow-visible!
`nate_ntm` runtime
    launches `nate-oha acp` without `--resume`
        ↓
ACP `session/new` (or equivalent)
        ↓
ACP returns `conversation_id`
        ↓
`nate_ntm` persists `conversation_id` in agent metadata
```

**Resume flow**

``` overflow-visible!
`nate_ntm` runtime
    loads persisted `conversation_id` from swarm/agent metadata
        ↓
launches:

nate-oha acp \
    --config BASE_CONFIG.json \
    --resume <conversation_id> \
    [--set path=value…]
```

Key points:

- `nate_ntm` **persists** the ACP‑owned `conversation_id` but does not generate it.
- Resume semantics depend on passing the same `conversation_id` back to `nate-oha` via `--resume`.
- Any previous scheme that deterministically derives IDs from `swarm_id`, `project_path`, or `agent_id` is considered obsolete under this epic.

## 2. ACP abstraction is agent‑centric, not turn‑centric

Earlier designs exposed ACP operations focused on conversations and turns:

``` overflow-visible!
ensure_conversation()
start_turn()
```

With the `nate-oha` runtime, this is no longer the right abstraction. The scheduler and orchestrator care about **agents** being supervised, not individual turns within a session.

The ACP layer for this epic should therefore revolve around operations like:

``` overflow-visible!
start_agent()      # launch or attach to an ACP agent
stop_agent()       # request a graceful stop
interrupt()        # optional, abort in‑flight work
status()           # report adapter‑level status for the agent
stream_events()    # deliver ACP/runtime events
```

Guidance:

- Turns and other fine‑grained control concepts are considered **internal** to `nate-oha` and the ACP runtime.
- `nate_ntm` should supervise **agent lifecycles** and consume **agent‑scoped events** from ACP.
- Any existing `ensure_conversation` / `start_turn` methods on the ACP abstraction may be removed, renamed, or demoted to internal details to align with this agent‑centric model.

## 3. The scheduler launches real ACP agents

The scheduler is responsible for supervising **real `nate-oha` ACP agents**, not for simulating placeholder runtime state.

Conceptually:

``` overflow-visible!
Scheduler
    ↓
NateOhaAcpClient.start_agent(…)
    ↓
launches `nate-oha acp …`
    ↓
connects ACP stream
```

Clarifications:

- The scheduler should:
  - Ensure all configured agents are present in runtime state.
  - Call the ACP adapter to start/stop those agents according to policy.
  - React to ACP/runtime events (including failures) surfaced through the adapter.
- Any existing helpers that “launch” agents by attaching fake subprocess handles or simulating lifecycle state are transitional only and should be removed or simplified once real ACP launch is wired in.

## 4. Eliminate legacy ACP paths in the production architecture

This epic **does not** aim to preserve the previous HTTP‑oriented OpenHands ACP design.

Implications:

- The old `OpenHandsAcpClient` HTTP adapter is not part of the target architecture and may be removed entirely once migration is complete.
- Backwards compatibility with the legacy HTTP ACP surface is **not a requirement**.
- Effort should be spent on clarifying and hardening the `nate-oha`‑based ACP path rather than maintaining multiple competing ACP implementations.

Historical code and specs that describe the HTTP OpenHands ACP path are acceptable as archival references, but they must not drive new design constraints under this epic.

## 5. Single ACP implementation; profiles via nate-oha configuration

There should be **one ACP implementation path** exercised by both development and production swarms:

``` overflow-visible!
          nate_ntm
              │
              │
      NateOhaAcpClient
              │
              │
      launches nate-oha
              │
              │
        ACP protocol
              │
              │
      OpenHands runtime
```

The primary distinction between “fake” and “real” environments is the **nate-oha configuration**, not separate ACP runtimes:

- Development / test:

  ``` overflow-visible!
  runtime.mode = "echo"
  ```

- Production:

  ``` overflow-visible!
  runtime.mode = "agent"
  ```

All other behaviour should be shared:

- Subprocess launch and shutdown.
- ACP stream connection and event parsing.
- Agent‑level status reporting.
- Resume semantics based on persisted `conversation_id`.

Any in‑process “fake ACP” implementation that simulates ACP behaviour (for example, by inventing conversations and turns without involving `nate-oha`) is considered a temporary testing aid only. The **production architecture** is defined by `NateOhaAcpClient` driving `nate-oha` in different modes, with a single ACP protocol and event pipeline.









