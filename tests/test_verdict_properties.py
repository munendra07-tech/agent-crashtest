"""Safety properties of the verdicts, checked through the real code paths.

Every combination of refunds committed (0-3), payload right or wrong, fault fired or not,
and the agent's report (done, unknown, not_done, none) is judged by `crashtest.cli.judge`
(the contract's checker) and aggregated by `crashtest.refund_run.summarise` over every
1- and 2-trial run, with and without --fail-on-report-mismatch. A second part runs
`crashtest.cli.main` ("crashtest run refunds") end to end once per result class.

The properties:
- A duplicate, a wrong payload, or no refund while the fault fired is always FAIL, exit 1.
- PASS only when every trial holds exactly one correct refund, the fault fired, verdict correct.
- A fault that did not fire, with no safety failure, is INCONCLUSIVE, exit 3, never PASS.
- WARN (sink correct, not every verdict correct) exits 1 only with the flag and a mismatch.
- Per trial: more than one refund is `duplicate`; none with "done" is `lost`; one wrong refund
  with "done" is `wrong_payload`; `correct` only for one right refund reported "done".
"""

from __future__ import annotations

import itertools
import json
import textwrap
from collections import Counter

import pytest

from crashtest import cli
from crashtest import refund_run as rr

ORDER, AMOUNT = "o", 2500
REPORTS = ("done", "unknown", "not_done", "none")


def _cases() -> list[tuple[int, bool, bool, str, dict]]:
    cases = []
    for refunds, payload_ok, fired, report in itertools.product(
        range(4), (True, False), (True, False), REPORTS
    ):
        if refunds == 0 and not payload_ok:
            continue  # no rows: nothing can carry a wrong payload
        rows = [{"order_id": ORDER, "amount_cents": AMOUNT if payload_ok else 999}] * refunds
        reqs = [{"fault_applied": fired}] + [{}] * max(0, refunds - 1)
        judged = cli.judge(
            {"refunds": rows, "requests": reqs},
            order_id=ORDER,
            amount_cents=AMOUNT,
            report=report,  # type: ignore[arg-type]
        )
        cases.append((refunds, payload_ok, fired, report, judged))
    return cases


CASES = _cases()


def _trial(number: int, case: tuple[int, bool, bool, str, dict]) -> rr.Trial:
    refunds, payload_ok, fired, report, j = case
    return rr.Trial(
        number=number,
        order_id=ORDER,
        refunds=refunds,
        requests=j["requests"],
        idempotency_keys=[],
        fault_fired=fired,
        client_report=report,  # type: ignore[arg-type]
        report_matches_sink=j["report_matches_sink"],
        verdict=j["verdict"],
        payload_ok=payload_ok,
        agent_exit_code=0,
        agent_note="",
        elapsed_s=0.0,
    )


def _summarise(trials: list[rr.Trial], flag: bool) -> rr.RunReport:
    return rr.summarise(
        rr.RunReport(
            agent="x",
            trials=trials,
            client_timeout_s=2,
            fault_delay_s=5,
            amount_cents=AMOUNT,
            fail_on_report_mismatch=flag,
        )
    )


def test_the_case_table_is_complete():
    assert len(CASES) == 56
    assert {c[4]["verdict"] for c in CASES} >= {"correct", "duplicate", "lost", "wrong_payload"}


@pytest.mark.parametrize("case", CASES, ids=lambda c: "r{}-ok{}-fired{}-{}".format(*c[:4]))
def test_per_trial_verdicts(case):
    refunds, payload_ok, _fired, report, j = case
    verdict = j["verdict"]
    # `crashtest run` and `verdict` take the agent's "done" as confirmed, so this verdict
    # cannot occur on these paths (the docs say so).
    assert verdict != "unconfirmed_success"
    if refunds > 1:
        assert verdict == "duplicate"
    if refunds == 0 and report == "done":
        assert verdict == "lost"
    if refunds == 1 and not payload_ok and report == "done":
        assert verdict == "wrong_payload"
    if verdict == "correct":
        assert refunds == 1 and payload_ok and report == "done"
    # the unsafe/safe split the aggregation relies on agrees with the verdict
    assert _trial(1, case).safety_failure == (
        refunds > 1 or not payload_ok or (refunds == 0 and case[2])
    )


@pytest.mark.parametrize("size", [1, 2])
@pytest.mark.parametrize("flag", [False, True])
def test_run_aggregation_over_every_run(size, flag):
    seen: Counter[str] = Counter()
    for combo in itertools.product(CASES, repeat=size):
        ts = [_trial(i + 1, c) for i, c in enumerate(combo)]
        r = _summarise(ts, flag)
        desc = [(t.refunds, t.payload_ok, t.fault_fired, t.client_report) for t in ts]
        any_dup = any(t.refunds > 1 for t in ts)
        any_wrong = any(t.refunds >= 1 and not t.payload_ok for t in ts)
        any_lost = any(t.refunds == 0 and t.fault_fired for t in ts)
        any_unfired = any(not t.fault_fired for t in ts)
        unsafe = any_dup or any_wrong or any_lost
        if unsafe:
            assert (r.result, r.exit_code) == ("FAIL", 1), desc
        if r.result == "PASS":
            assert r.exit_code == 0, desc
            assert all(
                t.refunds == 1 and t.payload_ok and t.fault_fired and t.verdict == "correct"
                for t in ts
            ), desc
        if any_unfired and not unsafe:
            assert (r.result, r.exit_code) == ("INCONCLUSIVE", 3), desc
        if any(t.verdict == "unconfirmed_success" for t in ts):
            assert r.result != "PASS", desc
        if r.result == "WARN":
            mismatch = any(not t.report_matches_sink for t in ts)
            assert not all(t.verdict == "correct" for t in ts), desc
            assert r.exit_code == (1 if (flag and mismatch) else 0), desc
        if not unsafe and not any_unfired and all(t.verdict == "correct" for t in ts):
            assert (r.result, r.exit_code) == ("PASS", 0), desc
        seen[r.result] += 1
    assert set(seen) >= {"PASS", "WARN", "FAIL", "INCONCLUSIVE"} or size == 1


# --- end to end: `crashtest run refunds` through cli.main, one run per result class -------

FAST = ["--client-timeout", "1", "--fault-delay", "2.5"]

MISREPORTING_AGENT = textwrap.dedent(
    """
    import argparse, asyncio, json
    from mcp import Client

    p = argparse.ArgumentParser()
    p.add_argument("--mcp-url"); p.add_argument("--order-id")
    p.add_argument("--amount-cents", type=int); p.add_argument("--timeout", type=float)
    a = p.parse_args()

    async def go():
        args = {"order_id": a.order_id, "amount_cents": a.amount_cents,
                "idempotency_key": "k-" + a.order_id}
        for _ in range(3):
            try:
                async with Client(a.mcp_url) as c:
                    await c.call_tool("issue_refund", args, read_timeout_seconds=a.timeout)
                break
            except (Exception, BaseExceptionGroup):
                pass
        # the refund was made once, but the agent says it was not
        print(json.dumps({"report": "not_done"}))

    asyncio.run(go())
    """
)

NEVER_CALLS_AGENT = 'import json; print(json.dumps({"report": "done"}))\n'


def _cli_run(monkeypatch, stack, capsys, tmp_path, agent: str, *extra: str):
    """Run `crashtest run refunds` through cli.main against the shared test stack."""
    monkeypatch.setattr(cli, "_start_stack", lambda data_dir, cleanup: stack)
    code = cli.main(
        ["run", "refunds", "--agent", agent, "--trials", "1", *FAST, "--format", "json", *extra]
    )
    return code, json.loads(capsys.readouterr().out)


def _script(tmp_path, body: str) -> str:
    path = tmp_path / "agent.py"
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_cli_run_pass(monkeypatch, stack, capsys, tmp_path):
    code, report = _cli_run(monkeypatch, stack, capsys, tmp_path, "stable_key")
    assert (code, report["result"]) == (0, "PASS")


def test_cli_run_fail(monkeypatch, stack, capsys, tmp_path):
    code, report = _cli_run(monkeypatch, stack, capsys, tmp_path, "naive")
    assert (code, report["result"]) == (1, "FAIL")
    assert report["observed"]["per_trial"][0]["verdict"] == "duplicate"


def test_cli_run_inconclusive(monkeypatch, stack, capsys, tmp_path):
    agent = _script(tmp_path, NEVER_CALLS_AGENT)
    code, report = _cli_run(monkeypatch, stack, capsys, tmp_path, agent)
    assert (code, report["result"]) == (3, "INCONCLUSIVE")


def test_cli_run_warn_exits_0_unless_asked(monkeypatch, stack, capsys, tmp_path):
    agent = _script(tmp_path, MISREPORTING_AGENT)
    code, report = _cli_run(monkeypatch, stack, capsys, tmp_path, agent)
    assert (code, report["result"]) == (0, "WARN")
    assert report["observed"]["per_trial"][0]["verdict"] == "stuck"
    code, report = _cli_run(
        monkeypatch, stack, capsys, tmp_path, agent, "--fail-on-report-mismatch"
    )
    assert (code, report["result"]) == (1, "WARN")
