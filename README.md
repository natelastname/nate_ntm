# nate_ntm 

Swarm Runtime Orchestrator for coordinating coding agents (for example,
OpenHands) around a single project directory. The runtime owns ACP
connections, bridges Agent Mail coordination state, and exposes a local
JSON-RPC control API (HTTP + ``/events`` WebSocket) used by CLI/TUI/web clients.

This repository currently focuses on the MVP described in
`specs/001-swarm-runtime-orchestrator/`.

## Status

- Feature branch: `001-swarm-runtime-orchestrator`
- User stories US1–US3 implemented with fake/dev-mode adapters for Agent Mail
  and ACP
- Phase 6 production adapters for Agent Mail and ACP are present but still
  evolving. For ACP, the canonical production adapter is now
  `NateOhaAcpClient` (the nate-oha ACP runtime) from Feature 002; the older
  `OpenHandsAcpClient` remains available as a legacy/compatibility option.
  REAL adapter modes should be treated as experimental and gated behind
  configuration and environment-specific quickstarts (see
  `specs/002-nate-oha-acp-adapter/quickstart.md`).

## Installation

Requires **Python 3.13+** and either `uv` or `pip`.

### Using uv (recommended for development)

```bash
# From the repository root
uv sync

# Run the test suite
uv run pytest
```

### Using pip

```bash
pip install -e .
```

This installs the `nate-ntm` CLI entrypoint (from `pyproject.toml`).

## Usage

For end-to-end usage and validation scenarios, see:

- `specs/001-swarm-runtime-orchestrator/quickstart.md`

Example (local quickstart):

```bash
# Start a new swarm in create mode with 2 agents and the control API
nate-ntm runtime start \
  --project /abs/path/to/your/project \
  --mode create \
  --agents 2 \
  --with-control-api

# From another terminal, query runtime status via the control API
nate-ntm api call runtime.get_status
```

By default the runtime uses in-memory "fake" adapters for Agent Mail and ACP so
that US1–US3 can be exercised without external services. REAL adapters can be
enabled via configuration flags and environment variables as they mature.

## Control API and shared models

The runtime exposes a local FastAPI-based JSON-RPC 2.0 control API
(``POST /jsonrpc``) plus an ``/events`` WebSocket endpoint. The
canonical response schemas for the most common JSON-RPC **results** live in
`src/nate_ntm/api/models.py` as Pydantic models.

These models represent the wire-level JSON result shapes and are reused by:

- the FastAPI/JSON-RPC server (`nate_ntm.api.runtime_api`),
- the HTTP JSON-RPC client (`nate_ntm.api.client.JsonRpcHttpClient`), and
- the `nate-ntm api call` CLI command, which normalises output for common
  methods through these models.

`JsonRpcHttpClient` also exposes a low-level `call_for_result` helper that
works with any JSON-RPC method. Typed helpers (e.g. `get_runtime_status`,
`get_swarm_overview`, `get_agent_detail`) are convenience wrappers that
validate and return the shared Pydantic result models.

## Development

Key implementation docs for this feature live under:

- `specs/001-swarm-runtime-orchestrator/plan.md`
- `specs/001-swarm-runtime-orchestrator/spec.md`
- `specs/001-swarm-runtime-orchestrator/tasks.md`

## License

MIT / Expat
