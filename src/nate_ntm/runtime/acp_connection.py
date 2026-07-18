"""Helpers for wiring nate-oha ACP subprocesses to the ACP SDK.

This module provides a small, testable seam around the ACP SDK's stdio
transport and client connection helpers. It is intentionally narrow:
its sole job is to spawn a subprocess, bind its stdio to an
:class:`acp.client.ClientSideConnection` using the runtime's
:class:`NateNtmAcpProtocolClient`, and hand the resulting objects back
to callers.

Higher-level orchestration (for example :class:`NateOhaAcpClient` and
:class:`AcpAgentSession`) remains responsible for:

* protocol initialization and capability negotiation,
* session creation / resume and conversation identifier management,
* long-lived supervision of the subprocess and connection lifecycle.

Keeping this wiring in one place avoids duplicating ACP-specific glue
code across the runtime.
"""

from __future__ import annotations

from asyncio.subprocess import Process
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Mapping

from acp.interfaces import ClientCapabilities

from .acp_protocol_client import (
    NATE_NTM_CLIENT_CAPABILITIES,
    NateNtmAcpProtocolClient,
)
from .events import AgentEvent

# Public re-export for consumers that prefer a named type.
EventSink = Callable[[AgentEvent], None]


@asynccontextmanager
async def open_nate_oha_acp_client(
    *,
    command: list[str],
    env: Mapping[str, str] | None,
    cwd: Path,
    agent_id: str,
    event_sink: EventSink,
    capabilities: ClientCapabilities | None = None,
    use_unstable_protocol: bool = False,
) -> AsyncIterator[tuple[Any, Process, NateNtmAcpProtocolClient]]:
    """Spawn a nate-oha ACP subprocess and bind an ACP client connection.

    Parameters
    ----------
    command:
        Command line used to launch the nate-oha ACP runtime. This should
        include the executable name (for example ``["nate-oha", "acp"]``)
        and any required flags (such as ``"--config"``) but **must not**
        include stdio-related arguments; those are owned by the ACP SDK.

    env:
        Environment mapping to use for the child process. Callers are
        expected to derive this from :class:`RuntimeConfig` and
        :class:`AgentMetadata` rather than from ambient process state.

    cwd:
        Working directory for the child process. This is typically the
        project root used by the nate_ntm runtime.

    agent_id:
        Identifier of the agent this connection is associated with. This
        is threaded through into the :class:`NateNtmAcpProtocolClient`
        so that translated :class:`AgentEvent` instances carry the
        correct ``agent_id``.

    event_sink:
        Callback invoked with each :class:`AgentEvent` produced by the
        :class:`NateNtmAcpProtocolClient`.

    capabilities:
        Optional :class:`ClientCapabilities` instance to advertise
        during ACP initialization. Callers that do not need to override
        capabilities should pass ``None`` and rely on
        :data:`NATE_NTM_CLIENT_CAPABILITIES`.

    use_unstable_protocol:
        Forwarded to :class:`acp.client.ClientSideConnection` to control
        whether the unstable ACP protocol surface is enabled.

    Yields
    ------
    connection, process, protocol_client
        A 3-tuple consisting of the low-level ACP client connection,
        the spawned subprocess handle, and the associated
        :class:`NateNtmAcpProtocolClient` instance.

    Notes
    -----
    This helper intentionally **does not** perform protocol
    initialization or session creation. Callers remain responsible for
    invoking :meth:`ClientSideConnection.initialize` and
    :meth:`ClientSideConnection.new_session` / ``load_session`` as
    appropriate, and for persisting any resulting conversation
    identifiers via higher-level abstractions such as
    :class:`AgentMetadata` and :class:`AcpAgentSession`.

    The helper is expressed as an async context manager so that callers
    can tie the subprocess and connection lifetime to their own
    supervision logic. Exiting the context closes the ACP connection;
    callers are expected to coordinate this with their shutdown and
    restart policy.
    """

    # Import ACP lazily so that importing this module does not
    # immediately pull in the full ACP stack for code paths that never
    # start an ACP-backed agent.
    from acp import spawn_stdio_transport
    from acp.client import ClientSideConnection

    resolved_caps = capabilities or NATE_NTM_CLIENT_CAPABILITIES

    # Use the ACP SDK helper to launch the subprocess and obtain asyncio
    # streams for its stdio. ``spawn_stdio_transport`` owns defensive
    # shutdown semantics; the surrounding context manager keeps the
    # subprocess alive for the duration of the caller's use.
    async with spawn_stdio_transport(
        command[0],
        *command[1:],
        env=env,
        cwd=cwd,
    ) as (reader, writer, process):
        # The runtime-specific protocol client is responsible for
        # translating ACP session updates into AgentEvent instances.
        protocol_client = NateNtmAcpProtocolClient(
            agent_id=agent_id,
            event_sink=event_sink,
        )

        # ``ClientSideConnection`` binds the protocol client to the
        # underlying JSON-RPC connection over the provided streams.
        #
        # Note: the caller remains responsible for invoking
        # ``initialize`` with the desired capabilities; we simply thread
        # the resolved capabilities through as defaults for that call.
        connection = ClientSideConnection(
            protocol_client,
            writer,
            reader,
            use_unstable_protocol=use_unstable_protocol,
        )

        try:
            yield connection, process, protocol_client
        finally:
            # Ensure the ACP connection is closed before leaving the
            # context. The ``spawn_stdio_transport`` helper performs
            # subprocess shutdown and escalation once stdin has been
            # closed.
            await connection.close()
