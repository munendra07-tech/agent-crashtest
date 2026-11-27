"""`crashtest controller`: a separate MCP server for the developer, never for the agent.

The agent under test connects to the refund MCP server and sees only simulated
tools. This server is the other side: the developer's own MCP client (the MCP
Inspector, an IDE, a script) uses it to drive a test of that agent:

  create_test_run     a run of N trials, each with a fresh order ID
  configure_scenario  the scenario's settings, before any fault is armed
  arm_fault           arm the scenario's fault for one trial's order
  get_run_status      per trial: armed, and what the sink holds now
  get_test_report     the report, from the sink through the project's checker

There is no reset or delete tool. The verdict comes only from the sink's rows,
judged by `crashtest.cli.judge` (the checker) and summarised by the same report
code as `crashtest run`; what the agent told its caller is supplied by the
developer, recorded as "agent-reported (not verified)", and never changes a
duplicate into anything else.

Token: the controller needs the sink's control token (from `.crashtest-stack.json`
or CRASHTEST_CONTROL_TOKEN) to arm faults. Over Streamable HTTP it also requires
that token from its own clients (`Authorization: Bearer <token>`), listens on
127.0.0.1 only, and checks Host and Origin. Never configure this server into the
agent under test: with it, the agent could arm faults and read the ground truth.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

from crashtest import control_token as ct
from crashtest import refund_run as rr
from crashtest import scenarios

CONTROLLER_TOOLS = (
    "create_test_run",
    "configure_scenario",
    "arm_fault",
    "get_run_status",
    "get_test_report",
)
CLIENT_REPORTS = (*rr.REPORTS, "none")


SCENARIOS = scenarios.REGISTRY
# The sink releases a held response this long after its delay; then nothing is in flight.
SETTLE_S = 0.5


class ControllerError(ValueError):
    """A request the controller refuses (unknown run, bad setting, wrong order of calls)."""


@dataclass
class ControllerRun:
    run_id: str
    scenario: scenarios.Scenario
    orders: list[str]
    settings: dict[str, Any]
    agent: str
    created_at: str
    armed: dict[int, float] = field(default_factory=dict)  # trial -> time.time() when armed


class Controller:
    """The controller's logic, kept apart from MCP so it can be tested directly."""

    def __init__(self, *, sink_url: str, mcp_url: str, token: str | None) -> None:
        if not token:
            raise ControllerError(
                "the controller needs the sink's control token: run it in the folder where "
                f"`crashtest serve` wrote .crashtest-stack.json, or set {ct.ENV}"
            )
        self.sink_url = sink_url.rstrip("/")
        self.mcp_url = mcp_url
        self.token = token
        self.runs: dict[str, ControllerRun] = {}
        # A scenario whose fault is not held by the sink (the cut: a proxy in front of
        # the MCP server) is started on first use and kept for the controller's life.
        self._faults: dict[str, scenarios.Faults] = {}

    def faults(self, scenario: scenarios.Scenario) -> scenarios.Faults:
        if scenario.name not in self._faults:
            self._faults[scenario.name] = scenarios.Faults(
                scenario, mcp_url=self.mcp_url, sink_url=self.sink_url, token=self.token
            ).__enter__()
        return self._faults[scenario.name]

    def close(self) -> None:
        for faults in self._faults.values():
            faults.__exit__(None, None, None)
        self._faults.clear()

    def agent_url(self, run: ControllerRun) -> str:
        """The URL the agent under test must use for this run (the proxy's, for the cut)."""
        return self.faults(run.scenario).agent_url

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.sink_url, timeout=10.0, headers=ct.headers(self.token)
        )

    def _run(self, run_id: str) -> ControllerRun:
        try:
            return self.runs[run_id]
        except KeyError:
            raise ControllerError(f"no run {run_id!r} in this controller") from None

    def _trial(self, run: ControllerRun, trial: int) -> str:
        if not 1 <= trial <= len(run.orders):
            raise ControllerError(f"trial must be 1 to {len(run.orders)}, got {trial}")
        return run.orders[trial - 1]

    def create_run(
        self, *, scenario: str = scenarios.DEFAULT, trials: int = 1, agent: str = "manual"
    ) -> dict[str, Any]:
        found = SCENARIOS.get(scenario)
        known = [name for name, s in SCENARIOS.items() if s.controller]
        if found is None:
            raise ControllerError(f"unknown scenario {scenario!r}; known: {', '.join(known)}")
        if not found.controller:
            raise ControllerError(
                f"the controller does not run {scenario!r} yet (its fault needs an MCP server "
                f"`crashtest serve` does not start); use `crashtest run {scenario}`. "
                f"The controller runs: {', '.join(known)}"
            )
        if not 1 <= trials <= 100:
            raise ControllerError("trials must be 1 to 100")
        run = ControllerRun(
            run_id=f"run-{uuid.uuid4()}",
            scenario=found,
            orders=[f"ord-{uuid.uuid4()}" for _ in range(trials)],
            settings=dict(found.defaults),
            agent=agent,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        self.runs[run.run_id] = run
        return self._describe(run)

    def configure(self, run_id: str, **settings: float | int | None) -> dict[str, Any]:
        run = self._run(run_id)
        if run.armed:
            raise ControllerError("configure the scenario before arming any fault")
        for name, value in settings.items():
            if value is None:
                continue
            if name not in run.settings:
                raise ControllerError(f"unknown setting {name!r}")
            run.settings[name] = type(run.settings[name])(value)
        if (
            "fault_delay_s" in run.settings
            and run.settings["client_timeout_s"] >= run.settings["fault_delay_s"]
        ):
            raise ControllerError(
                "client_timeout_s must be shorter than fault_delay_s, or no response is lost"
            )
        if run.settings["amount_cents"] <= 0:
            raise ControllerError("amount_cents must be positive")
        return self._describe(run)

    async def arm(self, run_id: str, trial: int) -> dict[str, Any]:
        run = self._run(run_id)
        order_id = self._trial(run, trial)
        if trial in run.armed:
            raise ControllerError(f"the fault for trial {trial} is already armed")
        if run.scenario.fault == "lost_response":
            async with self._client() as sink:
                r = await sink.put(
                    "/control/faults",
                    json={
                        "order_id": order_id,
                        "delay_s": run.settings["fault_delay_s"],
                        "times": 1,
                    },
                )
                r.raise_for_status()
        else:
            self.faults(run.scenario).arm(order_id, run.settings)
        run.armed[trial] = time.time()
        return {
            "run_id": run.run_id,
            "trial": trial,
            "order_id": order_id,
            "armed": run.scenario.fault_description(run.settings),
            "next": (
                f"have the agent call issue_refund for order {order_id} with amount_cents "
                f"{run.settings['amount_cents']} and a timeout of "
                f"{run.settings['client_timeout_s']:g} s, on {self.agent_url(run)}"
            ),
        }

    async def _truth(self, sink: httpx.AsyncClient, order_id: str) -> dict:
        r = await sink.get("/refunds", params={"order_id": order_id})
        r.raise_for_status()
        return r.json()

    async def status(self, run_id: str) -> dict[str, Any]:
        run = self._run(run_id)
        trials = []
        async with self._client() as sink:
            for n, order_id in enumerate(run.orders, start=1):
                truth = await self._truth(sink, order_id)
                trials.append(
                    {
                        "trial": n,
                        "order_id": order_id,
                        "armed": n in run.armed,
                        "refunds_in_sink": len(truth["refunds"]),
                        "requests_in_sink": len(truth["requests"]),
                        "fault_fired": self.faults(run.scenario).record(order_id, truth)[0],
                    }
                )
        return {**self._describe(run), "trials": trials}

    async def report(
        self, run_id: str, agent_reports: dict[str, str] | None = None, fmt: str = "json"
    ) -> dict[str, Any]:
        run = self._run(run_id)
        if fmt not in rr.FORMATS:
            raise ControllerError(f"format must be one of {', '.join(rr.FORMATS)}")
        said = {str(k): v for k, v in (agent_reports or {}).items()}
        for key, value in said.items():
            if value not in CLIENT_REPORTS:
                raise ControllerError(
                    f"agent report for trial {key} must be one of {CLIENT_REPORTS}"
                )
        if unarmed := [n for n in range(1, len(run.orders) + 1) if n not in run.armed]:
            raise ControllerError(f"arm the fault for trial(s) {rr._list(unarmed)} first")
        delay = run.settings.get("fault_delay_s", 0.0)
        trials = []
        async with self._client() as sink:
            for n, order_id in enumerate(run.orders, start=1):
                truth = await self._truth(sink, order_id)
                # A held response is released `delay` after its request reached the
                # sink: wait until nothing can still be in flight, then read again.
                fired = [q for q in truth["requests"] if q.get("fault_applied")]
                if fired:
                    received = datetime.fromisoformat(str(fired[0]["received_at"]))
                    wait = received.timestamp() + delay + SETTLE_S - time.time()
                    if wait > 0:
                        await asyncio.sleep(wait)
                        truth = await self._truth(sink, order_id)
                fired_ok, record = self.faults(run.scenario).record(order_id, truth)
                trials.append(
                    rr.trial_from_truth(
                        n,
                        order_id=order_id,
                        truth=truth,
                        report=said.get(str(n), "none"),
                        amount_cents=run.settings["amount_cents"],
                        agent_exit_code=None,
                        agent_note="",
                        elapsed_s=0.0,
                        fault_fired=fired_ok,
                        fault_record=record,
                        scenario=run.scenario.name,
                    )
                )
        result = rr.summarise(
            rr.RunReport(
                agent=run.agent,
                trials=trials,
                client_timeout_s=run.settings["client_timeout_s"],
                fault_delay_s=delay,
                amount_cents=run.settings["amount_cents"],
                run_id=run.run_id,
                scenario=run.scenario.name,
                scenario_version=run.scenario.version,
                driver="controller",
            )
        )
        return {
            "run_id": run.run_id,
            "scenario": run.scenario.name,
            "scenario_version": run.scenario.version,
            "result": result.result,
            "exit_code": result.exit_code,
            "summary": result.summary,
            "verdict_source": (
                "the sink's rows, judged by crashtest.checker; agent reports are the "
                "developer's statement of what the agent said, not verified"
            ),
            "format": fmt,
            "report": rr.FORMATS[fmt](result),
        }

    def _describe(self, run: ControllerRun) -> dict[str, Any]:
        return {
            "run_id": run.run_id,
            "scenario": run.scenario.name,
            "scenario_version": run.scenario.version,
            "created_at": run.created_at,
            "agent": run.agent,
            "settings": dict(run.settings),
            "agent_mcp_url": self.agent_url(run),
            "trials": [
                {"trial": n, "order_id": o, "armed": n in run.armed}
                for n, o in enumerate(run.orders, start=1)
            ],
        }


def build_server(controller: Controller) -> MCPServer:
    """The controller's MCP server: the five tools, and nothing that resets or deletes."""
    server = MCPServer("crashtest-controller")

    @server.tool()
    def create_test_run(
        scenario: str = scenarios.DEFAULT, trials: int = 1, agent: str = "manual"
    ) -> dict[str, Any]:
        """Create a test run: one fresh order ID per trial for the agent under test.

        Args:
            scenario: The scenario to run (see `scenarios` in the result); default refunds.
            trials: How many orders (trials), 1 to 100.
            agent: A name for the agent under test, used in the report.
        """
        out = controller.create_run(scenario=scenario, trials=trials, agent=agent)
        return {
            **out,
            "scenarios": scenarios.describe(controller_only=True),
        }

    @server.tool()
    def configure_scenario(
        run_id: str,
        client_timeout_s: float | None = None,
        fault_delay_s: float | None = None,
        amount_cents: int | None = None,
    ) -> dict[str, Any]:
        """Change a run's settings before any fault is armed.

        Args:
            run_id: The run from create_test_run.
            client_timeout_s: The timeout the agent should use, in seconds.
            fault_delay_s: How long the sink holds the response; longer than the timeout.
            amount_cents: The refund amount the agent should send.
        """
        return controller.configure(
            run_id,
            client_timeout_s=client_timeout_s,
            fault_delay_s=fault_delay_s,
            amount_cents=amount_cents,
        )

    @server.tool()
    async def arm_fault(run_id: str, trial: int = 1) -> dict[str, Any]:
        """Arm the scenario's fault for one trial's order, then run the agent on it.

        Args:
            run_id: The run from create_test_run.
            trial: Which trial (1-based).
        """
        return await controller.arm(run_id, trial)

    @server.tool()
    async def get_run_status(run_id: str) -> dict[str, Any]:
        """What the sink holds now for each trial of a run (read-only).

        Args:
            run_id: The run from create_test_run.
        """
        return await controller.status(run_id)

    @server.tool()
    async def get_test_report(
        run_id: str, agent_reports: dict[str, str] | None = None, format: str = "json"
    ) -> dict[str, Any]:
        """The run's report: verdicts from the sink's rows through the project's checker.

        Args:
            run_id: The run from create_test_run.
            agent_reports: What the agent told its caller, per trial number ("1": "done");
                done, unknown, not_done or none (the default). Recorded, not trusted.
            format: json, text, markdown or junit.
        """
        return await controller.report(run_id, agent_reports, format)

    return server
