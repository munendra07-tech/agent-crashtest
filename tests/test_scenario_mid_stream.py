"""The `refunds-cut-mid-stream` scenario (experiment 008's mid_stream cut).

The server (stateful, SSE responses, no event store) sends the tool's first progress
notification, then pauses before it calls the sink; a proxy lets the notification
through and cuts before the response. The server cancels the tool, so the cut call
refunds nothing. Ground truth: the sink's rows plus the proxy's record.
"""

from __future__ import annotations

import json
import sys
import textwrap

import pytest

from crashtest import cli, scenarios
from crashtest import refund_run as rr
from crashtest.controller import Controller, ControllerError

MID = "refunds-cut-mid-stream"


def _agent(name: str) -> list[str]:
    return [sys.executable, str(rr.find_builtin_agent(name))]


def _script(tmp_path, body: str) -> list[str]:
    path = tmp_path / "agent.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(path)]


# One attempt, no retry; asks for progress or not; prints `after_error` on a failure.
ONE_ATTEMPT = """
    import argparse, asyncio, json
    from mcp import Client
    p = argparse.ArgumentParser()
    for a in ("--mcp-url", "--order-id", "--amount-cents", "--timeout"):
        p.add_argument(a)
    args = p.parse_args()
    async def on_progress(progress, total, message):
        pass
    async def go():
        try:
            async with Client(args.mcp_url) as c:
                r = await c.call_tool(
                    "issue_refund",
                    {{"order_id": args.order_id, "amount_cents": int(args.amount_cents)}},
                    read_timeout_seconds=float(args.timeout),
                    {progress}
                )
            return "not_done" if r.is_error else "done"
        except (Exception, BaseExceptionGroup):
            return "{after_error}"
    print(json.dumps({{"report": asyncio.run(go())}}))
"""


def _one_attempt(tmp_path, *, progress: bool, after_error: str) -> list[str]:
    arg = "progress_callback=on_progress," if progress else ""
    return _script(tmp_path, ONE_ATTEMPT.format(progress=arg, after_error=after_error))


def _mid_url(stack) -> str:
    url = stack.mcp_urls.get(scenarios.MID_STREAM_TRANSPORT)
    if not url:  # the Docker stack serves only the default transport
        pytest.skip(f"this stack does not serve the {scenarios.MID_STREAM_TRANSPORT} transport")
    return url


def _run(stack, agent_cmd, agent="a", trials=2) -> rr.RunReport:
    return rr.run_refunds(
        agent=agent,
        agent_cmd=agent_cmd,
        mcp_url=_mid_url(stack),
        sink_url=stack.sink_url,
        trials=trials,
        client_timeout_s=scenarios.CUT_MID_STREAM.defaults["client_timeout_s"],
        control_token=stack.control_token,
        progress=False,
        scenario=MID,
    )


def test_the_scenario_is_registered():
    s = scenarios.get(MID)
    assert (s.fault, s.drop_point, s.transport) == ("cut_mid_stream", "mid_stream", MID_T)
    assert s.zero_refunds_after_fault_is_loss is False
    assert s.controls == {"naive": "PASS", "stable_key": "PASS"}
    assert s.defaults["client_timeout_s"] > scenarios.MID_STREAM_PAUSE_S
    assert s.min_settle_s > scenarios.MID_STREAM_PAUSE_S
    assert s.controller is True


MID_T = scenarios.MID_STREAM_TRANSPORT


@pytest.mark.parametrize("agent", ["naive", "stable_key"])
def test_both_example_agents_refund_once(stack, agent):
    run = _run(stack, _agent(agent), agent=agent)
    assert (run.result, run.exit_code) == ("PASS", 0), run.summary
    for t in run.trials:
        assert (t.refunds, t.verdict, t.fault_fired) == (1, "correct", True)
        rec = t.fault_record
        assert rec is not None and rec["drop_point"] == "mid_stream"
        assert rec["notifications_forwarded"] >= 1
        assert rec["sink_refunds_at_cut"] == 0
        # The proxy saw the cut call and the retry; only the retry refunded.
        assert rec["calls_seen_by_proxy"] == 2
    text = rr.format_text(run)
    assert "connection cut mid-stream" in text and "lost response" not in text


def test_a_client_that_asks_for_no_progress_tests_nothing(stack, tmp_path):
    run = _run(stack, _one_attempt(tmp_path, progress=False, after_error="unknown"), trials=1)
    assert (run.result, run.exit_code) == ("INCONCLUSIVE", 3), run.summary
    t = run.trials[0]
    assert t.fault_fired is False
    assert t.recommended_fix == rr.FIX_ASK_FOR_PROGRESS


def test_reporting_done_after_the_cut_is_a_lost_refund(stack, tmp_path):
    run = _run(stack, _one_attempt(tmp_path, progress=True, after_error="done"), trials=1)
    t = run.trials[0]
    assert (t.refunds, t.fault_fired, t.verdict) == (0, True, "lost")
    assert run.result == "FAIL"


def test_crashtest_run_and_selftest(monkeypatch, stack, capsys):
    _mid_url(stack)
    monkeypatch.setattr(cli, "_start_stack", lambda data_dir, cleanup, *transports: stack)
    code = cli.main(["run", MID, "--agent", "naive", "--trials", "1", "--format", "json"])
    report = json.loads(capsys.readouterr().out)
    assert (code, report["result"], report["scenario"]) == (0, "PASS", MID)
    assert report["settings"]["client_timeout_s"] == 10.0
    assert cli.main(["selftest", "--scenario", MID, "--trials", "1"]) == 0
    assert "selftest passed (refunds-cut-mid-stream)" in capsys.readouterr().out


def test_a_controller_without_the_mid_stream_server_says_what_to_do(stack):
    # An older `crashtest serve` wrote no mcp_urls in its stack file.
    controller = Controller(
        sink_url=stack.sink_url, mcp_url=stack.mcp_url, token=stack.control_token
    )
    with pytest.raises(ControllerError, match="restart it with this version"):
        controller.create_run(scenario=MID)


async def test_the_controller_runs_the_mid_stream_cut(stack):
    import asyncio
    import subprocess

    from mcp import Client

    from crashtest import control_token
    from crashtest.controller import build_server

    controller = Controller(
        sink_url=stack.sink_url,
        mcp_url=stack.mcp_url,
        token=stack.control_token,
        mcp_urls={MID_T: _mid_url(stack)},
    )

    def agent(url: str, order_id: str) -> str:
        done = subprocess.run(
            [*_agent("naive"), "--mcp-url", url, "--order-id", order_id, "--amount-cents",
             "2500", "--timeout", "10"],
            capture_output=True, text=True, timeout=120, env=control_token.agent_env(),
        )  # fmt: skip
        return rr._client_report(done.stdout)

    try:
        async with Client(build_server(controller)) as dev:
            created = (await dev.call_tool("create_test_run", {"scenario": MID})).structured_content
            assert MID in {s["name"] for s in created["scenarios"]}
            assert created["agent_mcp_url"] != _mid_url(stack)  # the proxy's
            run_id, order_id = created["run_id"], created["trials"][0]["order_id"]
            await dev.call_tool("arm_fault", {"run_id": run_id, "trial": 1})
            said = await asyncio.to_thread(agent, created["agent_mcp_url"], order_id)
            out = (
                await dev.call_tool(
                    "get_test_report", {"run_id": run_id, "agent_reports": {"1": said}}
                )
            ).structured_content
    finally:
        controller.close()
    assert (out["result"], out["scenario"]) == ("PASS", MID)
    [t] = json.loads(out["report"])["observed"]["per_trial"]
    assert (t["verdict"], t["fault_fired"], t["refunds"]) == ("correct", True, 1)
    assert t["fault_record"]["notifications_forwarded"] >= 1
