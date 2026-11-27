"""tools/triage_report.py: the milestone 1 rules, applied to Tester report issues."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("triage_report", ROOT / "tools" / "triage_report.py")
assert _spec and _spec.loader
tr = importlib.util.module_from_spec(_spec)
sys.modules["triage_report"] = tr
_spec.loader.exec_module(tr)

PASS_OUTPUT = (
    "crashtest run refunds: PASS - 5 of 5 trials refunded exactly once (verdict correct)\n"
    "agent: my_agent.py; fault: lost response; exit code 0\n"
)


def body(**over: str) -> str:
    """An issue body as GitHub renders the Tester report form."""
    fields = {
        tr.F_SUBMISSION: tr.COMPLETED_RUN,
        tr.F_RELATION: "Independent tester (not a maintainer or contributor of agent-crashtest)",
        tr.F_SELFTEST: "Passed",
        tr.F_SELFTEST_CUT: "Not run",
        tr.F_REFUNDS: "PASS",
        tr.F_CUT: "Not run",
        tr.F_OUTPUT: "```text\n" + PASS_OUTPUT + "```",
        tr.F_OS: "Ubuntu 24.04",
        tr.F_PYTHON: "3.12.4",
        tr.F_VERSION: "0.3.0",
        tr.F_INSTALL: "pip install from a release tag",
        tr.F_CLIENT: "Pydantic AI 2.68",
        "How your agent or client connected": "An agent script run by crashtest run",
        tr.F_QUOTE: "Yes, anonymously",
        tr.F_CHECKS: "- [X] I removed secrets, tokens, customer data and anything private.",
    }
    fields.update(over)
    return "\n\n".join(f"### {k}\n\n{v or '_No response_'}" for k, v in fields.items())


def issue(number: int, author: str = "alice", labels=("tester-report",), **over: str) -> dict:
    return {
        "number": number,
        "url": f"https://github.com/x/y/issues/{number}",
        "author": {"login": author},
        "labels": [{"name": n} for n in labels],
        "body": body(**over),
    }


def one(**over) -> tr.Report:
    return tr.triage([issue(1, **over)])["reports"][0]


# --- the form and the script stay in line ---------------------------------------------


def test_every_field_the_script_reads_is_in_the_issue_form():
    form = (ROOT / ".github" / "ISSUE_TEMPLATE" / "tester_report.yml").read_text(encoding="utf-8")
    labels = {m.strip().strip('"') for m in re.findall(r"^\s+label:\s*(.+)$", form, flags=re.M)}
    used = [v for k, v in vars(tr).items() if k.startswith("F_")]
    assert used and [f for f in used if f not in labels] == []
    for option in (tr.COMPLETED_RUN, tr.SELFTEST_ONLY, tr.INDEPENDENT, tr.NOT_RUN, tr.ENV_ERROR):
        assert option in form


# --- one report -------------------------------------------------------------------------


def test_a_completed_independent_run_with_matching_output_counts():
    r = one()
    assert (r.verdict, r.reasons) == ("counts", [])
    assert r.setup == ("linux", "pydantic ai")


def test_a_self_test_only_report_does_not_count_and_is_tracked():
    t = tr.triage([issue(3, **{tr.F_SUBMISSION: tr.SELFTEST_ONLY + " (crashtest selftest, ...)"})])
    assert t["reports"][0].verdict == "does_not_count"
    assert t["self_test_only"] == [3] and t["counted"] == 0


def test_a_maintainers_run_does_not_count():
    r = one(**{tr.F_RELATION: "Maintainer or contributor of agent-crashtest"})
    assert r.verdict == "does_not_count" and "independent" in r.reasons[0]


def test_both_scenarios_not_run_does_not_count():
    assert one(**{tr.F_REFUNDS: "Not run"}).verdict == "does_not_count"


def test_the_same_tester_counts_once():
    t = tr.triage([issue(1), issue(2), issue(3, author="bob")])
    assert [r.verdict for r in t["reports"]] == ["counts", "does_not_count", "counts"]
    assert "already has a counted report" in t["reports"][1].reasons[0]


@pytest.mark.parametrize(
    "output, why",
    [
        ("crashtest run refunds: FAIL - 3 of 3 trials refunded more than once", "shows FAIL"),
        ("pip install worked", "no 'crashtest run refunds' result"),
    ],
)
def test_output_that_does_not_support_the_result_needs_a_human(output, why):
    r = one(**{tr.F_OUTPUT: output})
    assert r.verdict == "needs_human_check" and why in r.reasons[0]


def test_a_json_report_supports_the_result():
    report = json.dumps({"scenario": "refunds", "result": "PASS", "exit_code": 0}, indent=2)
    assert one(**{tr.F_OUTPUT: report}).verdict == "counts"


def test_the_cut_scenario_alone_can_count():
    out = "crashtest run refunds-cut-before-request: PASS - 3 of 3 trials refunded exactly once"
    r = one(**{tr.F_REFUNDS: "Not run", tr.F_CUT: "PASS", tr.F_OUTPUT: out})
    assert r.verdict == "counts"


def test_an_environment_error_needs_a_human():
    r = one(**{tr.F_REFUNDS: tr.ENV_ERROR, tr.F_OUTPUT: "crashtest: the local stack did not start"})
    assert r.verdict == "needs_human_check" and "environment error" in r.reasons[0]


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-" + "a" * 40,
        "ghp_" + "A" * 36,
        '"control_token": "' + "x" * 43 + '"',
        "-----BEGIN OPENSSH PRIVATE KEY-----",
    ],
)
def test_a_possible_secret_is_never_counted_automatically(secret):
    r = one(**{tr.F_OUTPUT: PASS_OUTPUT + secret})
    assert r.verdict == "needs_human_check" and "may contain a secret" in r.reasons[0]


def test_the_secrets_box_must_be_ticked():
    r = one(**{tr.F_CHECKS: "- [ ] I removed secrets"})
    assert r.verdict == "needs_human_check"


def test_empty_optional_fields_read_as_empty():
    r = one(**{tr.F_CLIENT: ""})
    assert r.client == "" and r.verdict == "needs_human_check"
    assert "no MCP client" in r.reasons[0]


@pytest.mark.parametrize(
    "text, family",
    [
        ("Windows 11", "windows"),
        ("Win10 Pro", "windows"),
        ("macOS 15 (Apple silicon)", "macos"),
        ("Ubuntu 24.04", "linux"),
        ("WSL2 on Windows 11", "linux"),
        ("Fedora 41", "linux"),
        ("my laptop", "unknown"),
    ],
)
def test_os_family(text, family):
    assert tr.os_family(text) == family


def test_client_versions_do_not_make_a_new_setup():
    assert tr.client_key("Pydantic AI 2.68") == tr.client_key("pydantic ai v2.70.1")
    assert tr.client_key("LangGraph 1.2 + langchain-mcp-adapters 0.3") != tr.client_key("mcp 2.3.0")


def test_issues_without_the_label_are_ignored():
    assert tr.triage([issue(1, labels=("bug",))])["reports"] == []


# --- the milestone ----------------------------------------------------------------------


def test_five_reports_from_two_setups_reach_the_milestone():
    issues = [issue(n, author=f"t{n}") for n in range(1, 5)]
    issues.append(issue(5, author="t5", **{tr.F_OS: "Windows 11"}))
    t = tr.triage(issues)
    assert (t["counted"], len(t["setups"]), t["milestone_reached"]) == (5, 2, True)


def test_five_reports_from_one_setup_do_not():
    t = tr.triage([issue(n, author=f"t{n}") for n in range(1, 6)])
    assert (t["counted"], len(t["setups"]), t["milestone_reached"]) == (5, 1, False)


def test_reports_are_judged_oldest_first():
    t = tr.triage([issue(9), issue(4)])
    assert [(r.number, r.verdict) for r in t["reports"]] == [(4, "counts"), (9, "does_not_count")]


# --- the command ------------------------------------------------------------------------


def test_the_command_prints_the_tracker(tmp_path, capsys):
    f = tmp_path / "issues.json"
    f.write_text(json.dumps([issue(1), issue(2, author="bob", **{tr.F_OUTPUT: "nothing"})]))
    assert tr.main(["--file", str(f)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("## Milestone 1: 1 of 5 counted reports, 1 of 2 setups")
    assert "| 1 | #1 | alice | yes | yes | Ubuntu 24.04 | Pydantic AI 2.68 | PASS |" in out
    assert "- #2: refunds: the form says PASS" in out


def test_the_command_json_output(tmp_path, capsys):
    f = tmp_path / "issues.json"
    f.write_text(json.dumps([issue(1)]))
    assert tr.main(["--file", str(f), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["counted"] == 1 and data["reports"][0]["verdict"] == "counts"


def test_unreadable_issues_exit_2(tmp_path, capsys):
    assert tr.main(["--file", str(tmp_path / "missing.json")]) == 2
    assert "cannot read the issues" in capsys.readouterr().err
