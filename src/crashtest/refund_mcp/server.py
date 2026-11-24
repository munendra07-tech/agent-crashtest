"""MCP server exposing refund creation and read-only status tools backed by the sink.

Run with:  python -m crashtest.refund_mcp.server   (Streamable HTTP)
           crashtest mcp --stdio                     (stdio; see crashtest.cli)
Env:       SINK_URL (default http://127.0.0.1:8101), MCP_HOST, MCP_PORT,
           MCP_STATELESS and MCP_JSON_RESPONSE (both default true),
           MCP_EVENT_STORE (default false: an in-memory event store, for
           resumable SSE streams; used only by a stateful server),
           MCP_PROGRESS_DELAY_S (default unset: when set, the tool first sends
           one progress notification, then waits this long before it calls
           the sink; experiment 008's mid-stream cut needs bytes on the
           stream before the commit),
           MCP_DNS_REBINDING_PROTECTION (default true: the Host and Origin
           headers must name 127.0.0.1, localhost or [::1], any port) and
           MCP_ALLOWED_HOSTS (extra allowed Host values, comma-separated, such
           as Docker container names "refund-mcp:*"; each also becomes an
           allowed http:// Origin)

The server adds no deduplication of its own. It forwards an optional
idempotency key to the sink as a header, the way a payments client would.
"""

from __future__ import annotations

import asyncio
import os
from typing import Literal, cast, get_args

import httpx
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel

from crashtest.refund_mcp.event_store import InMemoryEventStore

SINK_URL = os.environ.get("SINK_URL", "http://127.0.0.1:8101")

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def _log_level(value: str) -> LogLevel:
    level = value.upper()
    if level not in get_args(LogLevel):
        raise ValueError(f"MCP_LOG_LEVEL must be one of {get_args(LogLevel)}, got {value!r}")
    return cast(LogLevel, level)


def _progress_delay(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    delay = float(value)
    if delay < 0:
        raise ValueError(f"MCP_PROGRESS_DELAY_S must not be negative, got {value!r}")
    return delay


# Experiment 008. None (the default, and experiments 001 to 007): no progress
# notification and no pause, so the tool behaves exactly as before.
PROGRESS_DELAY_S = _progress_delay(os.environ.get("MCP_PROGRESS_DELAY_S"))

mcp = MCPServer("refund-service", log_level=_log_level(os.environ.get("MCP_LOG_LEVEL", "WARNING")))


class RefundResult(BaseModel):
    """Typed result, so the SDK returns structured content and not only text."""

    refund_id: int
    order_id: str
    amount_cents: int
    replayed: bool


class RefundStatus(BaseModel):
    """Whether the sink has recorded a refund for an order."""

    exists: bool


@mcp.tool()
async def issue_refund(
    order_id: str,
    amount_cents: int,
    idempotency_key: str | None = None,
    ctx: Context | None = None,
) -> RefundResult:
    """Refund an order. This moves money and must not be repeated for the same order.

    Args:
        order_id: The order to refund.
        amount_cents: Amount to refund, in cents.
        idempotency_key: Optional. A caller-chosen key identifying this one refund
            operation. Calls repeating the same key return the original refund.
    """
    if PROGRESS_DELAY_S is not None and ctx is not None:
        # Sent only if the client asked for progress (a progress token); either
        # way the pause comes before the sink call, so before the commit.
        await ctx.report_progress(0, 1, "refund started")
        await asyncio.sleep(PROGRESS_DELAY_S)
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{SINK_URL}/refunds",
            json={"order_id": order_id, "amount_cents": amount_cents},
            headers=headers,
        )
    response.raise_for_status()
    return RefundResult.model_validate(response.json())


@mcp.tool()
async def check_refund_status(order_id: str) -> RefundStatus:
    """Check whether a refund exists without changing sink state.

    @param order_id: The order whose refund status should be checked.
    @returns: Whether the sink has recorded a refund for the order.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(f"{SINK_URL}/refunds", params={"order_id": order_id})
    response.raise_for_status()
    return RefundStatus(exists=bool(response.json()["refunds"]))


def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# Host and Origin values a local client sends. A web page the developer opens
# cannot make a browser send these for a rebinding attacker's own domain.
LOCAL_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")


def transport_security() -> TransportSecuritySettings:
    """DNS-rebinding protection: on by default, local hosts only.

    The MCP SDK checks every HTTP request's Host header (421 when it is not
    allowed) and Origin header, if present (403). Docker Compose keeps the check
    on and adds its container names through MCP_ALLOWED_HOSTS; turning it off
    takes MCP_DNS_REBINDING_PROTECTION=false, an explicit setting.
    """
    if not _env_flag("MCP_DNS_REBINDING_PROTECTION", True):
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    extra = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    hosts = [*LOCAL_HOSTS, *extra]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"http://{h}" for h in hosts],
    )


def run_stdio(sink_url: str) -> None:
    """Serve the same tool over stdio (stdout carries only the protocol)."""
    global SINK_URL
    SINK_URL = sink_url.rstrip("/")
    mcp.run("stdio")


def main() -> None:
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8102"))
    mcp.run(
        "streamable-http",
        host=host,
        port=port,
        # Default: one request, one JSON response, no server-side session. That is
        # the simplest transport behaviour, so the baseline measures retries and
        # nothing else. Experiment 002 turns each of these off in turn.
        stateless_http=_env_flag("MCP_STATELESS", True),
        json_response=_env_flag("MCP_JSON_RESPONSE", True),
        # Experiment 007: with a store, an SSE response opens with a priming event
        # (an event ID, no data) and a client that loses the stream can resume it
        # with Last-Event-ID. `retry_interval` is left unset, so the client waits
        # its own default before reconnecting.
        event_store=InMemoryEventStore() if _env_flag("MCP_EVENT_STORE", False) else None,
        transport_security=transport_security(),
    )


if __name__ == "__main__":
    main()
