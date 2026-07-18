"""Agent Mail coordination adapters for the swarm runtime (T014/T101).

This module defines a small, runtime-owned abstraction for interacting
with an Agent Mail coordination service. The primary implementation is
:class:`McpAgentMailClient`, a production-oriented adapter that talks
to a running Agent Mail server (for example via the ``mcp_agent_mail``
package) over HTTP/JSON-RPC.

The goal is to keep the runtime core testable and allow higher layers
(daemon, scheduler, API) to depend on a narrow interface regardless of
transport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import json
import os

from ..config.runtime_config import RuntimeConfig

__all__ = [
    "AgentMailClientError",
    "BaseAgentMailClient",
    "McpAgentMailClient",
]


class AgentMailClientError(RuntimeError):
    """Base error type for Agent Mail adapter failures.

    In the real adapter this will be used to wrap lower-level
    network/HTTP/MCP exceptions so that callers can handle all
    integration failures in a uniform way. The fake client used in
    tests generally does not raise this error.
    """



def _extract_jsonrpc_result(payload: Any, *, request_name: str) -> Any:
    """Unwrap an MCP JSON-RPC response payload.

    This mirrors the semantics used by the reference ``mcp_agent_mail``
    CLI: non-dict payloads are treated as invalid responses, ``error``
    objects raise :class:`AgentMailClientError`, and tool results are
    unwrapped from ``structuredContent`` / ``structured_content`` when
    present.
    """

    if not isinstance(payload, Mapping):
        raise AgentMailClientError(f"{request_name}: invalid server response")

    error = payload.get("error")
    if isinstance(error, Mapping):
        message = str(error.get("message") or "server request failed")
        detail = error.get("data")
        if isinstance(detail, Mapping):
            detail = detail.get("message") or detail.get("detail") or detail
        if detail not in (None, "", message):
            message = f"{message}: {detail}"
        raise AgentMailClientError(f"{request_name}: {message}")

    result = payload.get("result")
    if not isinstance(result, Mapping):
        # Some tools may return bare lists or scalars.
        return result

    structured_missing = object()
    structured = result.get("structuredContent", structured_missing)
    if structured is structured_missing:
        structured = result.get("structured_content", structured_missing)
    if structured is not structured_missing:
        if isinstance(structured, Mapping):
            return structured.get("result", structured)
        return structured

    return result



class BaseAgentMailClient:
    """Abstract interface for Agent Mail coordination.

    Implementations are expected to be **runtime-owned**: a
    :class:`~nate_ntm.runtime.daemon.RuntimeDaemon` (or tests) should
    construct an adapter instance and reuse it for the lifetime of the
    process.

    The interface is intentionally small and focused on the needs of the
    runtime:

    * Create or reuse a *project-level* Agent Mail identifier for the
      swarm.
    * Create or reuse *per-agent* identities within that project.
    * Report whether agents currently have unread mail so that
      ``swarm.get_overview`` and related APIs can expose a
      ``has_unread_mail`` flag.
    """

    # The following methods define the public contract. Concrete
    # implementations *must* override them.

    def ensure_project(self) -> str:  # pragma: no cover - abstract
        """Ensure an Agent Mail project exists for this swarm.

        The returned string is an opaque identifier understood by the
        backing Agent Mail service. Implementations must be **idempotent**:
        repeated calls for the same runtime configuration MUST return the
        same project identifier.
        """

        raise NotImplementedError

    def ensure_agent_identity(self, agent_id: str) -> str:  # pragma: no cover - abstract
        """Ensure an Agent Mail identity exists for ``agent_id``.

        The returned string is an opaque identifier representing the
        agent within the Agent Mail project. Implementations must be
        **idempotent** per agent: calling this multiple times for the
        same ``agent_id`` MUST return the same identity string.
        """

        raise NotImplementedError

    def ensure_agent_identity_with_credentials(
        self, agent_id: str, credentials_hint: str | None = None
    ) -> Tuple[str, str | None]:
        """Ensure identity and optionally propagate credentials for ``agent_id``.

        Real adapters that manage per-agent credentials (for example Agent
        Mail registration tokens) may use ``credentials_hint`` when
        binding to an existing identity and return an updated credential
        value.

        The default implementation delegates to :meth:`ensure_agent_identity`
        and simply passes ``credentials_hint`` through unchanged.
        """

        identity = self.ensure_agent_identity(agent_id)
        return identity, credentials_hint

    def get_unread_mail_flags(self, agent_ids: Iterable[str]) -> Dict[str, bool]:  # pragma: no cover - abstract
        """Return a mapping of ``agent_id`` to ``has_unread_mail``.

        Implementations should treat unknown agents conservatively as not
        having unread mail (``False``) unless they have a strong reason
        to do otherwise.
        """

        raise NotImplementedError


@dataclass(slots=True)
class McpAgentMailClient(BaseAgentMailClient):
    """Production Agent Mail adapter backed by MCP HTTP APIs (T101).

    This implementation speaks JSON-RPC over HTTP to a running
    ``mcp_agent_mail`` server. It relies on the Agent Mail tools

    * ``ensure_project`` to create/lookup a coordination project for the
      swarm's project directory.
    * ``register_agent`` to create/lookup per-agent identities and
      obtain registration tokens.
    * ``fetch_inbox`` to detect whether an agent currently has unread
      mail.

    Network failures and server-side tool errors are surfaced as
    :class:`AgentMailClientError` so that callers can decide how to
    react (for example, fail fast on startup vs. degrade mailbox
    polling).
    """

    config: RuntimeConfig
    base_url: str | None = None
    bearer_token: str | None = None
    timeout: float = 5.0

    _project_id: str | None = field(default=None, init=False)
    _agent_identities: Dict[str, str] = field(default_factory=dict, init=False)
    _agent_tokens: Dict[str, str] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        """Resolve endpoint and auth settings from arguments or environment."""

        url = (
            self.base_url
            or os.environ.get("NATE_NTM_AGENT_MAIL_URL")
            or os.environ.get("AGENT_MAIL_URL")
            or "http://127.0.0.1:8765/api"
        )
        self.base_url = url.strip()

        token = (
            self.bearer_token
            or os.environ.get("NATE_NTM_AGENT_MAIL_TOKEN")
            or os.environ.get("AGENT_MAIL_TOKEN")
            or ""
        )
        self.bearer_token = token.strip()

    # ------------------------------------------------------------------
    # BaseAgentMailClient API
    # ------------------------------------------------------------------

    def ensure_project(self) -> str:
        """Ensure an Agent Mail project exists for this runtime's project.

        The project key is derived from :class:`RuntimeConfig` and treated
        as the swarm's stable identifier for talking to Agent Mail. The
        adapter caches the resulting identifier so repeated calls in the
        same process are cheap.
        """

        if self._project_id is not None:
            return self._project_id

        # Prefer an explicit Agent Mail project key when configured;
        # otherwise fall back to the absolute project path. This keeps
        # deployment-time configuration in :class:`RuntimeConfig` as the
        # single source of truth while remaining backwards compatible with
        # earlier quickstarts that used the project path directly.
        project_key = (self.config.agent_mail_project or str(self.config.project_path)).strip()

        # Call the Agent Mail ``ensure_project`` tool for its side effects
        # (for example, creating the project or validating credentials),
        # but treat the configured ``project_key`` as the canonical
        # identifier within the runtime. The same key is reused for
        # per-agent operations and persisted into swarm metadata.
        _ = self._call_tool(
            name="ensure_project",
            arguments={"human_key": project_key},
            request_id="nate-ntm-ensure-project",
            request_name="Agent Mail ensure_project",
        )

        self._project_id = project_key
        return project_key

    def ensure_agent_identity(self, agent_id: str) -> str:
        """Ensure an Agent Mail identity exists for ``agent_id``.

        This forwards to :meth:`ensure_agent_identity_with_credentials`
        and discards the credential value.
        """

        identity, _ = self.ensure_agent_identity_with_credentials(agent_id)
        return identity

    def ensure_agent_identity_with_credentials(
        self, agent_id: str, credentials_hint: str | None = None
    ) -> Tuple[str, str | None]:
        """Ensure an identity + registration token for ``agent_id``.

        ``credentials_hint`` may contain a previously stored registration
        token (for example from :class:`AgentMetadata`). When provided,
        it is passed to Agent Mail so that existing identities can be
        re-authorized without minting a new token.
        """

        # Use cached values when available to avoid unnecessary HTTP calls.
        cached_identity = self._agent_identities.get(agent_id)
        cached_token = self._agent_tokens.get(agent_id)
        if cached_identity is not None:
            token = cached_token or credentials_hint
            return cached_identity, token

        # Always ensure the project exists before registering agents and
        # reuse the same project key that :meth:`ensure_project` returned so
        # that all Agent Mail operations for this swarm are scoped
        # consistently.
        project_key = self.ensure_project()

        arguments: Dict[str, Any] = {
            "project_key": project_key,
            "program": "nate-ntm-runtime",
            "model": "nate-ntm-swarm",
            "name": agent_id,
            "task_description": "",
        }
        if credentials_hint:
            arguments["registration_token"] = credentials_hint

        result = self._call_tool(
            name="register_agent",
            arguments=arguments,
            request_id=f"nate-ntm-register-agent:{agent_id}",
            request_name=f"Agent Mail register_agent({agent_id})",
        )

        if isinstance(result, Mapping):
            identity = str(result.get("name") or agent_id)
            token_raw = result.get("registration_token")
            token = str(token_raw).strip() if token_raw is not None else ""
            token_out: str | None = token or None
        else:  # pragma: no cover - defensive
            identity = agent_id
            token_out = credentials_hint

        self._agent_identities[agent_id] = identity
        if token_out:
            self._agent_tokens[agent_id] = token_out

        return identity, token_out

    def get_unread_mail_flags(self, agent_ids: Iterable[str]) -> Dict[str, bool]:
        """Return unread-mail flags per agent via ``fetch_inbox``.

        Network or authentication failures are treated conservatively as
        "no unread mail" for the affected agents. Callers who need richer
        error reporting should surface separate health checks for the
        Agent Mail service.
        """

        ids = list(agent_ids)
        if not ids:
            return {}

        # Ensure the project exists; this is cheap when cached, and reuse
        # the same project key that :meth:`ensure_project` returned.
        project_key = self.ensure_project()

        flags: Dict[str, bool] = {}
        for agent_id in ids:
            token = self._agent_tokens.get(agent_id)
            if not token:
                # Without a registration token we cannot authenticate;
                # treat this as "no unread mail" rather than raising.
                flags[agent_id] = False
                continue

            arguments: Dict[str, Any] = {
                "project_key": project_key,
                "agent_name": agent_id,
                "limit": 1,
                "urgent_only": False,
                "include_bodies": False,
                "unread_only": True,
                "registration_token": token,
            }

            try:
                result = self._call_tool(
                    name="fetch_inbox",
                    arguments=arguments,
                    request_id=f"nate-ntm-fetch-inbox:{agent_id}",
                    request_name=f"Agent Mail fetch_inbox({agent_id})",
                )
            except AgentMailClientError:
                flags[agent_id] = False
                continue

            if isinstance(result, list):
                flags[agent_id] = len(result) > 0
            else:
                flags[agent_id] = False

        return flags

    # ------------------------------------------------------------------
    # Low-level HTTP/JSON-RPC helpers
    # ------------------------------------------------------------------

    def _call_tool(
        self,
        *,
        name: str,
        arguments: Mapping[str, Any],
        request_id: str,
        request_name: str,
    ) -> Any:
        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": dict(arguments)},
        }
        return self._post_jsonrpc(payload, request_name=request_name)

    def _post_jsonrpc(self, payload: Mapping[str, Any], *, request_name: str) -> Any:
        body = json.dumps(payload).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"

        req = Request(self.base_url, data=body, headers=headers, method="POST")

        try:
            with urlopen(req, timeout=self.timeout) as resp:
                response_body = resp.read()
        except HTTPError as exc:  # pragma: no cover - network/HTTP error
            raise AgentMailClientError(
                f"{request_name}: HTTP {exc.code} error from Agent Mail server"
            ) from exc
        except URLError as exc:  # pragma: no cover - network error
            raise AgentMailClientError(
                f"{request_name}: failed to reach Agent Mail server"
            ) from exc

        text = response_body.decode("utf-8") if response_body else ""
        if not text:
            decoded: Any = {}
        else:
            try:
                decoded = json.loads(text)
            except ValueError as exc:  # pragma: no cover - defensive
                raise AgentMailClientError(
                    f"{request_name}: invalid JSON response from Agent Mail server"
                ) from exc

        return _extract_jsonrpc_result(decoded, request_name=request_name)

