"""JSON-RPC dispatch helpers for the runtime control API.

This module provides a small, transport-agnostic dispatcher that maps
JSON-RPC-style request objects to :class:`RuntimeApiServer` handler
methods.

It intentionally focuses on the MVP surface from
``specs/001-swarm-runtime-orchestrator/contracts/runtime-api.md``:

* ``runtime.get_status``
* ``runtime.shutdown``
* ``swarm.get_overview``
* ``agent.get_detail``
* ``events.subscribe``
* ``events.unsubscribe``

The eventual WebSocket server will be responsible for:

* Receiving raw text frames from clients.
* Parsing JSON into Python dictionaries.
* Passing the resulting mapping into :func:`dispatch_request`.
* Serializing the resulting response mapping back to JSON.

Network and async concerns are intentionally out-of-scope here.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping

from .server import RuntimeApiServer

JSONRPC_VERSION = "2.0"

__all__ = [
    "JSONRPC_VERSION",
    "dispatch_request",
]


def _make_result(response_id: Any, result: Any) -> Dict[str, Any]:
    """Return a JSON-RPC success envelope."""

    return {
        "jsonrpc": JSONRPC_VERSION,
        "result": result,
        "id": response_id,
    }


def _make_error(
    response_id: Any,
    *,
    code: int,
    message: str,
    data: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Return a JSON-RPC error envelope.

    The shape mirrors the contract in
    ``specs/001-swarm-runtime-orchestrator/contracts/runtime-api.md``.
    """

    error: Dict[str, Any] = {
        "code": code,
        "message": message,
    }
    if data:
        error["data"] = data

    return {
        "jsonrpc": JSONRPC_VERSION,
        "error": error,
        "id": response_id,
    }


def dispatch_request(
    server: RuntimeApiServer,
    request: Mapping[str, Any],
) -> Dict[str, Any]:
    """Dispatch a single JSON-RPC-style request to the API server.

    Parameters
    ----------
    server:
        The :class:`RuntimeApiServer` instance to dispatch against.

    request:
        A mapping representing the JSON-RPC request object. Only the
        MVP methods from ``contracts/runtime-api.md`` are supported.

    Returns
    -------
    dict
        A JSON-serializable response object in the JSON-RPC 2.0 shape
        with either a ``result`` or an ``error`` key.
    """

    response_id = request.get("id")

    # Basic protocol validation ------------------------------------------------
    if request.get("jsonrpc") != JSONRPC_VERSION:
        return _make_error(
            response_id,
            code=1000,
            message="Invalid jsonrpc version",
            data={"expected": JSONRPC_VERSION},
        )

    method = request.get("method")
    if not isinstance(method, str):
        return _make_error(
            response_id,
            code=1000,
            message="Missing or invalid method name",
        )

    raw_params = request.get("params")
    if raw_params is None:
        params: Mapping[str, Any] = {}
    elif isinstance(raw_params, Mapping):
        params = raw_params
    else:
        return _make_error(
            response_id,
            code=1000,
            message="Params must be an object",
        )

    # Method dispatch ----------------------------------------------------------
    try:
        if method == "runtime.get_status":
            result = server.get_runtime_status()

        elif method == "swarm.get_overview":
            result = server.get_swarm_overview()

        elif method == "runtime.shutdown":
            timeout = params.get("timeout_seconds", 30)
            try:
                timeout_int = int(timeout)
            except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
                raise ValueError("timeout_seconds must be an integer") from exc

            result = server.shutdown_runtime(timeout_seconds=timeout_int)

        elif method == "agent.get_detail":
            if "agent_id" not in params:
                return _make_error(
                    response_id,
                    code=1000,
                    message="Missing required parameter: agent_id",
                )

            agent_id = str(params["agent_id"])
            max_events_raw = params.get("max_events", 100)
            try:
                max_events = int(max_events_raw)
            except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
                raise ValueError("max_events must be an integer") from exc

            try:
                result = server.get_agent_detail(
                    agent_id=agent_id,
                    max_events=max_events,
                )
            except KeyError:
                # Map unknown agents to a structured JSON-RPC error.
                return _make_error(
                    response_id,
                    code=1001,
                    message="Agent not found",
                    data={"agent_id": agent_id},
                )

        elif method == "events.subscribe":
            agent_ids_val = params.get("agent_ids")
            if agent_ids_val is not None and not isinstance(agent_ids_val, list):
                return _make_error(
                    response_id,
                    code=1000,
                    message="agent_ids must be a list or null",
                )

            include_runtime = bool(params.get("include_runtime", True))
            result = server.subscribe_events(
                agent_ids=agent_ids_val,
                include_runtime=include_runtime,
            )

        elif method == "events.unsubscribe":
            if "subscription_id" not in params:
                return _make_error(
                    response_id,
                    code=1000,
                    message="Missing required parameter: subscription_id",
                )

            subscription_id = str(params["subscription_id"])
            result = server.unsubscribe_events(subscription_id)

        else:
            # Unknown method: treat as an invalid request per the MVP contract.
            return _make_error(
                response_id,
                code=1000,
                message=f"Unknown method: {method}",
            )

    except ValueError as exc:
        # Parameter validation errors bubble up here.
        return _make_error(
            response_id,
            code=1000,
            message="Invalid parameters",
            data={"detail": str(exc)},
        )
    except RuntimeError as exc:
        # Runtime state conflicts (e.g., invalid shutdown transitions).
        return _make_error(
            response_id,
            code=1100,
            message="Runtime state conflict",
            data={"detail": str(exc)},
        )
    except Exception as exc:  # pragma: no cover - defensive
        # Fallback for unexpected errors in handlers.
        return _make_error(
            response_id,
            code=1200,
            message="Internal error",
            data={"detail": str(exc)},
        )

    return _make_result(response_id, result)
