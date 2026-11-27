"""Triage Tester report issues and count them toward milestone 1.

Plain code, no model: it reads the fields of the Tester report issue form
(`.github/ISSUE_TEMPLATE/tester_report.yml`) from each issue body, applies the
milestone 1 rules, and checks the pasted output against the result the tester
chose. Issue text is only parsed, never acted on.

Milestone 1: 5 counted reports from at least 2 different setups. A report counts
only when all of these hold:
  1. it is a Tester report issue;
  2. "What did you run?" is a completed run with the tester's own agent or client;
  3. the tester is independent (not a maintainer or contributor);
  4. at least one scenario result is not "Not run", and the full output supports it;
  5. the tester is a different person from the reports already counted.
A setup is the operating system family (Windows, macOS, Linux; WSL counts as
Linux) with the MCP client or framework. Two reports differ in setup when either
one differs.

Usage:
  python tools/triage_report.py                       # issues labelled tester-report, via gh
  python tools/triage_report.py --repo OWNER/NAME
  python tools/triage_report.py --file issues.json    # output of gh issue list --json ...
  add --json for machine-readable output

Exit code 0 in every case where the triage ran; 2 when the issues cannot be read.
The output is for the maintainer: nothing is posted, labelled or closed.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

DEFAULT_REPO = "munendra07-tech/agent-crashtest"
LABEL = "tester-report"
MILESTONE_REPORTS = 5
MILESTONE_SETUPS = 2

# Field labels, exactly as the issue form renders them as "### <label>" headings.
F_SUBMISSION = "What did you run?"
F_RELATION = "Your relation to the project"
F_SELFTEST = "crashtest selftest"
F_SELFTEST_CUT = "crashtest selftest --scenario refunds-cut-before-request"
F_REFUNDS = "Your agent or client: refunds (lost response after the commit)"
F_CUT = "Your agent or client: refunds-cut-before-request (connection cut before the request)"
F_OUTPUT = "Full output"
F_OS = "Operating system"
F_PYTHON = "Python version"
F_VERSION = "agent-crashtest version"
F_INSTALL = "How you installed or ran it"
F_CLIENT = "MCP client or agent framework, with version"
F_QUOTE = "May the maintainer quote or count your result publicly?"
F_CHECKS = "Before submitting"

COMPLETED_RUN = "Completed run with my own agent or MCP client"
SELFTEST_ONLY = "Self-test only"
INDEPENDENT = "Independent tester"
NOT_RUN = "Not run"
ENV_ERROR = "Environment error (exit code 2)"
RESULTS = ("PASS", "WARN", "FAIL", "INCONCLUSIVE")

# The scenario each result field belongs to, as `crashtest run <scenario>` names it.
SCENARIO_FIELDS = {"refunds": F_REFUNDS, "refunds-cut-before-request": F_CUT}

# Things that look like credentials. A match never counts a report automatically:
# the maintainer checks it, and asks the tester to edit the issue if it is real.
SECRET_PATTERNS = {
    "Anthropic API key": r"sk-ant-[A-Za-z0-9_-]{20,}",
    "OpenAI-style API key": r"\bsk-[A-Za-z0-9]{32,}",
    "GitHub token": r"\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}",
    "AWS access key": r"\bAKIA[0-9A-Z]{16}\b",
    "crashtest control token": r'"(?:control_)?token"\s*:\s*"[A-Za-z0-9_-]{30,}"',
    "private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
}

Verdict = Literal["counts", "does_not_count", "needs_human_check"]


@dataclass
class Report:
    number: int
    url: str
    author: str
    submission: str
    relation: str
    results: dict[str, str]  # scenario -> the result the tester chose
    selftests: dict[str, str]  # scenario -> Not run / Passed / Failed
    os: str
    os_family: str
    python: str
    version: str
    install: str
    client: str
    client_key: str
    quote: str
    secrets_checkbox: bool
    output_results: dict[str, list[str]]  # scenario -> results found in the output
    verdict: Verdict = "does_not_count"
    reasons: list[str] = field(default_factory=list)
    new_setup: bool = False

    @property
    def setup(self) -> tuple[str, str]:
        return (self.os_family, self.client_key)


# --- parsing -----------------------------------------------------------------------


def parse_fields(body: str) -> dict[str, str]:
    """The issue form's fields: each '### <label>' heading and the text under it."""
    fields: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        heading = re.match(r"^###\s+(.+?)\s*$", line)
        if heading:
            if current is not None:
                fields[current] = "\n".join(lines).strip()
            current, lines = heading.group(1), []
        elif current is not None:
            lines.append(line)
    if current is not None:
        fields[current] = "\n".join(lines).strip()
    # GitHub writes "_No response_" for an optional field left empty.
    return {k: ("" if v == "_No response_" else v) for k, v in fields.items()}


def os_family(text: str) -> str:
    t = text.lower()
    if "wsl" in t:
        return "linux"
    if "windows" in t or re.search(r"\bwin\s?\d", t):
        return "windows"
    if any(w in t for w in ("macos", "mac os", "os x", "darwin", "apple")) or re.search(
        r"\bmac\b", t
    ):
        return "macos"
    if any(
        w in t
        for w in ("linux", "ubuntu", "debian", "fedora", "arch", "centos", "rhel", "alpine", "mint")
    ):
        return "linux"
    return "unknown"


def client_key(text: str) -> str:
    """The client or framework without versions, so 'Pydantic AI 2.68' == 'pydantic ai'."""
    t = text.lower()
    t = re.sub(r"\(.*?\)", " ", t)
    t = re.sub(r"\bv?\d+(?:\.\d+)*[a-z0-9.+-]*", " ", t)
    t = re.sub(r"[^a-z0-9+\-_/ ]", " ", t)
    return re.sub(r"\s+", " ", t).strip() or "none"


def output_results(output: str) -> dict[str, list[str]]:
    """The results that `crashtest run` printed in the pasted output, per scenario.

    Reads the text report's first line ("crashtest run refunds: PASS - ...") and the
    JSON report's "scenario" and "result" keys.
    """
    found: dict[str, list[str]] = {s: [] for s in SCENARIO_FIELDS}
    for scenario, result in re.findall(
        r"crashtest (?:run|controller) ([a-z-]+): (PASS|WARN|FAIL|INCONCLUSIVE)\b", output
    ):
        if scenario in found:
            found[scenario].append(result)
    # A JSON report (--format json) has one "scenario" and one "result" key each.
    scen = re.findall(r'"scenario"\s*:\s*"([a-z-]+)"', output)
    res = re.findall(r'"result"\s*:\s*"(PASS|WARN|FAIL|INCONCLUSIVE)"', output)
    if len(scen) == len(res):
        for s, r in zip(scen, res, strict=True):
            if s in found and r not in found[s]:
                found[s].append(r)
    return found


def _choice(value: str, options: tuple[str, ...]) -> str:
    for option in options:
        if value.startswith(option):
            return option
    return value


def parse_report(issue: dict[str, Any]) -> Report:
    f = parse_fields(issue.get("body") or "")
    output = f.get(F_OUTPUT, "")
    author = issue.get("author") or {}
    return Report(
        number=int(issue["number"]),
        url=issue.get("url", ""),
        author=(author.get("login") if isinstance(author, dict) else str(author)) or "",
        submission=_choice(f.get(F_SUBMISSION, ""), (COMPLETED_RUN, SELFTEST_ONLY)),
        relation=_choice(f.get(F_RELATION, ""), (INDEPENDENT, "Maintainer or contributor")),
        results={s: f.get(label, "").strip() for s, label in SCENARIO_FIELDS.items()},
        selftests={
            "refunds": f.get(F_SELFTEST, "").strip(),
            "refunds-cut-before-request": f.get(F_SELFTEST_CUT, "").strip(),
        },
        os=f.get(F_OS, ""),
        os_family=os_family(f.get(F_OS, "")),
        python=f.get(F_PYTHON, ""),
        version=f.get(F_VERSION, ""),
        install=f.get(F_INSTALL, ""),
        client=f.get(F_CLIENT, ""),
        client_key=client_key(f.get(F_CLIENT, "")),
        quote=f.get(F_QUOTE, ""),
        secrets_checkbox="[x]" in f.get(F_CHECKS, "").lower(),
        output_results=output_results(output),
    )


def secrets_found(text: str) -> list[str]:
    return [name for name, pattern in SECRET_PATTERNS.items() if re.search(pattern, text or "")]


# --- the rules -----------------------------------------------------------------------


def judge(report: Report, body: str, counted_authors: set[str]) -> Report:
    """Apply the milestone 1 rules to one report (`counted_authors`: already counted)."""
    fail: list[str] = []
    check: list[str] = []
    if report.submission != COMPLETED_RUN:
        fail.append("self-test only, or no completed run (tracked separately; does not count)")
    if report.relation != INDEPENDENT:
        fail.append("not an independent tester (maintainer or contributor)")
    ran = {s: r for s, r in report.results.items() if r and r != NOT_RUN}
    if not ran:
        fail.append('both scenario results are "Not run"')
    if report.author and report.author in counted_authors:
        fail.append(f"{report.author} already has a counted report")

    for scenario, chosen in ran.items():
        printed = report.output_results.get(scenario, [])
        if chosen in RESULTS:
            if chosen not in printed:
                check.append(
                    f"{scenario}: the form says {chosen}, but the output shows "
                    + (", ".join(printed) if printed else f"no 'crashtest run {scenario}' result")
                )
            elif len(set(printed)) > 1:
                check.append(f"{scenario}: the output shows several results ({', '.join(printed)})")
        elif chosen == ENV_ERROR:
            check.append(f"{scenario}: environment error; read the output and reproduce it")
        else:
            check.append(f"{scenario}: unrecognised result {chosen!r}")
    if report.os_family == "unknown":
        check.append(f"operating system not recognised: {report.os!r}")
    if report.client_key == "none" and report.submission == COMPLETED_RUN:
        check.append("no MCP client or framework given for a completed run")
    if found := secrets_found(body):
        check.append(
            f"the issue may contain a secret ({', '.join(found)}): ask the tester to remove it"
        )
    if not report.secrets_checkbox:
        check.append("the 'I removed secrets' box is not ticked")

    if fail:
        report.verdict, report.reasons = "does_not_count", fail + check
    elif check:
        report.verdict, report.reasons = "needs_human_check", check
    else:
        report.verdict, report.reasons = "counts", []
    return report


def triage(issues: list[dict[str, Any]]) -> dict[str, Any]:
    """Judge every report, oldest first, and work out milestone 1."""
    reports: list[Report] = []
    counted_authors: set[str] = set()
    setups: set[tuple[str, str]] = set()
    for issue in sorted(issues, key=lambda i: int(i["number"])):
        labels = {
            lab.get("name") if isinstance(lab, dict) else lab for lab in issue.get("labels", [])
        }
        if labels and LABEL not in labels:
            continue
        report = judge(parse_report(issue), issue.get("body") or "", counted_authors)
        if report.verdict == "counts":
            counted_authors.add(report.author)
            report.new_setup = report.setup not in setups
            setups.add(report.setup)
        reports.append(report)
    counted = [r for r in reports if r.verdict == "counts"]
    return {
        "reports": reports,
        "counted": len(counted),
        "setups": sorted(setups),
        "self_test_only": [r.number for r in reports if r.submission == SELFTEST_ONLY],
        "needs_human_check": [r.number for r in reports if r.verdict == "needs_human_check"],
        "milestone_reached": len(counted) >= MILESTONE_REPORTS and len(setups) >= MILESTONE_SETUPS,
    }


# --- output ----------------------------------------------------------------------------


def _cell(text: str) -> str:
    return (text or "-").replace("|", "/").replace("\n", " ")


def format_markdown(t: dict[str, Any]) -> str:
    out = [
        f"## Milestone 1: {t['counted']} of {MILESTONE_REPORTS} counted reports, "
        f"{len(t['setups'])} of {MILESTONE_SETUPS} setups"
        + (" (reached)" if t["milestone_reached"] else ""),
        "",
        "| # | Issue | Tester | Completed run? | Independent? | OS | Client or framework "
        "| refunds | cut-before-request | Counts? |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(t["reports"], 1):
        counts = {"counts": "yes", "does_not_count": "no", "needs_human_check": "check"}[r.verdict]
        if r.verdict == "counts" and not r.new_setup:
            counts += " (same setup as an earlier one)"
        out.append(
            "| "
            + " | ".join(
                _cell(x)
                for x in (
                    str(i),
                    f"#{r.number}",
                    r.author,
                    "yes" if r.submission == COMPLETED_RUN else "no",
                    "yes" if r.relation == INDEPENDENT else "no",
                    r.os,
                    r.client,
                    r.results["refunds"],
                    r.results["refunds-cut-before-request"],
                    counts,
                )
            )
            + " |"
        )
    reasons = [(r.number, why) for r in t["reports"] for why in r.reasons]
    if reasons:
        out += ["", "### Notes", *(f"- #{n}: {why}" for n, why in reasons)]
    if t["self_test_only"]:
        out += [
            "",
            "Self-test-only reports (tracked, not counted): "
            + ", ".join(f"#{n}" for n in t["self_test_only"]),
        ]
    out += [
        "",
        "Quoting: use a tester's name or result publicly only as their report allows "
        "(the 'May the maintainer quote' field).",
    ]
    return "\n".join(out) + "\n"


def format_json(t: dict[str, Any]) -> str:
    data = {
        **t,
        "reports": [asdict(r) for r in t["reports"]],
        "setups": [list(s) for s in t["setups"]],
    }
    return json.dumps(data, indent=2) + "\n"


def fetch(repo: str) -> list[dict[str, Any]]:
    proc = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--label",
            LABEL,
            "--state",
            "all",
            "--limit",
            "500",
            "--json",
            "number,title,body,author,url,labels,createdAt",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(proc.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--file", help="read issues from this JSON file instead of GitHub")
    parser.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    args = parser.parse_args(argv)
    try:
        if args.file:
            with open(args.file, encoding="utf-8") as f:
                issues = json.load(f)
        else:
            issues = fetch(args.repo)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"triage_report: cannot read the issues: {exc}", file=sys.stderr)
        return 2
    t = triage(issues)
    sys.stdout.write(format_json(t) if args.json else format_markdown(t))
    return 0


if __name__ == "__main__":
    sys.exit(main())
