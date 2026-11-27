"""`crashtest run refunds`: the example agents, the report, its formats and exit codes."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from crashtest import cli
from crashtest import refund_run as rr

ROOT = Path(__file__).resolve().parents[1]
# Short timings keep the end-to-end tests fast; the margin is the same 2.5x as the defaults.
FAST = {"client_timeout_s": 1.0, "fault_delay_s": 2.5}


def _run(stack, agent: str, trials: int = 2, **extra) -> rr.RunReport:
    return rr.run_refunds(
        agent=agent,
        agent_cmd=[sys.executable, str(rr.resolve_agent(agent))],
        mcp_url=stack.mcp_url,
        sink_url=stack.sink_url,
        trials=trials,
        progress=False,
        **FAST,
        **extra,
    )


def test_the_naive_agent_fails_with_a_duplicate(stack):
    run = _run(stack, "naive")
    assert (run.result, run.exit_code) == ("FAIL", 1)
    assert [t.refunds for t in run.trials] == [2, 2]
    assert all(t.verdict == "duplicate" and t.fault_fired for t in run.trials)
    assert all(t.client_report == "done" and not t.report_matches_sink for t in run.trials)
    assert "without an idempotency key" in run.causes[0]
    assert run.fixes == [rr.FIX_STABLE_KEY]


def test_the_stable_key_agent_passes(stack):
    run = _run(stack, "stable_key")
    assert (run.result, run.exit_code) == ("PASS", 0)
    assert [(t.refunds, t.requests, t.verdict) for t in run.trials] == [(1, 2, "correct")] * 2
    assert run.causes == run.fixes == run.caveats == []


def _agent(tmp_path: Path, body: str) -> str:
    path = tmp_path / "agent.py"
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_an_agent_that_never_calls_the_tool_is_inconclusive(stack, tmp_path):
    agent = _agent(tmp_path, 'import json; print(json.dumps({"report": "done"}))\n')
    run = _run(stack, agent, trials=1)
    assert (run.result, run.exit_code) == ("INCONCLUSIVE", 3)
    assert run.trials[0].fault_fired is False
    assert "did not call issue_refund" in run.causes[0]


def test_an_agent_that_gives_up_honestly_warns_but_passes_the_gate(stack, tmp_path):
    """One refund in the sink and an honest 'unknown': not `correct`, but not unsafe."""
    agent = _agent(
        tmp_path,
        (ROOT / "examples" / "stable_key_agent.py")
        .read_text(encoding="utf-8")
        .replace("MAX_ATTEMPTS = 3", "MAX_ATTEMPTS = 1"),
    )
    run = _run(stack, agent, trials=1)
    assert (run.result, run.exit_code) == ("WARN", 0)
    assert run.trials[0].verdict == "in_doubt_surfaced"


def _trial(**kw) -> rr.Trial:
    base = dict(
        number=1,
        order_id="ord-1",
        refunds=1,
        requests=2,
        idempotency_keys=["k", "k"],
        fault_fired=True,
        client_report="done",
        report_matches_sink=True,
        verdict="correct",
        payload_ok=True,
        agent_exit_code=0,
        agent_note="",
        elapsed_s=1.0,
    )
    base.update(kw)
    t = rr.Trial(**base)  # type: ignore[arg-type]
    t.likely_cause, t.recommended_fix = rr.diagnose(t)
    return t


def _report(*trials: rr.Trial, mismatch: bool = False) -> rr.RunReport:
    return rr.summarise(
        rr.RunReport(
            agent="mine.py",
            trials=list(trials),
            client_timeout_s=2.0,
            fault_delay_s=5.0,
            amount_cents=2500,
            fail_on_report_mismatch=mismatch,
        )
    )


DUP = {"refunds": 2, "idempotency_keys": [None, None], "verdict": "duplicate"}
# The sink holds one refund, but the agent said it failed: a report mismatch.
MISMATCH = {"client_report": "not_done", "report_matches_sink": False, "verdict": "stuck"}


@pytest.mark.parametrize(
    ("trials", "mismatch", "result", "code"),
    [
        ([{}], False, "PASS", 0),
        ([{}, DUP], False, "FAIL", 1),
        ([{"refunds": 1, "payload_ok": False, "verdict": "wrong_payload"}], False, "FAIL", 1),
        ([{"refunds": 0, "verdict": "lost"}], False, "FAIL", 1),
        ([{"refunds": 0, "fault_fired": False, "verdict": "stuck"}], False, "INCONCLUSIVE", 3),
        ([DUP, {"refunds": 0, "fault_fired": False}], False, "FAIL", 1),  # a duplicate wins
        ([MISMATCH], False, "WARN", 0),  # reported, but does not fail the run
        ([MISMATCH], True, "WARN", 1),  # unless asked to
        ([{"client_report": "unknown", "verdict": "in_doubt_surfaced"}], True, "WARN", 0),
    ],
)
def test_result_and_exit_code(trials, mismatch, result, code):
    run = _report(*(_trial(number=i + 1, **t) for i, t in enumerate(trials)), mismatch=mismatch)
    assert (run.result, run.exit_code) == (result, code)


@pytest.mark.parametrize(
    ("keys", "says"),
    [
        ([None, None], "without an idempotency key"),
        (["a", "b"], "different idempotency key"),
    ],
)
def test_the_likely_cause_names_how_the_agent_retried(keys, says):
    assert says in _trial(**{**DUP, "idempotency_keys": keys}).likely_cause


def test_every_format_keeps_observed_cause_and_fix_apart():
    run = _report(_trial(), _trial(number=2, **DUP))
    text = rr.format_text(run)
    observed, rest = text.split("LIKELY CAUSE (inference")
    cause, fix = rest.split("RECOMMENDED FIX")
    assert "actual refunds (in the sink): 3" in observed and "retried" not in observed
    assert "1 of 2 trials refunded exactly once" in observed and "not a rate" in observed
    assert rr.SCOPE in text and rr.SANDBOX in text
    assert "retried without an idempotency key" in cause
    assert "idempotency key from the business operation" in fix

    md = rr.format_markdown(run)
    assert md.index("### Observed") < md.index("### Likely cause") < md.index("### Recommended")

    data = json.loads(rr.format_json(run))
    assert data["result"] == "FAIL" and data["exit_code"] == 1
    assert (data["scope"], data["sandbox"]) == (rr.SCOPE, rr.SANDBOX)
    assert data["repeatability"].startswith("1 of 2 trials")
    assert data["observed"]["actual_refunds"] == 3
    assert data["observed"]["duplicates_per_trial"] == [0, 1]
    assert data["likely_cause"]["inference"] is True
    assert data["recommended_fix"] == [rr.FIX_STABLE_KEY]

    suite = ET.fromstring(rr.format_junit(run))
    assert (suite.get("tests"), suite.get("failures")) == ("2", "1")
    failures = suite.findall("testcase/failure")
    assert [f.get("type") for f in failures] == ["duplicate"]
    assert "Likely cause (inference)" in (failures[0].text or "")


def test_junit_fails_a_mismatch_only_when_asked():
    assert ET.fromstring(rr.format_junit(_report(_trial(**MISMATCH)))).get("failures") == "0"
    suite = ET.fromstring(rr.format_junit(_report(_trial(**MISMATCH), mismatch=True)))
    assert [f.get("type") for f in suite.findall("testcase/failure")] == ["report_mismatch"]


def test_the_agent_report_is_the_last_json_line():
    assert rr._client_report('log\n{"report": "done"}\n') == "done"
    assert rr._client_report('{"report": "maybe"}') == "none"
    assert rr._client_report("no json here") == "none"
    assert rr._client_report("") == "none"


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--agent", "no/such/agent.py"], "neither naive nor stable_key"),
        (["--agent", "naive", "--trials", "0"], "--trials"),
        (["--agent", "naive", "--client-timeout", "5", "--fault-delay", "5"], "shorter"),
    ],
)
def test_configuration_errors_exit_2(argv, message, capsys):
    assert cli.main(["run", "refunds", *argv]) == 2
    assert message in capsys.readouterr().err


def test_run_refunds_end_to_end_starts_its_own_stack(tmp_path, monkeypatch, capsys):
    pytest.importorskip("pgserver")
    monkeypatch.chdir(tmp_path)
    junit = tmp_path / "junit.xml"
    code = cli.main(
        [
            "run",
            "refunds",
            "--agent",
            "naive",
            "--trials",
            "1",
            "--client-timeout",
            "1",
            "--fault-delay",
            "2.5",
            "--format",
            "json",
            "--junit-file",
            str(junit),
            "--markdown-file",
            str(tmp_path / "summary.md"),
            "--data-dir",
            str(tmp_path / "pg"),
        ]
    )
    assert code == 1
    assert json.loads(capsys.readouterr().out)["result"] == "FAIL"
    assert ET.parse(junit).getroot().get("failures") == "1"
    assert "**FAIL**" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_the_package_list_names_every_package():
    """pyproject lists packages by hand (to install examples/); none may be missing."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    listed = set(config["tool"]["setuptools"]["packages"])
    found = {
        ".".join(p.parent.relative_to(ROOT / "src").parts)
        for p in (ROOT / "src" / "crashtest").rglob("__init__.py")
    }
    assert listed == found | {"crashtest.examples"}


def test_both_example_agents_are_found():
    for name in rr.BUILTIN_AGENTS:
        assert rr.find_builtin_agent(name).is_file()


# --- selftest -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("naive", "safe", "says"),
    [
        ([DUP, DUP], [{}, {}], None),  # both controls behave
        ([DUP, {}], [{}, {}], "no duplicate in trial(s) 2"),
        ([{}, {}], [{}, {}], "should FAIL but got PASS"),
        ([DUP, {**DUP, "fault_fired": False}], [{}, {}], "not confirmed for the naive"),
        ([DUP, DUP], [{}, DUP], "should PASS but got FAIL"),
        ([DUP, DUP], [{"fault_fired": False, "refunds": 0}], "not confirmed for the stable-key"),
    ],
)
def test_selftest_checks_both_controls(naive, safe, says):
    problems = rr.check_controls(
        _report(*(_trial(number=i + 1, **t) for i, t in enumerate(naive))),
        _report(*(_trial(number=i + 1, **t) for i, t in enumerate(safe))),
    )
    if says is None:
        assert problems == []
    else:
        assert any(says in p for p in problems), problems


def test_selftest_end_to_end(tmp_path, monkeypatch, capsys):
    pytest.importorskip("pgserver")
    monkeypatch.chdir(tmp_path)
    code = cli.main(["selftest", "--trials", "1", "--data-dir", str(tmp_path / "pg")])
    out = capsys.readouterr().out
    assert code == 0, out
    assert out.strip() == rr.SELFTEST_PASSED


def test_selftest_reports_a_misbehaving_control(monkeypatch, capsys):
    """If the harness stops detecting the duplicate, selftest blames the harness: exit 1."""
    fake_stack = type("S", (), {"mcp_url": "m", "sink_url": "s", "control_token": "t"})()
    monkeypatch.setattr(cli, "_start_stack", lambda data_dir, cleanup, *transports: fake_stack)
    monkeypatch.setattr(
        rr, "run_refunds", lambda **kw: _report(_trial())
    )  # every control "passes": the naive one misbehaves
    assert cli.main(["selftest"]) == 1
    out = capsys.readouterr().out
    assert "the harness, not your agent, is at fault" in out
    assert "naive retry agent should FAIL but got PASS" in out


def test_selftest_environment_error_exits_2(monkeypatch, capsys):
    def no_stack(data_dir, cleanup, *transports):
        raise rr.SetupError("the local stack did not start: test")

    monkeypatch.setattr(cli, "_start_stack", no_stack)
    assert cli.main(["selftest"]) == 2
    assert "did not start" in capsys.readouterr().err


def test_reports_say_what_ran_the_test_and_nothing_personal():
    import getpass
    import platform
    import socket

    run = _report(_trial())
    data = json.loads(rr.format_json(run))
    assert data["report_schema"] == rr.REPORT_SCHEMA
    env = data["environment"]
    assert set(env) == {"agent_crashtest", "mcp", "python", "os", "os_release", "machine"}
    assert env["python"] == platform.python_version() and env["os"] == platform.system()
    text = json.dumps(env)
    for personal in {socket.gethostname(), getpass.getuser(), str(Path.home())}:
        assert len(personal) < 4 or personal not in text
    props = {
        p.get("name"): p.get("value")
        for p in ET.fromstring(rr.format_junit(run)).findall("properties/property")
    }
    assert props["env_python"] == env["python"] and props["env_mcp"] == env["mcp"]


def test_the_check_status_agent_passes_with_the_lookup_caveat(stack):
    """A lookup avoided the duplicate, but nothing enforced it: the report says so."""
    agent = str(ROOT / "examples" / "check_status_agent.py")
    run = _run(stack, agent, trials=1)
    assert (run.result, run.exit_code) == ("PASS", 0)
    assert run.trials[0].idempotency_keys == [None]
    assert run.caveats == [rr.CAVEAT_NO_KEY_LOST_RESPONSE]
    assert "nothing to fix" not in rr.format_text(run)
