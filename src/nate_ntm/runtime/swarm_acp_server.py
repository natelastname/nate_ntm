from __future__ import annotations

"""Swarm ACP server adapter primitives for Epic 009.

This module provides the *production* adapter surface that binds
:class:`SwarmACPMux` to concrete ACP server connections.

For User Story 1 (US1) the focus is intentionally narrow:

* create exactly one :class:`SwarmACPMux` per external ACP session; and
* expose a minimal per-session API that:

  - executes the three-stage `_attach` transaction
    (prepare → acknowledgment → activate);
  - dispatches `_detach` to :meth:`SwarmACPMux.detach`;
  - routes ordinary prompt and interrupt operations through the mux; and
  - serialises `_attach`, `_detach`, and session shutdown for one
    external session.

Higher-level concerns such as reserved-control parsing, logical error
mapping, and structured connection lifetime are covered by later tasks
(T019, T020, T025 in ``specs/009-swarm-acp-mux/tasks.md``) and build on
this minimal session primitive.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from pydantic import ValidationError
from acp import RequestError, CLIENT_METHODS
from acp import schema as acp_schema
from acp.agent.router import AGENT_METHODS
from acp.connection import Connection, StreamDirection
from acp.telemetry import span_context

from .acp_types import SessionUpdate, SessionNotification
from .acp_update_stream import AgentSessionNotActive
from .daemon import RuntimeDaemon
from .swarm_acp_mux import (
    ExternalACPConnection,
    NoAttachedAgentError,
    StaleAttachmentError,
    SwarmACPMux,
    SwarmACPMuxClosedError,
    SwarmAgentClient,
    UnknownAgentError,
    UnsupportedReservedUpdateError,
)

__all__ = [
    "SwarmACPServerSession",
    "ConnectionExternalACPConnection",
    "build_swarm_acp_request_handler",
    "SwarmACPConnection",
]

logger = logging.getLogger(__name__)



@dataclass(slots=True)
class SwarmACPServerSession:
    """Adapter-owned state for a single external ACP session.

    A new :class:`SwarmACPServerSession` instance must be created for
    each external ACP session. Construction wires a dedicated
    :class:`SwarmACPMux` for that session and provides a small, explicit
    API used by the server adapter's request handlers.

    This type deliberately **does not** own the concrete ACP transport
    or wire protocol. Callers supply an :class:`ExternalACPConnection`
    that exposes the ``session_update`` method used by the mux for
    forwarding typed :class:`SessionUpdate` objects.

    Concurrency model (US1 / contract §1):

    * For a given external session, the adapter MUST treat `_attach`,
      `_detach`, and mux/connection shutdown as a single-threaded control
      stream.
    * This is enforced here via ``_control_lock`` so that no second
      `_attach` / `_detach` / shutdown sequence overlaps an in-flight
      attachment transaction.
    * Ordinary agent-directed operations (prompt/interrupt) are routed
      directly through the mux and may be subject to adapter-specific
      concurrency rules in later tasks.
    """

    daemon: RuntimeDaemon
    agent_client: SwarmAgentClient
    external_connection: ExternalACPConnection
    external_session_id: str

    mux: SwarmACPMux = field(init=False)

    # Per-session control lock used to serialize `_attach`, `_detach`,
    # and shutdown. This is layered on top of the mux's own internal
    # lifecycle lock and follows the serialization requirements from the
    # session contract (see ``specs/009-swarm-acp-mux/contracts``).
    _control_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        """Create the per-session :class:`SwarmACPMux`.

        Exactly one mux is instantiated for each external ACP session
        and it MUST NOT be shared across sessions (spec §3, contract §1).
        """

        self.mux = SwarmACPMux(
            daemon=self.daemon,
            agent_client=self.agent_client,
            external_connection=self.external_connection,
            external_session_id=self.external_session_id,
        )

    # ------------------------------------------------------------------
    # Reserved swarm-control operations (US1 subset)
    # ------------------------------------------------------------------

    async def attach(
        self,
        agent_id: str,
        *,
        acknowledge: Callable[[str], Awaitable[None]],
    ) -> None:
        """Perform the logical `_attach` operation for this session.

        This method encodes the three-stage attachment transaction
        described in spec §8.1–§8.3 and the session contract §3.3 using
        :meth:`SwarmACPMux.attach`:

        1. ``prepare_attach(agent_id)`` establishes the internal Epic 008
           subscription via
           :meth:`SwarmAgentClient.subscribe_acp_updates`.
        2. The provided ``acknowledge`` callback is awaited to write the
           `_attach` success acknowledgment back to the external client.
        3. ``activate_attachment(prepared)`` begins forwarding retained
           and live typed updates, releasing the forwarding gate only
           after acknowledgment has succeeded.

        Acknowledgment failures or cancellation roll back only newly
        prepared attachments; pre-existing healthy attachments reused
        idempotently remain intact. These semantics are enforced by the
        mux itself; this adapter layer is responsible for serialising the
        transaction so that no second `_attach` or `_detach` overlaps
        steps (1)–(3).
        """

        logger.info(
            "session_attach",
            extra={
                "external_session_id": self.external_session_id,
                "agent_id": agent_id,
            },
        )

        async with self._control_lock:
            await self.mux.attach(agent_id, acknowledge=acknowledge)

    async def detach(self) -> None:
        """Perform the logical `_detach` operation for this session.

        Detach is serialised with `_attach` and shutdown for the same
        external session to preserve the single-threaded control-stream
        invariant. The underlying mux implements idempotent detach
        semantics and ensures that only its own subscription is removed
        from the agent's :class:`AcpSessionUpdateStream`.
        """

        async with self._control_lock:
            logger.info(
                "session_detach",
                extra={
                    "external_session_id": self.external_session_id,
                    "agent_id": self.mux.attached_agent_id,
                },
            )
            await self.mux.detach()


    async def handle_reserved_control(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Parse and dispatch a logical reserved swarm-control operation.

        The *logical* request shape mirrors the contract in
        ``specs/009-swarm-acp-mux/contracts/swarm-acp-mux-session.md``::

            {
                "op": "_swarm_status" | "_agent_detail" | "_detach",
                "payload": { ... }
            }

        This helper performs validation and dispatches to the mux and
        daemon. It intentionally does **not** construct a concrete ACP
        error object; callers should use :meth:`map_mux_error` to map
        any raised domain errors or validation failures to logical
        ``MUX_*`` error codes and wrap them in the wire-level error
        envelope mandated by the ACP SDK.

        The `_attach` operation is **not** handled here because its
        success acknowledgment must be written as part of the
        prepare/acknowledge/activate transaction. For `_attach`, the
        concrete server adapter should decode the request and call
        :meth:`attach` with an acknowledgment callback that writes the
        success response before activation.
        """

        if not isinstance(request, Mapping):
            raise ValueError("Reserved control request must be a mapping")

        try:
            op = request["op"]  # type: ignore[index]
            payload = request["payload"]  # type: ignore[index]
        except KeyError as exc:
            raise ValueError("Reserved control request must include 'op' and 'payload'") from exc

        if not isinstance(op, str):
            raise ValueError("Reserved control 'op' must be a string")

        if not isinstance(payload, Mapping):
            raise ValueError("Reserved control 'payload' must be a mapping")

        if not op.startswith("_"):
            # This helper is only for reserved controls. Any non-underscore
            # name reaching it is treated as an invalid reserved request.
            raise UnsupportedReservedUpdateError(f"Operation {op!r} is not a reserved control")

        if op == "_swarm_status":
            if payload:
                raise ValueError("_swarm_status expects an empty payload")
            return self.mux.get_swarm_status()

        if op == "_agent_detail":
            try:
                agent_id = payload["agent_id"]  # type: ignore[index]
            except KeyError as exc:
                raise ValueError("_agent_detail payload must include 'agent_id'") from exc

            if not isinstance(agent_id, str):
                raise ValueError("agent_id must be a string")

            max_events_obj: Any | None = payload.get("max_events")  # type: ignore[call-arg]
            if max_events_obj is None:
                return self.mux.get_agent_detail(agent_id=agent_id)

            if not isinstance(max_events_obj, int):
                raise ValueError("max_events must be an integer")

            return self.mux.get_agent_detail(agent_id=agent_id, max_events=max_events_obj)

        if op == "_attach":
            # `_attach` success responses must be written inside the
            # prepare/acknowledge/activate transaction and therefore
            # cannot be modeled as a simple logical return value here.
            # The concrete server adapter should decode `_attach`
            # requests and call :meth:`attach` with an acknowledgment
            # callback that writes the success response before
            # activation.
            raise UnsupportedReservedUpdateError(
                "_attach must be handled via SwarmACPServerSession.attach(...)"
            )

        if op == "_detach":
            if payload:
                raise ValueError("_detach expects an empty payload")
            await self.detach()
            return {"detached": True}

        # Any other underscore-prefixed operation is treated as an
        # unsupported reserved control.
        raise UnsupportedReservedUpdateError(f"Unknown reserved control operation {op!r}")

    # ------------------------------------------------------------------
    # Ordinary agent-directed operations
    # ------------------------------------------------------------------

    async def prompt(self, text: str) -> str | None:
        """Send a prompt to the currently attached agent via the mux.

        Preconditions (enforced by :class:`SwarmACPMux`):

        * the mux must be open; and
        * an agent must be attached.

        Violations raise :class:`SwarmACPMuxClosedError` or
        :class:`NoAttachedAgentError` respectively and are mapped to
        logical error codes by higher-level adapter logic in later
        tasks.
        """

        return await self.mux.prompt(text)

    async def interrupt(self) -> None:
        """Request an interrupt for the currently attached agent via the mux."""

        await self.mux.interrupt()

    # ------------------------------------------------------------------
    # Session shutdown / cleanup
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Connection lifetime: first-completion race (US3)
    # ------------------------------------------------------------------

    async def run_connection(
        self,
        serve_inbound: Callable[["SwarmACPServerSession"], Awaitable[None]],
        *,
        close_transport: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Run inbound handling and mux failure monitoring to first completion.

        This helper implements the structured connection lifetime described in
        spec §10 and T025 in ``specs/009-swarm-acp-mux/tasks.md``.

        For one external ACP session it:

        * starts inbound request processing via ``serve_inbound(self)``;
        * starts a watcher task that awaits :meth:`SwarmACPMux.wait_failed`;
        * awaits whichever task completes first; and
        * cancels and awaits the losing task.

        The *winner's* result or exception is propagated to the caller. Normal
        inbound completion therefore terminates the connection, inbound
        failures propagate as adapter errors, and forwarding failures propagate
        from :meth:`SwarmACPMux.wait_failed`.

        After the race completes, the helper closes the per-session mux and
        then invokes ``close_transport`` exactly once if provided. The concrete
        ACP server adapter should supply a callback that terminates the
        underlying transport and wire protocol for this external session.
        """

        logger.info(
            "session_run_connection_start",
            extra={"external_session_id": self.external_session_id},
        )

        try:
            # Use the session as an async context manager so that the per-session
            # mux is always closed exactly once when the connection ends.
            async with self:
                loop = asyncio.get_running_loop()

                inbound_task = loop.create_task(
                    serve_inbound(self),
                    name=f"swarm-acp-inbound:{self.external_session_id}",
                )
                failure_task = loop.create_task(
                    self.mux.wait_failed(),
                    name=f"swarm-acp-forwarding-watch:{self.external_session_id}",
                )

                done, pending = await asyncio.wait(
                    {inbound_task, failure_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                inbound_done = inbound_task in done
                failure_done = failure_task in done

                logger.info(
                    "session_run_connection_winner",
                    extra={
                        "external_session_id": self.external_session_id,
                        "inbound_done": inbound_done,
                        "failure_done": failure_done,
                    },
                )

                # Cancel whichever side lost the race and await it to avoid leaked
                # tasks or unobserved exceptions.
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

                # Propagate the winner's result or exception.
                for task in done:
                    task.result()
        finally:
            if close_transport is not None:
                try:
                    await close_transport()
                finally:
                    logger.info(
                        "session_run_connection_transport_closed",
                        extra={"external_session_id": self.external_session_id},
                    )




    async def close(self) -> None:
        """Close the mux and serialise shutdown with attach/detach.

        This helper is intended to be invoked when the external ACP
        session terminates. It ensures that:

        * no new `_attach` / `_detach` begins while shutdown is in
          progress; and
        * the underlying mux is always closed exactly once.

        Mapping to wire-level behaviour (for example, terminating the
        concrete ACP transport) is owned by higher-level server code and
        is implemented in later tasks.
        """

        async with self._control_lock:
            logger.info(
                "session_close",
                extra={
                    "external_session_id": self.external_session_id,
                    "attached_agent_id": self.mux.attached_agent_id,
                },
            )
            await self.mux.close()

    @staticmethod
    def map_mux_error(exc: BaseException) -> str:
        """Map mux/domain errors and validation failures to logical ``MUX_*`` codes.

        The mapping is defined in the Swarm ACP mux session contract
        (§2 "Error Codes (Logical)"). The returned code is a
        transport-independent logical value; callers are responsible for
        embedding it into the concrete ACP error envelope.
        """

        if isinstance(exc, SwarmACPMuxClosedError):
            return "MUX_CLOSED"
        if isinstance(exc, NoAttachedAgentError):
            return "MUX_NO_ATTACHED_AGENT"
        if isinstance(exc, UnknownAgentError):
            return "MUX_UNKNOWN_AGENT"
        if isinstance(exc, AgentSessionNotActive):
            return "MUX_AGENT_SESSION_NOT_ACTIVE"
        if isinstance(exc, StaleAttachmentError):
            return "MUX_STALE_ATTACHMENT"
        if isinstance(exc, (UnsupportedReservedUpdateError, ValueError, KeyError, TypeError)):
            # Unknown or malformed reserved control, or validation failure.
            return "MUX_INVALID_REQUEST"

        # Anything else is treated as an unexpected internal failure.
        logger.error("Unhandled error in SwarmACPServerSession", exc_info=exc)
        return "MUX_INTERNAL_ERROR"

    async def __aenter__(self) -> "SwarmACPServerSession":
        # Allow connection handlers to use the session as an async
        # context manager in later user stories. The mux is fully
        # initialised at construction time so we simply return ``self``.
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        await self.close()


# ---------------------------------------------------------------------------
# Concrete ACP transport adapter helpers (US3 Addendum)
# ---------------------------------------------------------------------------


class ConnectionExternalACPConnection(ExternalACPConnection):  # type: ignore[misc]
    """ExternalACPConnection backed by an ACP :class:`Connection`.

    This is the production bridge used by :class:`SwarmACPMux` to forward
    typed :class:`SessionUpdate` models to an external ACP client over
    JSON-RPC 2.0. The mux itself remains unaware of JSON or wire-level
    details and works purely with the typed :class:`SessionUpdate` alias.

    Telemetry continues to flow exclusively through the Epic 008 typed
    update layer: the concrete ACP connection only ever sees
    :class:`SessionNotification` models constructed from
    :class:`SessionUpdate` instances.

    The :class:`Connection` instance is bound lazily so that callers can
    construct the :class:`SwarmACPServerSession` and underlying
    :class:`Connection` in whichever order is most convenient. The bridge
    MUST be bound via :meth:`bind` before any updates are forwarded.
    """

    def __init__(self, conn: Connection | None = None) -> None:  # pragma: no cover - trivial initialiser
        self._conn: Connection | None = conn

    def bind(self, conn: Connection) -> None:
        """Bind the underlying :class:`Connection` used for forwarding.

        This method may be called at most once; rebinding is not supported.
        """

        if self._conn is not None and self._conn is not conn:
            raise RuntimeError("ConnectionExternalACPConnection is already bound")
        self._conn = conn

    async def session_update(self, *, session_id: str, update: SessionUpdate) -> None:
        """Forward a typed :class:`SessionUpdate` over the ACP connection."""

        if self._conn is None:  # pragma: no cover - defensive programming
            raise RuntimeError("ConnectionExternalACPConnection is not bound to a Connection")

        notification = SessionNotification(session_id=session_id, update=update)
        payload = notification.model_dump(mode="json", by_alias=True, exclude_none=True)
        await self._conn.send_notification(CLIENT_METHODS["session_update"], payload)


def _raise_acp_error_from_mux(exc: BaseException) -> None:
    """Translate mux/domain errors into concrete ACP :class:`RequestError`.

    The logical ``MUX_*`` codes remain transport-independent and are
    embedded into the JSON-RPC error's ``data`` field under the
    ``"mux_code"`` key. Callers MUST pass in the original domain
    exception so that logging and error mapping remain consistent across
    reserved controls and ordinary agent-directed operations.
    """

    if isinstance(exc, RequestError):
        # Already a concrete ACP error; surface as-is.
        raise exc

    code = SwarmACPServerSession.map_mux_error(exc)
    data = {"mux_code": code}

    if code == "MUX_INVALID_REQUEST":
        # Malformed reserved-control payloads and similar validation
        # failures are reported as JSON-RPC "Invalid params".
        raise RequestError.invalid_params(data) from exc

    # All other mux/domain failures map to "Internal error" with the
    # stable logical code surfaced in the error data.
    raise RequestError.internal_error(data) from exc


def build_swarm_acp_request_handler(
    session: SwarmACPServerSession,
) -> Callable[[str, Any | None, bool], Awaitable[Any]]:
    """Return a Connection handler that exposes the swarm over ACP.

    The returned callable matches the :class:`acp.connection.Connection`
    ``MethodHandler`` protocol::

        async def handler(method: str, params: Any | None, is_notification: bool) -> Any: ...

    Behaviour:

    * routes ``session/prompt`` requests to :meth:`SwarmACPServerSession.prompt`;
    * routes ``session/cancel`` requests and notifications to
      :meth:`SwarmACPServerSession.interrupt`;
    * decodes reserved swarm controls (``_swarm_status``, ``_agent_detail``,
      ``_detach``) and dispatches them via
      :meth:`SwarmACPServerSession.handle_reserved_control`;
    * rejects ``_attach`` via :meth:`handle_reserved_control` so callers must
      use :meth:`SwarmACPServerSession.attach` with an explicit acknowledgment
      callback; and
    * maps mux/domain failures to concrete :class:`RequestError` instances
      using :func:`_raise_acp_error_from_mux`.

    This helper deliberately keeps all ACP-specific decoding and error
    mapping in one place so that transport wiring code can remain thin.
    """

    prompt_method = AGENT_METHODS["session_prompt"]
    cancel_method = AGENT_METHODS["session_cancel"]

    async def handler(method: str, params: Any | None, is_notification: bool) -> Any:
        # Ordinary ACP agent-directed operations ---------------------------------
        if method == prompt_method and not is_notification:
            try:
                request = acp_schema.PromptRequest.model_validate(params or {})
            except ValidationError as exc:
                # Malformed base ACP request -> JSON-RPC invalid_params.
                raise RequestError.invalid_params({"reason": "Invalid PromptRequest"}) from exc

            # Map the structured ACP prompt blocks to the logical mux prompt
            # contract, which currently exposes a simple UTF-8 text string.
            #
            # For Epic 009 we restrict ourselves to text-only prompts. When
            # multiple text blocks are present, we concatenate their ``text``
            # fields in order; non-text content blocks are ignored. This keeps
            # the Swarm ACP adapter compatible with the existing
            # :class:`NateOhaAcpClient` interface without introducing a second
            # prompt representation.
            prompt_text_parts: list[str] = []
            for block in request.prompt:
                if isinstance(block, acp_schema.TextContentBlock) and getattr(block, "text", None):
                    prompt_text_parts.append(block.text)
            prompt_text = "".join(prompt_text_parts)

            try:
                result_text = await session.prompt(prompt_text)
            except BaseException as exc:  # pragma: no cover - mapped by _raise_acp_error_from_mux
                _raise_acp_error_from_mux(exc)

            # The logical Swarm session contract exposes prompt/interrupt
            # behaviour via typed SessionUpdate streaming rather than tight
            # coupling to PromptResponse fields. We therefore return a
            # minimal, structurally valid PromptResponse and surface the
            # optional textual result (if any) in ``field_meta`` for clients
            # that care about it.
            response = acp_schema.PromptResponse(
                stop_reason="end_turn",
                usage=None,
                field_meta={"swarm_output": result_text} if result_text is not None else None,
            )
            return response.model_dump(mode="json", by_alias=True, exclude_none=True)

        if method == cancel_method:
            # Treat both requests and notifications as best-effort interrupts.
            try:
                acp_schema.CancelNotification.model_validate(params or {})
            except ValidationError as exc:
                raise RequestError.invalid_params({"reason": "Invalid CancelNotification"}) from exc

            try:
                await session.interrupt()
            except BaseException as exc:  # pragma: no cover - mapped by _raise_acp_error_from_mux
                _raise_acp_error_from_mux(exc)

            # Notifications ignore the return value; requests receive a
            # JSON-RPC result of ``null``.
            return None

        # Reserved swarm controls -------------------------------------------------
        if isinstance(method, str) and method.startswith("_"):
            # Reserved controls are modelled as extension methods whose params
            # object becomes the logical ``payload`` mapping.
            if params is None:
                payload: Mapping[str, Any] = {}
            elif isinstance(params, Mapping):
                payload = params
            else:
                _raise_acp_error_from_mux(ValueError("Reserved control params must be an object"))

            logical_request = {"op": method, "payload": payload}

            try:
                return await session.handle_reserved_control(logical_request)
            except BaseException as exc:  # pragma: no cover - mapped by _raise_acp_error_from_mux
                _raise_acp_error_from_mux(exc)

        # Anything else is a missing method from the ACP client's perspective.
        raise RequestError.method_not_found(method)

    return handler



class SwarmACPConnection(Connection):  # type: ignore[misc]
    """Specialised :class:`Connection` that implements `_attach` semantics.

    This subclass delegates most methods to :class:`acp.connection.Connection`
    but overrides :meth:`_run_request` to route the reserved `_attach`
    operation through :class:`SwarmACPServerSession.attach` with an
    acknowledgment callback that sends the JSON-RPC success response *before*
    activating the mux attachment.

    All other methods (including `_swarm_status`, `_agent_detail`, `_detach`,
    and ordinary agent-directed operations) are handled by the
    :func:`build_swarm_acp_request_handler` provided at construction time and
    use the default :class:`Connection` request handling behaviour.
    """

    def __init__(
        self,
        *,
        session: SwarmACPServerSession,
        writer: asyncio.StreamWriter,
        reader: asyncio.StreamReader,
        receive_timeout: float | None = None,
    ) -> None:
        self._session = session
        handler = build_swarm_acp_request_handler(session)
        super().__init__(
            handler=handler,
            writer=writer,
            reader=reader,
            listening=False,
            receive_timeout=receive_timeout,
        )

    async def _run_request(self, message: dict[str, Any]) -> Any:  # type: ignore[override]
        method = message.get("method")
        if method == "_attach":
            return await self._run_attach_request(message)
        return await super()._run_request(message)

    async def _run_attach_request(self, message: dict[str, Any]) -> Any:
        """Handle a single `_attach` request with correct ack ordering.

        This method:

        * validates the logical `_attach` payload shape;
        * calls :meth:`SwarmACPServerSession.attach` with an acknowledgment
          callback that sends the JSON-RPC success response using this
          connection's sender; and
        * maps mux/domain failures to :class:`RequestError` instances with the
          appropriate logical ``MUX_*`` code embedded in ``error.data``.
        """

        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
        method = message["method"]

        with span_context("acp.request", attributes={"method": method}):
            try:
                params = message.get("params") or {}
                if not isinstance(params, Mapping):
                    raise ValueError("_attach params must be an object")

                try:
                    agent_id = params["agent_id"]  # type: ignore[index]
                except KeyError as exc:
                    raise ValueError("_attach payload must include 'agent_id'") from exc

                if not isinstance(agent_id, str):
                    raise ValueError("agent_id must be a string")

                async def acknowledge(attached_id: str) -> None:
                    # Sanity check: mux and adapter must agree on the agent id.
                    if attached_id != agent_id:
                        raise RuntimeError(
                            "SwarmACPConnection: acknowledgment agent_id mismatch"
                        )

                    result_obj = {"attached_agent_id": attached_id}
                    payload["result"] = result_obj
                    await self._sender.send(payload)
                    self._notify_observers(StreamDirection.OUTGOING, payload)

                # Perform the three-stage attachment transaction using the
                # server session helper. The acknowledgment callback writes
                # the `_attach` success response before activation begins.
                await self._session.attach(agent_id, acknowledge=acknowledge)

                # For symmetry with the base implementation, return the
                # result object that was sent to the client.
                return payload.get("result")

            except RequestError as exc:
                # If a concrete RequestError is raised directly, surface it
                # using the standard JSON-RPC error envelope.
                payload["error"] = exc.to_error_obj()
                await self._sender.send(payload)
                self._notify_observers(StreamDirection.OUTGOING, payload)
                raise
            except Exception as exc:
                # Map mux/domain failures and validation errors to ACP-level
                # RequestError instances carrying a stable logical code.
                try:
                    _raise_acp_error_from_mux(exc)
                except RequestError as mapped:
                    payload["error"] = mapped.to_error_obj()
                    await self._sender.send(payload)
                    self._notify_observers(StreamDirection.OUTGOING, payload)
                    raise

