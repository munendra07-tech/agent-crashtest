"""Phase 3a: stdio transport, the control token, DNS-rebinding protection, parity.

- `/control/*` refuses calls without the control token; `GET /refunds` stays open.
- `crashtest arm` passes the token (from a stack in this process, the environment
  or the stack file); the agent under test never gets it.
- The locally served MCP server rejects a foreign Host (421) or Origin (403).
- `crashtest mcp --stdio` serves the same tool, errors clearly without a sink, and
  the same agent behaviour gets the same verdict over stdio and over HTTP.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import HTTPException
from mcp import Client, StdioServerParameters

from crashtest import cli, control_token
from crashtest import refund_run as rr
from crashtest.sink.app import require_control_token

AGENT = Path(__file__).parent / "data" / "transport_agent.py"
FAULT = {"delay_s": 0.1, "times": 1}


def _stdio_server(sink_url: str) -> StdioServerParameters:
    import os

    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "crashtest.cli", "mcp", "--stdio", "--sink-url", sink_url],
        env=dict(os.environ),
    )


# --- the control token ------------------------------------------------------------


async def test_control_refuses_calls_without_the_token(stack, order_id):
    async with httpx.AsyncClient(base_url=stack.sink_url, timeout=30.0) as anon:
        faults = await anon.put("/control/faults", json={"order_id": order_id, **FAULT})
        reset = await anon.post("/control/reset")
        wrong = await anon.post("/control/reset", headers=control_token.headers("not-the-token"))
        not_bearer = await anon.put(
            "/control/faults",
            json={"order_id": order_id, **FAULT},
            headers={"Authorization": f"Basic {stack.control_token}"},
        )
        truth = await anon.get("/refunds", params={"order_id": order_id})
    assert (faults.status_code, reset.status_code) == (401, 401)
    assert (wrong.status_code, not_bearer.status_code) == (401, 401)
    assert truth.status_code == 200  # read-only ground truth stays open


async def test_control_accepts_the_token(sink, order_id):
    # The `sink` fixture carries the token, as the harness does.
    r = await sink.put("/control/faults", json={"order_id": order_id, **FAULT})
    assert r.status_code == 200


def test_a_sink_started_without_a_token_refuses_all_control(monkeypatch):
    monkeypatch.delenv(control_token.SINK_ENV, raising=False)
    with pytest.raises(HTTPException) as refused:
        require_control_token("Bearer anything")
    assert refused.value.status_code == 503


def test_token_matching():
    assert control_token.matches("Bearer abc", "abc")
    assert control_token.matches("bearer abc", "abc")
    assert not control_token.matches("Bearer abd", "abc")
    assert not control_token.matches(None, "abc")
    assert not control_token.matches("Bearer abc", None)
    assert not control_token.matches("abc", "abc")


def test_the_agent_environment_never_carries_the_token():
    env = {"PATH": "x", control_token.ENV: "t", control_token.SINK_ENV: "t"}
    assert control_token.agent_env(env) == {"PATH": "x"}


def test_the_agent_under_test_does_not_inherit_the_token(stack, tmp_path, monkeypatch):
    monkeypatch.setenv(control_token.ENV, "visible-if-leaked")
    script = tmp_path / "env_agent.py"
    script.write_text(
        "import json, os\n"
        f"names = ({control_token.ENV!r}, {control_token.SINK_ENV!r})\n"
        "leaked = any(n in os.environ for n in names)\n"
        "print(json.dumps({'report': 'unknown' if leaked else 'not_done'}))\n",
        encoding="utf-8",
    )
    trial = rr.run_trial(
        1,
        agent_cmd=[sys.executable, str(script)],
        mcp_url=stack.mcp_url,
        sink_url=stack.sink_url,
        client_timeout_s=0.05,
        fault_delay_s=0.1,
        amount_cents=2500,
        agent_timeout_s=30.0,
        control_token=stack.control_token,
    )
    assert trial.client_report == "not_done"


def test_arm_takes_the_token_from_the_stack_file(stack, tmp_path, monkeypatch, capsys):
    """A sink URL this process did not start: the token must come from the file or env."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(control_token.ENV, raising=False)
    # The same sink under another name, so the in-process registration does not apply.
    port = urlsplit(stack.sink_url).port
    other_name = f"http://localhost:{port}"
    if control_token.token_for(other_name):
        pytest.skip("this stack's token is in the environment")
    order_id = f"ord-{uuid.uuid4()}"

    assert cli.main(["arm", order_id, "--sink-url", other_name]) == 2
    assert "refused" in capsys.readouterr().err

    Path(cli.STATE_FILE).write_text(
        json.dumps({"sink_url": other_name, "control_token": stack.control_token}),
        encoding="utf-8",
    )
    assert cli.main(["arm", order_id, "--delay", "0.1"]) == 0
    monkeypatch.setenv(control_token.ENV, stack.control_token)
    Path(cli.STATE_FILE).unlink()
    assert cli.main(["arm", order_id, "--delay", "0.1", "--sink-url", other_name]) == 0


# --- DNS-rebinding protection -----------------------------------------------------

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


def test_the_mcp_server_rejects_a_foreign_host_or_origin(stack):
    url = stack.mcp_url
    port = urlsplit(url).port
    with httpx.Client(timeout=10.0) as c:
        bad_host = c.post(url, json=INIT, headers={**MCP_HEADERS, "Host": "evil.example"})
        rebound = c.post(url, json=INIT, headers={**MCP_HEADERS, "Host": f"evil.example:{port}"})
        bad_origin = c.post(
            url, json=INIT, headers={**MCP_HEADERS, "Origin": "http://evil.example"}
        )
        local = c.post(
            url,
            json=INIT,
            headers={
                **MCP_HEADERS,
                "Host": f"localhost:{port}",
                "Origin": f"http://localhost:{port}",
            },
        )
    assert (bad_host.status_code, rebound.status_code) == (421, 421)
    assert bad_origin.status_code == 403
    assert local.status_code == 200


def test_rebinding_protection_settings(monkeypatch):
    from crashtest.refund_mcp import server

    monkeypatch.delenv("MCP_DNS_REBINDING_PROTECTION", raising=False)
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "refund-mcp:*")
    on = server.transport_security()
    assert on.enable_dns_rebinding_protection
    assert on.allowed_hosts == ["127.0.0.1:*", "localhost:*", "[::1]:*", "refund-mcp:*"]
    assert "http://refund-mcp:*" in on.allowed_origins
    monkeypatch.setenv("MCP_DNS_REBINDING_PROTECTION", "false")
    assert not server.transport_security().enable_dns_rebinding_protection


# --- stdio ------------------------------------------------------------------------


async def test_stdio_serves_the_same_tool(stack, sink, order_id):
    async with Client(_stdio_server(stack.sink_url)) as client:
        tools = await client.list_tools()
        result = await client.call_tool(
            "issue_refund", {"order_id": order_id, "amount_cents": 2500}
        )
    assert [t.name for t in tools.tools] == ["issue_refund"]
    assert not result.is_error
    truth = (await sink.get("/refunds", params={"order_id": order_id})).json()
    assert len(truth["refunds"]) == 1


def test_stdio_without_a_sink_errors_clearly(tmp_path):
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "crashtest.cli",
            "mcp",
            "--stdio",
            "--sink-url",
            "http://127.0.0.1:9",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )
    assert proc.returncode == 2
    assert proc.stdout == ""  # stdout belongs to the protocol
    assert "no sink is running" in proc.stderr and "crashtest serve" in proc.stderr


def test_stdio_with_a_missing_stack_file_errors_clearly(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "crashtest.cli", "mcp", "--stdio", "--stack-file", "nope.json"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
    )
    assert proc.returncode == 2
    assert "no stack file" in proc.stderr


def test_stdio_finds_the_sink_in_the_stack_file(stack, tmp_path, monkeypatch):
    monkeypatch.delenv("CRASHTEST_SINK_URL", raising=False)
    state = tmp_path / "stack.json"
    state.write_text(json.dumps({"sink_url": stack.sink_url}), encoding="utf-8")
    assert cli._sink_url(None, state) == stack.sink_url


# --- the same verdict over stdio and HTTP -------------------------------------------


def _run(stack, monkeypatch, mode: str, transport: str) -> rr.RunReport:
    monkeypatch.setenv("CRASHTEST_TEST_AGENT_MODE", mode)
    monkeypatch.setenv("CRASHTEST_TEST_AGENT_TRANSPORT", transport)
    monkeypatch.setenv("CRASHTEST_TEST_AGENT_SINK_URL", stack.sink_url)
    return rr.run_refunds(
        agent=f"{mode}-{transport}",
        agent_cmd=[sys.executable, str(AGENT)],
        mcp_url=stack.mcp_url,
        sink_url=stack.sink_url,
        trials=2,
        control_token=stack.control_token,
        progress=False,
    )


@pytest.mark.parametrize(
    ("mode", "result", "verdict"),
    [("naive", "FAIL", "duplicate"), ("stable_key", "PASS", "correct")],
)
def test_same_verdict_over_stdio_and_http(stack, monkeypatch, mode, result, verdict):
    runs = {t: _run(stack, monkeypatch, mode, t) for t in ("http", "stdio")}
    for transport, run in runs.items():
        assert run.result == result, (transport, run.summary)
        assert [t.verdict for t in run.trials] == [verdict, verdict], transport
        assert all(t.fault_fired for t in run.trials), transport
    facts = {
        t: (r.result, r.exit_code, r.observed()["verdicts"], r.observed()["actual_refunds"])
        for t, r in runs.items()
    }
    assert facts["http"] == facts["stdio"]
