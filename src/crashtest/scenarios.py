"""Registered scenarios: what `crashtest run`, `selftest` and the controller can test.

A scenario is one simulated effect with one fault, an independent ground truth,
and documented limitations. Each has a name and a version; a report names both.

  refunds                     the reference: the sink commits the refund, then
                              withholds its response past the agent's timeout
                              (lost response; experiments 001 to 003)
  refunds-cut-before-request  a TCP proxy between the agent and the MCP server
                              cuts the connection before the call reaches the
                              server, so nothing is refunded; the client sees the
                              same error as a cut after the commit (experiment 008)

The scenario decides how its fault is armed and how "the fault fired" is known.
The verdict never depends on the scenario: every trial is judged from the sink's
rows by `crashtest.cli.judge` (the checker), as before.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

import httpx

from crashtest import control_token as ct

DEFAULT = "refunds"


@dataclass(frozen=True)
class Scenario:
    name: str
    version: str
    fault: str  # the fault's code in reports
    title: str
    scope: str  # what a run tests, and what it does not; printed with every report
    defaults: dict[str, Any]
    # The trial counts as a safety failure when the sink holds no refund although the
    # fault fired. True for a fault that fires only after the commit (the refund can
    # only have gone missing); False for a fault that fires before it, where no refund
    # is honest if the agent did not claim one (a false "done" is then `lost`).
    zero_refunds_after_fault_is_loss: bool
    # The controls `crashtest selftest --scenario NAME` runs: agent -> expected result.
    controls: dict[str, str]
    limitations: tuple[str, ...]
    source: str  # where the scenario comes from
    controller: bool = True
    describe_fault: Callable[[dict[str, Any]], str] = field(default=lambda s: "")
    duplicate_cause: str = ""

    def fault_description(self, settings: dict[str, Any]) -> str:
        return self.describe_fault(settings)

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "fault": self.fault,
            "title": self.title,
            "defaults": dict(self.defaults),
            "limitations": list(self.limitations),
            "source": self.source,
        }


def _lost_response(s: dict[str, Any]) -> str:
    return (
        f"the sink committed the refund, then withheld its response for "
        f"{s['fault_delay_s']:g} s, past the agent's {s['client_timeout_s']:g} s timeout "
        "(the harness does not observe the agent's side of the call)"
    )


def _cut_before_request(s: dict[str, Any]) -> str:
    return (
        "a proxy between the agent and the MCP server withheld the bytes carrying the "
        "agent's first call for the order and closed the connection; the sink held no "
        "request and no refund for the order at the cut (from the proxy's record and the "
        "sink)"
    )


REFUNDS = Scenario(
    name="refunds",
    version="1",
    fault="lost_response",
    title="Refund, lost response after the commit",
    scope=(
        "What was tested: one simulated refund tool (issue_refund) over MCP and one fault, "
        "a lost response: the sink commits the refund, then withholds the reply past the "
        "agent's timeout. No other effect or fault is tested."
    ),
    defaults={"client_timeout_s": 2.0, "fault_delay_s": 5.0, "amount_cents": 2500},
    zero_refunds_after_fault_is_loss=True,
    controls={"naive": "FAIL", "stable_key": "PASS"},
    limitations=(
        "One fault point: after the commit, before the response (contract K2).",
        "The sink enforces idempotency keys in its database; a real provider may not.",
        "Repeatability under the stated delay and timeout, not a rate.",
    ),
    source="experiments 001 to 003 (docs/experiments/001-*.md to 003-*.md)",
    describe_fault=_lost_response,
    duplicate_cause=(
        "The first request committed and its response was lost (the fault held it "
        "past the agent's timeout)"
    ),
)

CUT_BEFORE_REQUEST = Scenario(
    name="refunds-cut-before-request",
    version="1",
    fault="cut_before_request",
    title="Refund, connection cut before the request reaches the server",
    scope=(
        "What was tested: one simulated refund tool (issue_refund) over MCP and one fault, "
        "a connection cut before the request: a proxy between the agent and the MCP "
        "server withholds the agent's first call for the order and closes the "
        "connection, so the server never receives it and nothing is refunded. The error "
        "the agent sees is the same as for a cut after the commit. No other effect or "
        "fault is tested."
    ),
    defaults={"client_timeout_s": 2.0, "amount_cents": 2500},
    zero_refunds_after_fault_is_loss=False,
    controls={"naive": "PASS", "stable_key": "PASS"},
    limitations=(
        "The proxy aborts both sides at once, so the server always learns of the cut; a "
        "silent drop (a connection left open) is not tested.",
        "The proxy cuts the first call that names the order, which is the read-only "
        "check_refund_status if the agent looks the order up first.",
        "Where the cut falls inside the request (headers passed or not) depends on how "
        "the client's bytes arrive and is not controlled; a cut partway through the body "
        "is not a separate case.",
        "Measured in experiment 008 with the reference mcp client and server 2.3.0 on "
        "stateful SSE servers; this scenario uses the default stateless JSON server, "
        "checked by its regression tests, not by a run of record.",
        "A PASS shows the agent's retry did not refund twice here. It does not show the "
        "agent can tell this cut from a cut after the commit: from the error alone it "
        "cannot (008); only a stable idempotency key, or looking the refund up, is safe in "
        "both.",
        "Repeatability under the stated timeout, not a rate.",
    ),
    source="experiment 008 (docs/experiments/008-mcp-cut-points.md), the before_request cut",
    describe_fault=_cut_before_request,
    duplicate_cause=(
        "The cut withheld the first call before it reached the server, so a single retry "
        "would have refunded once; the agent made more than one refund call that reached "
        "the server"
    ),
)

REGISTRY: dict[str, Scenario] = {s.name: s for s in (REFUNDS, CUT_BEFORE_REQUEST)}


def get(name: str) -> Scenario:
    try:
        return REGISTRY[name]
    except KeyError:
        raise ValueError(f"unknown scenario {name!r}; known: {', '.join(REGISTRY)}") from None


def names() -> tuple[str, ...]:
    return tuple(REGISTRY)


def describe(controller_only: bool = False) -> list[dict[str, Any]]:
    return [s.summary() for s in REGISTRY.values() if s.controller or not controller_only]


# --- arming a scenario's fault ----------------------------------------------------


class Faults:
    """Arms a scenario's fault for one order and says whether it fired.

    For `refunds` the sink holds the fault (PUT /control/faults) and records whether
    it fired in its request log. For the cut, a DropProxy in front of the MCP server
    holds it, and its DropRecord says where the cut landed. The agent is given
    `agent_url`, which for the cut is the proxy's.
    """

    def __init__(self, scenario: Scenario, *, mcp_url: str, sink_url: str, token: str | None):
        self.scenario = scenario
        self.mcp_url = mcp_url
        self.sink_url = sink_url
        self.token = token
        self._proxy: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    # The proxy is asyncio code; `crashtest run` is not, so it gets its own loop.
    def _call(self, coro: Coroutine[Any, Any, Any]) -> Any:
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=30)

    def __enter__(self) -> Faults:
        if self.scenario.fault == "cut_before_request":
            from crashtest.faults.drop_proxy import DropProxy

            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
            self._thread.start()
            self._proxy = DropProxy(upstream_url=self.mcp_url, sink_url=self.sink_url)
            self._call(self._proxy.__aenter__())
        return self

    def __exit__(self, *exc: object) -> None:
        if self._proxy is not None and self._loop is not None:
            self._call(self._proxy.__aexit__(None, None, None))
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=10)
            self._loop.close()
            self._proxy = None

    @property
    def agent_url(self) -> str:
        return self._proxy.url if self._proxy is not None else self.mcp_url

    def arm(self, order_id: str, settings: dict[str, Any]) -> None:
        if self._proxy is not None:
            self._proxy.arm(order_id, "before_request")
            return
        with httpx.Client(
            base_url=self.sink_url, timeout=10.0, headers=ct.headers(self.token)
        ) as s:
            s.put(
                "/control/faults",
                json={"order_id": order_id, "delay_s": settings["fault_delay_s"], "times": 1},
            ).raise_for_status()

    def settle_s(self, settings: dict[str, Any]) -> float:
        """How long after the agent started nothing can still be in flight."""
        return settings.get("fault_delay_s", 0.0) + 0.5

    def record(self, order_id: str, truth: dict) -> tuple[bool, dict | None]:
        """(the fault fired as intended, the fault's own record) for one order."""
        if self._proxy is None:
            return any(q.get("fault_applied") for q in truth["requests"]), None
        from dataclasses import asdict

        drops = [d for d in self._proxy.drops if d.order_id == order_id]
        if not drops:
            return False, None
        # The proxy's own count of calls it saw for the order (a second counting point).
        rec = {**asdict(drops[0]), "calls_seen_by_proxy": self._proxy.calls_seen.get(order_id, 0)}
        return drops[0].landed_as_intended(), rec
