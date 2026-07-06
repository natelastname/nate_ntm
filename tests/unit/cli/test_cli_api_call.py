"""Unit tests for the Typer-based `api call` command (T009).

These tests exercise parameter parsing and JSON-RPC invocation behavior
for `nate_ntm.cli.api_call` without requiring a real HTTP server.

Network interactions are stubbed by replacing
:class:`JsonRpcHttpClient` with a small fake that records calls and
returns predefined responses.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping

from typer.testing import CliRunner

from nate_ntm.cli import app


runner = CliRunner()


class _FakeClient:
    """Test double for :class:`JsonRpcHttpClient`.

    It captures the last call and returns a configurable response
    envelope.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8765, timeout: float | None = None) -> None:  # type: ignore[assignment]
        self.host = host
        self.port = port
        self.timeout = timeout
        self.last_method: str | None = None
        self.last_params: Mapping[str, Any] | None = None
        # Default success response; tests can override by patching attributes
        self.response: Mapping[str, Any] = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"ok": True},
        }

    async def call_async(self, method: str, params: Mapping[str, Any] | None = None, *, request_id: int = 1) -> Mapping[str, Any]:  # type: ignore[override]
        self.last_method = method
        self.last_params = params or {}
        return self.response


def test_api_call_runtime_get_status_success(monkeypatch) -> None:
    from nate_ntm import cli as cli_mod

    fake = _FakeClient()
    monkeypatch.setattr(cli_mod, "JsonRpcHttpClient", lambda host, port: fake)

    result = runner.invoke(app, ["api", "call", "runtime.get_status"])

    assert result.exit_code == 0, result.output
    assert fake.last_method == "runtime.get_status"
    assert fake.last_params == {}

    payload = json.loads(result.stdout)
    assert payload == {"ok": True}


def test_api_call_parses_params_and_surfaces_jsonrpc_errors(monkeypatch) -> None:
    from nate_ntm import cli as cli_mod

    fake = _FakeClient()
    fake.response = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": 123, "message": "Boom"},
    }

    monkeypatch.setattr(cli_mod, "JsonRpcHttpClient", lambda host, port: fake)

    result = runner.invoke(
        app,
        [
            "api",
            "call",
            "agent.get_detail",
            "--param",
            "agent_id=nav-1",
            "--param",
            "max_events=10",
        ],
    )

    # JSON-RPC errors should yield a non-zero exit code and the error
    # payload should be rendered to stderr as JSON.
    assert result.exit_code == 1
    assert fake.last_method == "agent.get_detail"
    assert fake.last_params == {"agent_id": "nav-1", "max_events": 10}

    error_payload = json.loads(result.stderr)
    assert error_payload == {"code": 123, "message": "Boom"}
