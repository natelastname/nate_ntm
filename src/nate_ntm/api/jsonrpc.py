"""Transport-agnostic JSON-RPC dispatch for the runtime control API."""

from __future__ import annotations

from typing import Any, Mapping

from .server import RuntimeApiServer

JSONRPC_VERSION = "2.0"

__all__ = ["JSONRPC_VERSION", "dispatch_request"]


def _result(response_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "result": result, "id": response_id}


def _error(
    response_id: Any,
    *,
    code: int,
    message: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "error": error, "id": response_id}


def dispatch_request(
    server: RuntimeApiServer,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    response_id = request.get("id")
    if request.get("jsonrpc") != JSONRPC_VERSION:
        return _error(
            response_id,
            code=1000,
            message="Invalid jsonrpc version",
            data={"expected": JSONRPC_VERSION},
        )

    method = request.get("method")
    if not isinstance(method, str):
        return _error(response_id, code=1000, message="Missing or invalid method name")

    raw_params = request.get("params")
    if raw_params is None:
        params: Mapping[str, Any] = {}
    elif isinstance(raw_params, Mapping):
        params = raw_params
    else:
        return _error(response_id, code=1000, message="Params must be an object")

    try:
        if method == "runtime.get_status":
            result = server.get_runtime_status()
        elif method == "swarm.get_overview":
            result = server.get_swarm_overview()
        elif method == "runtime.shutdown":
            result = server.shutdown_runtime(
                timeout_seconds=int(params.get("timeout_seconds", 30))
            )
        elif method == "agent.get_detail":
            if "agent_id" not in params:
                return _error(
                    response_id,
                    code=1000,
                    message="Missing required parameter: agent_id",
                )
            agent_id = str(params["agent_id"])
            try:
                result = server.get_agent_detail(agent_id=agent_id)
            except KeyError:
                return _error(
                    response_id,
                    code=1001,
                    message="Agent not found",
                    data={"agent_id": agent_id},
                )
        else:
            return _error(
                response_id,
                code=1000,
                message=f"Unknown method: {method}",
            )
    except (TypeError, ValueError) as exc:
        return _error(
            response_id,
            code=1000,
            message="Invalid parameters",
            data={"detail": str(exc)},
        )
    except RuntimeError as exc:
        return _error(
            response_id,
            code=1100,
            message="Runtime state conflict",
            data={"detail": str(exc)},
        )
    except Exception as exc:
        return _error(
            response_id,
            code=1200,
            message="Internal error",
            data={"detail": str(exc)},
        )

    return _result(response_id, result)
