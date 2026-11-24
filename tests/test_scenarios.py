"""The scenario interface and the first research-derived scenario (experiment 008).

- The reference scenario, `refunds`, is one registered scenario; its behaviour and
  verdicts are unchanged (the existing tests and tests/test_verdict_properties.py).
- `refunds-cut-before-request`: a proxy cuts the agent's first call before it reaches
  the server. Ground truth: the sink's rows plus the proxy's record that the call was
  withheld. Both controls from 008: a naive retry refunds once; the error the client
  sees is the same as for a cut after the commit, where the naive retry refunds twice.
"""

from __future__ import annotations

import json
import sys
import textwrap

import pytest
from mcp import Client

from crashtest import cli, scenarios
from crashtest import refund_run as rr
from crashtest.controller import Controller, build_server
from crashtest.experiment.trial import run_trial as experiment_trial
from crashtest.faults.drop_proxy import DropProxy

CUT = "refunds-cut-before-request"


def _agent(name: str) -> list[str]:
    return [sys.executable, str(rr.find_builtin_agent(name))]


def _script(tmp_path, body: str) -> list[str]:
    path = tmp_path / "agent.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(path)]


# One attempt, no retry; prints what it is told to print after a failure.
ONE_ATTEMPT = """
    import argparse, asyncio, json
    from mcp import Client
    p = argparse.ArgumentParser()
    for a in ("--mcp-url", "--order-id", "--amount-cents", "--timeout"):
        p.add_argument(a)
    args = p.parse_args()
    async def go():
        try:
            async with Client(args.mcp_url) as c:
                r = await c.call_tool(
                    "issue_refund",
                    {{"order_id": args.order_id, "amount_cents": int(args.amount_cents)}},
                    read_timeout_seconds=float(args.timeout),
                )
            return "not_done" if r.is_error else "done"
        except (Exception, BaseExceptionGroup):
            return "{after_error}"
    print(json.dumps({{"report": asyncio.run(go())}}))
"""


def _run(stack, agent_cmd, agent="a", trials=2, scenario=CUT) -> rr.RunReport:
    return rr.run_refunds(
        agent=agent,
        agent_cmd=agent_cmd,
        mcp_url=stack.mcp_url,
        sink_url=stack.sink_url,
        trials=trials,
        control_token=stack.control_token,
        progress=False,
        scenario=scenario,
    )


# --- the registry ------------------------------------------------------------------


def test_the_registry():
    assert scenarios.names() == ("refunds", CUT)
    for s in scenarios.REGISTRY.values():
        assert s.version and s.fault and s.limitations and s.source and s.scope
        assert set(s.controls) == {"naive", "stable_key"}
    ref = scenarios.get("refunds")
    assert (ref.fault, ref.zero_refunds_after_fault_is_loss) == ("lost_response", True)
    assert ref.scope == rr.SCOPE
    assert ref.defaults == {"client_timeout_s": 2.0, "fault_delay_s": 5.0, "amount_cents": 2500}
    cut = scenarios.get(CUT)
    assert (cut.fault, cut.zero_refunds_after_fault_is_loss) == ("cut_before_request", False)
    assert "fault_delay_s" not in cut.defaults
    with pytest.raises(ValueError, match="unknown scenario"):
        scenarios.get("nope")


def test_the_cli_offers_every_scenario():
    parser = cli.build_parser()
    for name in scenarios.names():
        args = parser.parse_args(["run", name, "--agent", "naive"])
        assert args.scenario == name
        assert parser.parse_args(["selftest", "--scenario", name]).scenario == name


# --- control 1: a naive retry refunds once -------------------------------------------


@pytest.mark.parametrize("agent", ["naive", "stable_key"])
def test_cut_before_request_both_example_agents_refund_once(stack, agent):
    run = _run(stack, _agent(agent), agent=agent)
    assert (run.result, run.exit_code) == ("PASS", 0), run.summary
    for t in run.trials:
        assert (t.refunds, t.verdict, t.fault_fired) == (1, "correct", True)
        rec = t.fault_record
        assert rec is not None and rec["request_withheld"] is True
        assert (rec["sink_requests_at_cut"], rec["sink_refunds_at_cut"]) == (0, 0)
        # The proxy saw two calls; only the retry reached the sink.
        assert rec["calls_seen_by_proxy"] == 2
        assert t.requests == 1
    report = run.to_dict()
    assert (report["scenario"], report["fault"]) == (CUT, "cut_before_request")
    assert "fault_delay_s" not in report["settings"]
    assert report["observed"]["per_trial"][0]["fault_record"]["drop_point"] == "before_request"
    text = rr.format_text(run)
    assert "connection cut before the request" in text and "lost response" not in text


# --- control 2: the error matches a lost response's (a cut after the commit) --------


async def test_cut_before_request_raises_the_same_error_as_a_cut_after_the_commit(stack):
    results = {}
    for point in ("before_request", "no_bytes"):
        async with DropProxy(upstream_url=stack.mcp_url, sink_url=stack.sink_url) as proxy:
            results[point] = await experiment_trial(
                arm="no_dedup",
                fault="connection_drop",
                mcp_url=stack.mcp_url,
                sink_url=stack.sink_url,
                drop_proxy=proxy,
                drop_point=point,
                client_timeout_s=10.0,
            )
            [drop] = proxy.drops
            assert drop.landed_as_intended()
    before, after = results["before_request"], results["no_bytes"]
    first = before.attempts[0], after.attempts[0]
    # What the client sees: the same outcome and the same exception.
    assert first[0].outcome == first[1].outcome == "disconnect"
    assert first[0].raised == first[1].raised and first[0].raised
    # What happened: nothing versus a committed refund, so the naive retry differs.
    assert (before.effects_committed, before.verdict) == (1, "correct")
    assert (after.effects_committed, after.verdict) == (2, "duplicate")


# --- the agent's answer after the cut ------------------------------------------------


def test_giving_up_after_the_cut_is_honest_but_not_done(stack, tmp_path):
    run = _run(stack, _script(tmp_path, ONE_ATTEMPT.format(after_error="unknown")), trials=1)
    [t] = run.trials
    assert (t.refunds, t.verdict, t.fault_fired) == (0, "in_doubt_surfaced", True)
    assert not t.safety_failure
    assert (run.result, run.exit_code) == ("WARN", 0)


def test_claiming_success_after_the_cut_is_a_lost_refund(stack, tmp_path):
    run = _run(stack, _script(tmp_path, ONE_ATTEMPT.format(after_error="done")), trials=1)
    [t] = run.trials
    assert (t.refunds, t.verdict, t.report_matches_sink) == (0, "lost", False)
    assert t.safety_failure
    assert (run.result, run.exit_code) == ("FAIL", 1)


def test_an_agent_that_never_calls_is_inconclusive(stack, tmp_path):
    never = 'import json; print(json.dumps({"report": "done"}))\n'
    run = _run(stack, _script(tmp_path, never), trials=1)
    assert (run.result, run.exit_code) == ("INCONCLUSIVE", 3)
    assert run.trials[0].fault_record is None


# --- exposed through crashtest run, selftest and the controller -----------------------


def test_crashtest_run_and_selftest_on_the_cut(monkeypatch, stack, capsys):
    monkeypatch.setattr(cli, "_start_stack", lambda data_dir, cleanup: stack)
    code = cli.main(["run", CUT, "--agent", "naive", "--trials", "1", "--format", "json"])
    report = json.loads(capsys.readouterr().out)
    assert (code, report["result"], report["scenario"]) == (0, "PASS", CUT)
    assert cli.main(["selftest", "--scenario", CUT, "--trials", "1"]) == 0
    assert "selftest passed (refunds-cut-before-request)" in capsys.readouterr().out


def test_selftest_blames_the_harness_if_the_cut_is_not_confirmed():
    def run(verdict: str, fired: bool) -> rr.RunReport:
        trial = rr.Trial(1, "o", 1, 1, [None], fired, "done", True, verdict, True, 0, "", 0.1)
        return rr.summarise(
            rr.RunReport(agent="x", trials=[trial], client_timeout_s=2, fault_delay_s=0,
                         amount_cents=2500, scenario=CUT)
        )  # fmt: skip

    assert rr.check_controls(run("correct", True), run("correct", True), CUT) == []
    problems = rr.check_controls(run("correct", False), run("correct", True), CUT)
    assert any("not confirmed" in p for p in problems)


async def test_the_controller_runs_the_cut(stack):
    controller = Controller(
        sink_url=stack.sink_url, mcp_url=stack.mcp_url, token=stack.control_token
    )
    try:
        async with Client(build_server(controller)) as dev:
            created = (await dev.call_tool("create_test_run", {"scenario": CUT})).structured_content
            assert created["agent_mcp_url"] != stack.mcp_url  # the proxy's
            assert {s["name"] for s in created["scenarios"]} == set(scenarios.names())
            run_id, order_id = created["run_id"], created["trials"][0]["order_id"]
            await dev.call_tool("arm_fault", {"run_id": run_id, "trial": 1})
            said = rr._client_report(
                (await _run_agent_async("naive", created["agent_mcp_url"], order_id)).stdout
            )
            out = (
                await dev.call_tool(
                    "get_test_report", {"run_id": run_id, "agent_reports": {"1": said}}
                )
            ).structured_content
    finally:
        controller.close()
    report = json.loads(out["report"])
    assert (out["result"], out["scenario"]) == ("PASS", CUT)
    [t] = report["observed"]["per_trial"]
    assert (t["verdict"], t["fault_fired"], t["fault_record"]["request_withheld"]) == (
        "correct",
        True,
        True,
    )


async def _run_agent_async(name: str, url: str, order_id: str):
    import asyncio
    import subprocess

    from crashtest import control_token

    def go():
        return subprocess.run(
            [*_agent(name), "--mcp-url", url, "--order-id", order_id, "--amount-cents", "2500",
             "--timeout", "2"],
            capture_output=True, text=True, timeout=120, env=control_token.agent_env(),
        )  # fmt: skip

    return await asyncio.to_thread(go)
