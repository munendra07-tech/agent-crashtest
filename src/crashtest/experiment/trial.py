"""One trial of the MCP refund experiment.

A trial is one logical operation: "refund order X once". The client calls the
`issue_refund` MCP tool, and if the call times out it retries, the way a naive
application-level retry loop does. The verdict comes only from what the sink
recorded, never from what the client believes happened.
"""

from __future__ import annotations

import asyncio
import importlib
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Literal

import anyio
import h11
import httpcore
import httpx
from mcp import Client, MCPError

from crashtest import control_token
from crashtest.checker import (
    REPORT_OF_STATUS,
    Report,
    SubjectStatus,
    Verdict,
    classify,
    payload_matches,
    report_matches_sink,
)
from crashtest.faults.drop_proxy import DropPoint, DropProxy

Arm = Literal["no_dedup", "stable_key", "fresh_key_per_attempt"]
Fault = Literal["none", "lost_response", "connection_drop"]

ARMS: tuple[Arm, ...] = ("no_dedup", "stable_key", "fresh_key_per_attempt")
FAULTS: tuple[Fault, ...] = ("none", "lost_response", "connection_drop")

REQUEST_TIMEOUT_CODE = -32001  # JSON-RPC code the MCP SDK raises when a request times out
CONNECTION_CLOSED_CODE = -32000  # ... and when the connection closes under a pending request

# Outcomes that leave the caller not knowing whether the effect happened.
AMBIGUOUS_OUTCOMES = ("timeout", "disconnect")


@dataclass
class Attempt:
    number: int
    idempotency_key: str | None
    outcome: str  # "ok", "timeout", "disconnect", or "error:<detail>"
    elapsed_s: float
    replayed: bool | None = None
    raised: str | None = None  # the exception type behind a failed attempt
    # The MCP protocol version the client negotiated, if it connected.
    protocol: str | None = None
    # Experiment 008: progress notifications the client delivered to its callback
    # during this attempt (None when the trial did not ask for progress).
    progress_seen: int | None = None


@dataclass
class TrialResult:
    arm: Arm
    fault: Fault
    order_id: str
    intended_effects: int
    effects_committed: int
    requests_received: int
    client_saw_success: bool
    verdict: Verdict
    attempts: list[Attempt] = field(default_factory=list)
    transport: str = "stateless_json"
    # Requests the sink received beyond the application's own attempts. Above zero
    # means something below the application (the SDK or the transport) resent.
    sdk_extra_requests: int = 0
    # The same difference with its sign kept: below zero means an attempt never
    # reached the sink, which the clamped figure above would hide.
    request_delta: int = 0
    # Tool calls the client put on the wire, counted at the proxy. None when the
    # trial did not run through a proxy.
    client_requests: int | None = None
    # Whether the injected fault is known to have fired after the commit. None
    # for trials with no fault.
    fault_confirmed: bool | None = None
    # What the subject told its caller agrees with the sink (contract v0.3, RA).
    report_matches_sink: bool | None = None
    # Experiment 007. The client's connect mode ("auto" is the SDK default), where
    # the proxy cut the connection (None with no drop), and how many client
    # requests carrying Last-Event-ID (a resumed stream) the proxy saw during the
    # trial (None without a proxy).
    client_mode: str = "auto"
    drop_point: str | None = None
    resume_requests: int | None = None
    # The proxy's sink reads retried after a dropped keep-alive connection, in this trial.
    sink_retries: int | None = None
    # True if the sink's counts moved between the two ground-truth reads.
    changed_after_first_read: bool = False
    # Set by the runner after the last trial: the order's refund count when the
    # whole run had finished differed from the count this trial recorded.
    changed_by_end_of_run: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _http_error_types() -> tuple[tuple[type[BaseException], ...], tuple[type[BaseException], ...]]:
    """Timeout and disconnect exception types from every HTTP stack that may be in use.

    mcp 2.x talks HTTP through `httpx2`/`httpcore2`, not `httpx`/`httpcore`; both
    families are covered so the classification survives either. A cut connection
    can also surface from any layer, depending on where the SDK was reading.
    """
    timeouts: list[type[BaseException]] = [TimeoutError, httpx.TimeoutException]
    disconnects: list[type[BaseException]] = [
        httpx.TransportError,
        httpcore.NetworkError,
        httpcore.ProtocolError,
        h11.RemoteProtocolError,
        anyio.ClosedResourceError,
        anyio.BrokenResourceError,
        anyio.EndOfStream,
        ConnectionError,
    ]
    for client_name, core_name in (("httpx2", "httpcore2"),):
        try:
            client = importlib.import_module(client_name)
            core = importlib.import_module(core_name)
        except ImportError:
            continue
        timeouts += [client.TimeoutException, core.TimeoutException]
        disconnects += [client.TransportError, core.NetworkError, core.ProtocolError]
    return tuple(timeouts), tuple(disconnects)


_TIMEOUT_ERRORS, _DISCONNECT_ERRORS = _http_error_types()


def _classify_failure(exc: BaseException) -> str:
    """Map a failed call to "timeout", "disconnect" or "error:<detail>".

    The SDK raises inside the client's task group, so the failure can arrive
    wrapped in an exception group when the connection closes; unwrap it first.
    A group is ambiguous if any member is: a dropped connection often surfaces
    as a disconnect plus whatever else was cancelled alongside it.
    """
    if isinstance(exc, BaseExceptionGroup):
        kinds = [_classify_failure(e) for e in exc.exceptions]
        for ambiguous in AMBIGUOUS_OUTCOMES:
            if ambiguous in kinds:
                return ambiguous
        if len(exc.exceptions) == 1:
            return kinds[0]
        return "error:" + "+".join(type(e).__name__ for e in exc.exceptions)
    if isinstance(exc, _TIMEOUT_ERRORS):
        return "timeout"
    if isinstance(exc, _DISCONNECT_ERRORS):
        return "disconnect"
    if isinstance(exc, MCPError):
        code = getattr(exc, "code", None)
        if code == REQUEST_TIMEOUT_CODE:
            return "timeout"
        if code == CONNECTION_CLOSED_CODE:
            return "disconnect"
        return f"error:mcp_{code}"
    return f"error:{type(exc).__name__}"


def _describe(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        return "group(" + ", ".join(_describe(e) for e in exc.exceptions) + ")"
    code = getattr(exc, "code", None) if isinstance(exc, MCPError) else None
    return type(exc).__name__ + (f"[{code}]" if code is not None else "")


def _progress_counter(attempt: Attempt):
    async def on_progress(progress: float, total: float | None, message: str | None) -> None:
        attempt.progress_seen = (attempt.progress_seen or 0) + 1

    return on_progress


def _key_for_attempt(arm: Arm, operation_key: str) -> str | None:
    if arm == "no_dedup":
        return None
    if arm == "stable_key":
        # Chosen once per logical operation and reused on every retry.
        return operation_key
    # A new key on every attempt. This models a caller (or a model) that
    # regenerates the request instead of repeating it: deduplication cannot match.
    return f"attempt-{uuid.uuid4()}"


async def run_trial(
    *,
    arm: Arm,
    fault: Fault,
    mcp_url: str,
    sink_url: str,
    client_timeout_s: float = 2.0,
    fault_delay_s: float = 5.0,
    max_attempts: int = 3,
    amount_cents: int = 2500,
    transport: str = "stateless_json",
    drop_proxy: DropProxy | None = None,
    settle_s: float = 0.3,
    client_mode: Literal["auto", "legacy"] = "auto",
    drop_point: DropPoint = "no_bytes",
    progress: bool = False,
) -> TrialResult:
    order_id = f"ord-{uuid.uuid4()}"
    operation_key = f"refund-{order_id}"
    attempts: list[Attempt] = []
    client_saw_success = False

    if fault == "connection_drop" and drop_proxy is None:
        raise ValueError("fault 'connection_drop' needs a drop_proxy in front of the MCP server")
    if drop_proxy is not None:
        # With a proxy, every call goes through it, so it can count what the
        # client actually sent. It only interferes when armed.
        mcp_url = drop_proxy.url
        drops_before = len(drop_proxy.drops)
        resumes_before = drop_proxy.resume_requests
        sink_retries_before = drop_proxy.sink_retries
        if fault == "connection_drop":
            # The proxy lets the call through, waits for the refund to commit, then
            # cuts this client's connection. Only the first attempt is affected.
            drop_proxy.arm(order_id, drop_point)
        else:
            drop_proxy.watch(order_id)

    async with httpx.AsyncClient(
        base_url=sink_url, timeout=30.0, headers=control_token.control_headers(sink_url)
    ) as sink:
        if fault == "lost_response":
            # The sink will commit the first refund for this order, then sit on
            # the response for longer than the client is willing to wait.
            r = await sink.put(
                "/control/faults",
                json={"order_id": order_id, "delay_s": fault_delay_s, "times": 1},
            )
            r.raise_for_status()

        for number in range(1, max_attempts + 1):
            key = _key_for_attempt(arm, operation_key)
            arguments: dict = {"order_id": order_id, "amount_cents": amount_cents}
            if key is not None:
                arguments["idempotency_key"] = key

            started = time.perf_counter()
            attempt = Attempt(number=number, idempotency_key=key, outcome="", elapsed_s=0.0)
            if progress:
                attempt.progress_seen = 0
            # Experiment 008: asking for progress gives the call a progress token,
            # so a server that reports progress puts an event on the stream.
            on_progress = _progress_counter(attempt) if progress else None
            try:
                # A fresh connection per attempt: the retry does not depend on
                # whatever state a timed-out session was left in.
                async with Client(mcp_url, mode=client_mode) as client:
                    attempt.protocol = client.session.protocol_version
                    result = await client.call_tool(
                        "issue_refund",
                        arguments,
                        read_timeout_seconds=client_timeout_s,
                        progress_callback=on_progress,
                    )
                if result.is_error:
                    attempt.outcome = "error:tool_error"
                else:
                    attempt.outcome = "ok"
                    structured = result.structured_content or {}
                    attempt.replayed = structured.get("replayed")
            except (Exception, BaseExceptionGroup) as exc:
                attempt.outcome = _classify_failure(exc)
                attempt.raised = _describe(exc)
            attempt.elapsed_s = round(time.perf_counter() - started, 3)
            attempts.append(attempt)

            if attempt.outcome == "ok":
                client_saw_success = True
                break
            if attempt.outcome not in AMBIGUOUS_OUTCOMES:
                break  # only an ambiguous outcome is retried

        # Ground truth, read twice. With an injected fault the effect is committed
        # before the response is withheld, so the first read already sees it. A
        # request that timed out with no fault may still be in flight, so the sink
        # is read again after a pause and any change is recorded.
        first_read = await _read_truth(sink, order_id)
        await asyncio.sleep(settle_s)
        truth = await _read_truth(sink, order_id)

    client_requests = drop_proxy.unwatch(order_id) if drop_proxy is not None else None
    resume_requests = (
        drop_proxy.resume_requests - resumes_before if drop_proxy is not None else None
    )
    # The proxy's sink reads that were retried after a dropped connection.
    sink_retries = drop_proxy.sink_retries - sink_retries_before if drop_proxy is not None else None

    fault_confirmed: bool | None = None
    if fault == "lost_response":
        fault_confirmed = any(r.get("fault_applied") for r in truth["requests"])
    elif fault == "connection_drop" and drop_proxy is not None:
        # Where the drop point says: after the commit for 002's and 007's points;
        # before anything reached the server, or mid-stream before the commit, for
        # 008's (crashtest.faults.drop_proxy.DropRecord.landed_as_intended).
        fault_confirmed = any(
            d.order_id == order_id and d.landed_as_intended()
            for d in drop_proxy.drops[drops_before:]
        )

    if client_saw_success:
        status: SubjectStatus = "completed"
    elif attempts and attempts[-1].outcome in AMBIGUOUS_OUTCOMES:
        status = "unknown"
    else:
        status = "failed"
    # What the client told its caller: success, an ambiguous error, or a definite one.
    told: Report = REPORT_OF_STATUS[status]

    effects = len(truth["refunds"])
    requests = len(truth["requests"])
    intended = 1
    return TrialResult(
        arm=arm,
        fault=fault,
        order_id=order_id,
        intended_effects=intended,
        effects_committed=effects,
        requests_received=requests,
        client_saw_success=client_saw_success,
        verdict=classify(
            effects=effects,
            intended=intended,
            status=status,
            payload_matches=payload_matches(
                truth["refunds"], order_id=order_id, amount_cents=amount_cents
            ),
            # Completion here is only ever reported on a success response from
            # the server (first-hand or a replay), so it is always confirmed.
            confirmed=client_saw_success,
        ),
        attempts=attempts,
        transport=transport,
        sdk_extra_requests=max(0, requests - len(attempts)),
        request_delta=requests - len(attempts),
        client_requests=client_requests,
        fault_confirmed=fault_confirmed,
        report_matches_sink=report_matches_sink(effects=effects, intended=intended, report=told),
        client_mode=client_mode,
        drop_point=drop_point if fault == "connection_drop" else None,
        resume_requests=resume_requests,
        sink_retries=sink_retries,
        changed_after_first_read=(
            len(first_read["refunds"]) != effects or len(first_read["requests"]) != requests
        ),
    )


async def _read_truth(sink: httpx.AsyncClient, order_id: str) -> dict:
    r = await sink.get("/refunds", params={"order_id": order_id})
    r.raise_for_status()
    return r.json()
