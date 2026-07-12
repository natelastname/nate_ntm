## Appendix C: Resume and ACP Conversation History

Unlike many client/server protocols, resuming a Nate OHA conversation does not reconnect to the conversation at its current endpoint.

Instead, when `nate-oha` is launched with a persisted conversation identifier, the ACP session is reconstructed by replaying the conversation from its beginning. The runtime therefore receives the complete conversation history before continuing with newly generated events.

The resume flow is:

``` overflow-visible!

1. Read persisted conversation_id.
2. Launch:
   nate-oha acp
     --config BASE
     --resume CONVERSATION_ID
     --set …

3. Connect the official ACP SDK to the subprocess stdio streams.
4. Initialize the ACP connection.
5. Establish the resumed ACP session.
6. Receive the ACP event stream for the conversation.
7. Reconstruct the runtime's in-memory view of the agent from that
   event stream.

8. Continue processing subsequent ACP events on the same connection.

```

The persisted conversation identifier remains the canonical identity of the conversation. `nate_ntm` persists this identifier but does not generate or derive it.

### ACP as the Source of Truth

The complete conversation history is owned by Nate OHA and exposed through ACP.

`nate_ntm` should treat the ACP stream as the authoritative representation of an agent's execution history rather than attempting to maintain a separate durable event log.

Conceptually:

``` overflow-visible!
Nate OHA conversation
        │
        ▼
ACP event stream
        │
        ▼
translate_acp_update(…)
        │
        ▼
AgentEventStream
        │
        ▼
Runtime views and supervision
```

Because Nate OHA already owns durable conversation history, the runtime does not need to persist a duplicate copy of the ACP event stream.

The runtime's `AgentEventStream` is therefore a bounded in-memory projection of the ACP stream rather than a second source of truth.

### Runtime Responsibilities

The runtime is responsible for:

- persisting the ACP-provided `conversation_id`;
- launching Nate OHA with `--resume <conversation_id>` when appropriate;
- consuming the ACP event stream;
- rebuilding its runtime state from that stream;
- exposing agent state and events through the runtime APIs.

The runtime is **not** responsible for reconstructing conversation history independently or synthesizing conversation state from metadata.

### Testing Resume

Resume testing should validate the complete lifecycle of a resumed conversation rather than simply verifying identifier persistence.

A representative integration test should:

``` overflow-visible!

1. Launch Nate OHA in echo mode.
2. Create a new ACP session.
3. Produce multiple identifiable ACP events.
4. Persist the returned session_id.
5. Stop the Nate OHA subprocess.
6. Relaunch with:
   --resume SESSION_ID

7. Verify that the runtime receives the existing conversation history.
8. Verify that AgentEventStream reflects that conversation.
9. Send a new prompt.
10. Verify that subsequent events continue on the same conversation.

```

The objective is to demonstrate that conversation continuity is preserved across runtime restarts and that the runtime reconstructs its in-memory state entirely from the ACP stream.

### Design Principle

> Nate OHA owns durable conversation history. ACP exposes that history as an event stream. `nate_ntm` consumes that stream to supervise agents, maintain runtime state, and expose runtime APIs.

This keeps responsibility cleanly divided:

- **Nate OHA** owns conversations, prompts, LLM execution, and durable history.
- **ACP** transports the conversation as a structured event stream.
- **`nate_ntm`** owns process supervision, scheduling, runtime metadata, and the projection of ACP events into runtime state.
