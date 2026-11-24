"""The test controller: a separate MCP server for the developer, never for the agent.

- A run driven through the controller gives the same verdict and report facts as
  `crashtest run` for the same agent behaviour (the two example agents).
- The controller has exactly its five tools: none resets or deletes.
- The agent-facing MCP server exposes no control tool, over HTTP or stdio.
- Over Streamable HTTP the controller requires the control token and checks Host.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from crashtest import control_token
from crashtest import refund_run as rr
from crashtest.controller import CONTROLLER_TOOLS, Controller, ControllerError, build_server

CONTROL_WORDS = ("arm", "fault", "reset", "delete", "verdict", "report", "configure", "run")


def _data(result) -> dict:
    assert not result.is_error, result.content
    return result.structured_content or json.loads(result.content[0].text)


def _agent(stack, name: str, order_id: str, timeout_s: float, amount: int) -> str:
    proc = subprocess.run(
        [
            sys.executable,
            str(rr.find_builtin_agent(name)),
            "--mcp-url",
            stack.mcp_url,
            "--order-id",
            order_id,
            "--amount-cents",
            str(amount),
            "--timeout",
            f"{timeout_s:g}",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=control_token.agent_env(),
    )
    return rr._client_report(proc.stdout)


def _facts(run: dict) -> dict:
    """What must agree between `crashtest run` and the controller (not IDs or timings)."""
    observed = {k: v for k, v in run["observed"].items() if k != "per_trial"}
    per_trial = [
        {
            "refunds": t["refunds"],
            "requests": t["requests"],
            "fault_fired": t["fault_fired"],
            "client_report": t["client_report"],
            "report_matches_sink": t["report_matches_sink"],
            "verdict": t["verdict"],
            "payload_ok": t["payload_ok"],
            "keyed": [k is not None for k in t["idempotency_keys"]],
            "likely_cause": t["likely_cause"],
            "recommended_fix": t["recommended_fix"],
        }
        for t in run["observed"]["per_trial"]
    ]
    return {
        "result": run["result"],
        "exit_code": run["exit_code"],
        "summary": run["summary"],
        "observed": observed,
        "per_trial": per_trial,
        "likely_cause": run["likely_cause"],
        "recommended_fix": run["recommended_fix"],
        "scenario": run["scenario"],
        "scenario_version": run["scenario_version"],
    }


@pytest.mark.parametrize(("agent", "expected"), [("naive", "FAIL"), ("stable_key", "PASS")])
async def test_controller_run_matches_crashtest_run(stack, agent, expected):
    trials, timeout_s, delay_s, amount = 2, 2.0, 5.0, 2500
    by_run = rr.run_refunds(
        agent=agent,
        agent_cmd=[sys.executable, str(rr.find_builtin_agent(agent))],
        mcp_url=stack.mcp_url,
        sink_url=stack.sink_url,
        trials=trials,
        client_timeout_s=timeout_s,
        fault_delay_s=delay_s,
        amount_cents=amount,
        control_token=stack.control_token,
        progress=False,
    )

    controller = Controller(
        sink_url=stack.sink_url, mcp_url=stack.mcp_url, token=stack.control_token
    )
    async with Client(build_server(controller)) as dev:
        created = _data(await dev.call_tool("create_test_run", {"trials": trials, "agent": agent}))
        run_id = created["run_id"]
        assert created["agent_mcp_url"] == stack.mcp_url
        _data(
            await dev.call_tool(
                "configure_scenario",
                {
                    "run_id": run_id,
                    "client_timeout_s": timeout_s,
                    "fault_delay_s": delay_s,
                    "amount_cents": amount,
                },
            )
        )
        said = {}
        for t in created["trials"]:
            _data(await dev.call_tool("arm_fault", {"run_id": run_id, "trial": t["trial"]}))
            said[str(t["trial"])] = _agent(stack, agent, t["order_id"], timeout_s, amount)
        status = _data(await dev.call_tool("get_run_status", {"run_id": run_id}))
        assert all(t["armed"] and t["fault_fired"] for t in status["trials"])
        out = _data(
            await dev.call_tool(
                "get_test_report", {"run_id": run_id, "agent_reports": said, "format": "json"}
            )
        )
        text = _data(
            await dev.call_tool(
                "get_test_report", {"run_id": run_id, "agent_reports": said, "format": "text"}
            )
        )["report"]

    report = json.loads(out["report"])
    assert out["result"] == by_run.result == expected
    assert (report["run_id"], report["driver"]) == (run_id, "controller")
    assert report["scenario"] == out["scenario"] and report["scenario_version"]
    assert [t["order_id"] for t in report["observed"]["per_trial"]] == [
        t["order_id"] for t in created["trials"]
    ]
    assert _facts(report) == _facts(by_run.to_dict())
    assert run_id in text and "via the test controller" in text


async def test_controller_has_only_its_five_tools():
    controller = Controller(sink_url="http://127.0.0.1:9", mcp_url="m", token="t")
    async with Client(build_server(controller)) as dev:
        names = {t.name for t in (await dev.list_tools()).tools}
    assert names == set(CONTROLLER_TOOLS)
    assert not any(w in n for n in names for w in ("reset", "delete", "drop", "truncate"))


async def test_the_agent_facing_server_exposes_no_control_tool(stack):
    async with Client(stack.mcp_url) as client:
        http_names = {t.name for t in (await client.list_tools()).tools}
    stdio = StdioServerParameters(
        command=sys.executable,
        args=["-m", "crashtest.cli", "mcp", "--stdio", "--sink-url", stack.sink_url],
        env=dict(os.environ),
    )
    async with Client(stdio) as client:
        stdio_names = {t.name for t in (await client.list_tools()).tools}
    assert http_names == stdio_names
    assert "issue_refund" in http_names
    assert not http_names & set(CONTROLLER_TOOLS)
    # Simulated tools only: nothing that arms, resets or judges.
    assert not [n for n in http_names - {"issue_refund"} if any(w in n for w in CONTROL_WORDS)]


async def test_controller_refusals(stack):
    with pytest.raises(ControllerError, match="control token"):
        Controller(sink_url=stack.sink_url, mcp_url=stack.mcp_url, token=None)
    c = Controller(sink_url=stack.sink_url, mcp_url=stack.mcp_url, token=stack.control_token)
    run_id = c.create_run(trials=1)["run_id"]
    with pytest.raises(ControllerError, match="shorter"):
        c.configure(run_id, client_timeout_s=5.0, fault_delay_s=2.0)
    with pytest.raises(ControllerError, match="arm the fault"):
        await c.report(run_id)
    with pytest.raises(ControllerError, match="trial must be"):
        await c.arm(run_id, 2)
    await c.arm(run_id, 1)
    with pytest.raises(ControllerError, match="already armed"):
        await c.arm(run_id, 1)
    with pytest.raises(ControllerError, match="before arming"):
        c.configure(run_id, fault_delay_s=6.0)
    with pytest.raises(ControllerError, match="must be one of"):
        await c.report(run_id, {"1": "refunded"})
    with pytest.raises(ControllerError, match="no run"):
        await c.status("run-nope")
    with pytest.raises(ControllerError, match="scenario"):
        c.create_run(scenario="nope")


async def test_a_controller_with_a_wrong_token_cannot_arm(stack):
    c = Controller(sink_url=stack.sink_url, mcp_url=stack.mcp_url, token="wrong")
    run_id = c.create_run()["run_id"]
    with pytest.raises(httpx.HTTPStatusError) as refused:
        await c.arm(run_id, 1)
    assert refused.value.response.status_code == 401


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_controller(stack, tmp_path):
    state = tmp_path / ".crashtest-stack.json"
    state.write_text(
        json.dumps(
            {
                "sink_url": stack.sink_url,
                "mcp_url": stack.mcp_url,
                "control_token": stack.control_token,
            }
        ),
        encoding="utf-8",
    )
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "crashtest.cli", "controller", "--http", "--port", str(port),
         "--stack-file", str(state)],
        env=control_token.agent_env(),
        stderr=subprocess.PIPE,
        text=True,
    )  # fmt: skip
    url = f"http://127.0.0.1:{port}/mcp"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            httpx.get(url, timeout=1.0)
            break
        except httpx.HTTPError:
            time.sleep(0.2)
    yield url
    proc.terminate()
    proc.wait(timeout=10)


async def test_http_controller_requires_the_token(stack, http_controller):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    accept = {"Accept": "application/json, text/event-stream"}
    async with httpx.AsyncClient(timeout=10.0) as c:
        anonymous = await c.post(http_controller, json=body, headers=accept)
        wrong = await c.post(
            http_controller, json=body, headers={**accept, **control_token.headers("nope")}
        )
        rebound = await c.post(
            http_controller,
            json=body,
            headers={
                **accept,
                **control_token.headers(stack.control_token),
                "Host": "evil.example",
            },
        )
    assert (anonymous.status_code, wrong.status_code) == (401, 401)
    assert rebound.status_code in (401, 421)  # refused either way
    authed = create_mcp_http_client(headers=control_token.headers(stack.control_token))
    async with Client(streamable_http_client(http_controller, http_client=authed)) as dev:
        names = {t.name for t in (await dev.list_tools()).tools}
        created = _data(await dev.call_tool("create_test_run", {"trials": 1}))
        status = _data(await dev.call_tool("get_run_status", {"run_id": created["run_id"]}))
    assert names == set(CONTROLLER_TOOLS)
    assert status["trials"][0]["refunds_in_sink"] == 0


def test_controller_without_a_stack_errors_clearly(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "crashtest.cli", "controller", "--stdio",
         "--stack-file", str(tmp_path / "missing.json")],
        capture_output=True, text=True, timeout=60, cwd=tmp_path,
        env=control_token.agent_env(),
    )  # fmt: skip
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "stack file" in proc.stderr


def test_controller_tools_are_documented():
    doc = (Path(__file__).resolve().parents[1] / "docs" / "controller.md").read_text("utf-8")
    for name in CONTROLLER_TOOLS:
        assert f"`{name}`" in doc
