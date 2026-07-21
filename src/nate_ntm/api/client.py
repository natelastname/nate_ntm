"""JSON-RPC HTTP client for the runtime control API."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Mapping

from .models import AgentDetailResult, RuntimeStatusResult, SwarmOverviewResult

JSONRPC_VERSION = "2.0"


class JsonRpcClientError(RuntimeError):
    def __init__(
        self,
        code: int,
        message: str,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.data = dict(data) if data is not None else None
        detail = f"JSON-RPC error {code}: {message}"
        if self.data:
            detail = f"{detail} ({self.data})"
        super().__init__(detail)


@dataclass(slots=True)
class JsonRpcHttpClient:
    host: str = "127.0.0.1"
    port: int = 8765
    timeout: float | None = 10.0

    async def call_async(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        request_id: int = 1,
    ) -> Mapping[str, Any]:
        import http.client

        def request() -> Mapping[str, Any]:
            connection = http.client.HTTPConnection(
                self.host,
                self.port,
                timeout=self.timeout,
            )
            try:
                body = json.dumps(
                    {
                        "jsonrpc": JSONRPC_VERSION,
                        "method": method,
                        "params": params or {},
                        "id": request_id,
                    }
                ).encode()
                connection.request(
                    "POST",
                    "/jsonrpc",
                    body,
                    {"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                raw = response.read().decode()
            finally:
                connection.close()
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status} {response.reason}: {raw}")
            return json.loads(raw)

        return await asyncio.to_thread(request)

    async def call_for_result(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        request_id: int = 1,
    ) -> Any:
        response = await self.call_async(method, params, request_id=request_id)
        if "error" in response:
            error = response["error"] or {}
            raise JsonRpcClientError(
                code=int(error.get("code", -1)),
                message=str(error.get("message", "Unknown error")),
                data=error.get("data"),
            )
        return response.get("result")

    async def get_runtime_status(self) -> RuntimeStatusResult:
        return RuntimeStatusResult.model_validate(
            await self.call_for_result("runtime.get_status")
        )

    async def get_swarm_overview(self) -> SwarmOverviewResult:
        return SwarmOverviewResult.model_validate(
            await self.call_for_result("swarm.get_overview")
        )

    async def get_agent_detail(self, agent_id: str) -> AgentDetailResult:
        return AgentDetailResult.model_validate(
            await self.call_for_result("agent.get_detail", {"agent_id": agent_id})
        )


def call(
    method: str,
    params: Mapping[str, Any] | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> Any:
    return asyncio.run(
        JsonRpcHttpClient(host=host, port=port).call_for_result(method, params)
    )
