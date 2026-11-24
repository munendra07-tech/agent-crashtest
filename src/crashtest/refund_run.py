"""`crashtest run refunds`: run an agent against the lost-response fault and report.

For each trial the harness picks a fresh order, arms the fault for it (the sink
commits the next refund, then holds its response past the agent's timeout), runs
the agent script once, and reads the sink. Each trial is judged by the project's
checker (`crashtest.cli.judge`, which calls `crashtest.checker.classify`); this
module only adds up the verdicts and writes the report.

The report has three parts, kept apart: what was observed (the sink's rows and
what the harness saw), the likely cause (an inference from those facts), and the
recommended fix.

An agent is any script with this contract:
  arguments  --mcp-url URL --order-id ID --amount-cents N --timeout SECONDS
  stdout     the last line is one JSON object whose "report" is what the agent
             told its caller about the refund: "done", "unknown" or "not_done".
             Anything else, or no such line, counts as "none".
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal
from xml.etree import ElementTree as ET

import httpx

from crashtest import control_token as ct
from crashtest.cli import judge

# The two example agents, by the names `--agent` accepts.
BUILTIN_AGENTS = {
    "naive": "naive_retry_agent.py",
    "stable_key": "stable_key_agent.py",
}
REPORTS = ("done", "unknown", "not_done")

Result = Literal["PASS", "FAIL", "WARN", "INCONCLUSIVE"]

# Exit codes.
EXIT_PASS = 0
EXIT_SAFETY = 1  # a duplicate or lost refund (or, if asked, a report that contradicts the sink)
EXIT_ENVIRONMENT = 2  # the stack, the sink or the agent script could not be used
EXIT_FAULT_NOT_CONFIRMED = 3  # the fault never fired, so the trial tested nothing


class SetupError(Exception):
    """An environment or configuration problem: exit code 2."""


def find_builtin_agent(name: str) -> Path:
    """The example agent's file: in the installed package, or in a source checkout."""
    import crashtest

    filename = BUILTIN_AGENTS[name]
    package = Path(crashtest.__file__).resolve().parent
    for candidate in (package / "examples" / filename, package.parents[1] / "examples" / filename):
        if candidate.is_file():
            return candidate
    raise SetupError(f"the example agent {filename} is not installed with this package")


def resolve_agent(agent: str) -> Path:
    if agent in BUILTIN_AGENTS:
        return find_builtin_agent(agent)
    path = Path(agent)
    if not path.is_file():
        raise SetupError(
            f"--agent {agent!r} is neither {' nor '.join(BUILTIN_AGENTS)} nor a script file"
        )
    return path.resolve()


@dataclass
class Trial:
    number: int
    order_id: str
    refunds: int
    requests: int
    idempotency_keys: list[str | None]
    fault_fired: bool
    client_report: str
    report_matches_sink: bool
    verdict: str
    payload_ok: bool
    agent_exit_code: int | None
    agent_note: str
    elapsed_s: float
    likely_cause: str = ""
    recommended_fix: str = ""

    @property
    def safety_failure(self) -> bool:
        """The sink does not hold exactly one refund with the intended payload."""
        if self.refunds > 1 or not self.payload_ok:
            return True
        return self.refunds == 0 and self.fault_fired


@dataclass
class RunReport:
    agent: str
    trials: list[Trial]
    client_timeout_s: float
    fault_delay_s: float
    amount_cents: int
    fail_on_report_mismatch: bool = False
    # Which run this is and what drove it: `crashtest run` or the test controller.
    run_id: str = field(default_factory=lambda: f"run-{uuid.uuid4()}")
    scenario: str = "refunds"
    scenario_version: str = "1"
    driver: Literal["run", "controller"] = "run"
    result: Result = "PASS"
    exit_code: int = EXIT_PASS
    summary: str = ""
    causes: list[str] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)

    def observed(self) -> dict[str, Any]:
        t = self.trials
        return {
            "trials": len(t),
            "expected_refunds": len(t),
            "actual_refunds": sum(x.refunds for x in t),
            "duplicates_per_trial": [max(x.refunds - 1, 0) for x in t],
            "requests_received": sum(x.requests for x in t),
            "client_reports": dict(Counter(x.client_report for x in t)),
            "reports_agreeing_with_sink": sum(x.report_matches_sink for x in t),
            "faults_fired": sum(x.fault_fired for x in t),
            "fault_description": self.fault_description(),
            "confirmed_success": sum(x.verdict == "correct" and x.fault_fired for x in t),
            "safety_failures": sum(x.safety_failure for x in t),
            "not_confirmed": sum(not x.safety_failure and not _confirmed(x) for x in t),
            "verdicts": dict(Counter(x.verdict for x in t)),
        }

    def fault_description(self) -> str:
        """What "the fault fired" means, in words: the sink's side, not the agent's."""
        return (
            f"the sink committed the refund, then withheld its response for "
            f"{self.fault_delay_s:g} s, past the agent's {self.client_timeout_s:g} s timeout "
            "(the harness does not observe the agent's side of the call)"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "scenario_version": self.scenario_version,
            "run_id": self.run_id,
            "driver": self.driver,
            "fault": "lost_response",
            "agent": self.agent,
            "result": self.result,
            "exit_code": self.exit_code,
            "summary": self.summary,
            "scope": SCOPE,
            "sandbox": SANDBOX,
            "repeatability": repeatability(self),
            "settings": {
                "client_timeout_s": self.client_timeout_s,
                "fault_delay_s": self.fault_delay_s,
                "amount_cents": self.amount_cents,
                "fail_on_report_mismatch": self.fail_on_report_mismatch,
            },
            "observed": {**self.observed(), "per_trial": [asdict(x) for x in self.trials]},
            "likely_cause": {"inference": True, "causes": self.causes},
            "recommended_fix": self.fixes,
        }


# --- one trial ------------------------------------------------------------------


def _client_report(stdout: str) -> str:
    for line in reversed(stdout.strip().splitlines()):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            report = obj.get("report")
            return report if report in REPORTS else "none"
        break
    return "none"


def _tail(text: str, limit: int = 300) -> str:
    text = text.strip()
    return text if len(text) <= limit else "..." + text[-limit:]


def run_trial(
    number: int,
    *,
    agent_cmd: list[str],
    mcp_url: str,
    sink_url: str,
    client_timeout_s: float,
    fault_delay_s: float,
    amount_cents: int,
    agent_timeout_s: float,
    control_token: str | None = None,
) -> Trial:
    order_id = f"ord-{uuid.uuid4()}"
    token = control_token or ct.token_for(sink_url)
    with httpx.Client(base_url=sink_url, timeout=10.0, headers=ct.headers(token)) as sink:
        sink.put(
            "/control/faults", json={"order_id": order_id, "delay_s": fault_delay_s, "times": 1}
        ).raise_for_status()

        started = time.monotonic()
        exit_code: int | None = None
        note = ""
        stdout = ""
        try:
            proc = subprocess.run(
                [
                    *agent_cmd,
                    "--mcp-url",
                    mcp_url,
                    "--order-id",
                    order_id,
                    "--amount-cents",
                    str(amount_cents),
                    "--timeout",
                    f"{client_timeout_s:g}",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=agent_timeout_s,
                # The agent under test never gets the sink's control token.
                env=ct.agent_env(),
            )
            exit_code, stdout = proc.returncode, proc.stdout
            if proc.returncode != 0:
                note = f"agent exited with code {proc.returncode}: {_tail(proc.stderr)}"
        except subprocess.TimeoutExpired:
            note = f"agent did not finish within {agent_timeout_s:g} s and was stopped"
        except OSError as exc:
            raise SetupError(f"cannot start the agent ({' '.join(agent_cmd)}): {exc}") from exc
        elapsed = time.monotonic() - started

        # The held response is released after fault_delay_s. Read the sink once
        # nothing can still be in flight for this order.
        settle = started + fault_delay_s + 0.5 - time.monotonic()
        if settle > 0:
            time.sleep(settle)
        r = sink.get("/refunds", params={"order_id": order_id})
        r.raise_for_status()
        truth = r.json()

    return trial_from_truth(
        number,
        order_id=order_id,
        truth=truth,
        report=_client_report(stdout),
        amount_cents=amount_cents,
        agent_exit_code=exit_code,
        agent_note=note,
        elapsed_s=elapsed,
    )


def trial_from_truth(
    number: int,
    *,
    order_id: str,
    truth: dict,
    report: str,
    amount_cents: int,
    agent_exit_code: int | None,
    agent_note: str,
    elapsed_s: float,
) -> Trial:
    """One trial, judged from the sink's rows (`truth`) by the checker.

    `report` is what the agent told its caller; it is recorded beside the verdict and
    used only as the checker allows (it never turns a duplicate into anything else).
    """
    judged = judge(truth, order_id=order_id, amount_cents=amount_cents, report=report)  # type: ignore[arg-type]
    trial = Trial(
        number=number,
        order_id=order_id,
        refunds=judged["refunds"],
        requests=judged["requests"],
        idempotency_keys=[q.get("idempotency_key") for q in truth["requests"]],
        fault_fired=any(q.get("fault_applied") for q in truth["requests"]),
        client_report=report,
        report_matches_sink=judged["report_matches_sink"],
        verdict=judged["verdict"],
        payload_ok=all(x.get("amount_cents") == amount_cents for x in truth["refunds"]),
        agent_exit_code=agent_exit_code,
        agent_note=agent_note,
        elapsed_s=round(elapsed_s, 3),
    )
    trial.likely_cause, trial.recommended_fix = diagnose(trial)
    return trial


# --- diagnosis: the inference, kept apart from the facts -----------------------

FIX_STABLE_KEY = (
    "Derive one idempotency key from the business operation (for example "
    "refund-<order_id>), send the same key on every attempt, and let the system that "
    "makes the refund enforce it (here the sink does, in its database). A key made "
    "fresh for each attempt does not help. Before retrying after a timeout, check "
    "whether the refund already exists."
)
FIX_DEFINITE_ANSWER = (
    "Treat a timeout as 'unknown', not as 'failed': retry with the same idempotency "
    "key, or look the refund up, so the caller gets a definite answer."
)
FIX_CALL_THE_TOOL = (
    "Make sure the agent calls issue_refund with the --order-id and --amount-cents it "
    "is given, and with a timeout shorter than the fault delay; run it by hand against "
    "`crashtest serve` to see what it does."
)


def diagnose(t: Trial) -> tuple[str, str]:
    """The likely cause and the recommended fix for one trial (inference)."""
    if t.refunds > 1:
        keys = t.idempotency_keys
        if all(k is None for k in keys):
            how = "retried without an idempotency key"
        elif len(set(keys)) > 1:
            how = "retried with a different idempotency key, so the sink saw a new refund"
        else:
            how = "repeated the call, and the sink did not deduplicate it"
        cause = (
            "The first request committed and its response was lost (the fault held it "
            f"past the agent's timeout); the agent {how}."
        )
        if t.client_report == "done":
            cause += " It then told its caller that one refund was made."
        return cause, FIX_STABLE_KEY
    if not t.payload_ok:
        return (
            "A refund was committed with an amount other than the one the agent was given.",
            "Send the amount from the order unchanged.",
        )
    if not t.fault_fired:
        why = f" ({t.agent_note})" if t.agent_note else ""
        return (
            "No refund for this order reached the sink while the fault was armed, so the "
            f"trial tested nothing: the agent probably did not call issue_refund with the "
            f"given order ID{why}.",
            FIX_CALL_THE_TOOL,
        )
    if t.verdict == "correct":
        return "", ""
    if t.verdict == "in_doubt_surfaced":
        return (
            "After the lost response the agent stopped and told its caller it does not "
            "know whether the refund was made. The refund was made once.",
            FIX_DEFINITE_ANSWER,
        )
    if t.verdict == "stuck":
        return (
            "The refund was made once, but the agent reported a failure or nothing: it "
            "probably treated the lost response as a definite failure, or gave up"
            + (f" ({t.agent_note})" if t.agent_note else "")
            + ".",
            FIX_DEFINITE_ANSWER,
        )
    return (
        f"The checker's verdict is {t.verdict}.",
        "See docs/contract.md, section 3, for what the verdict means.",
    )


def summarise(run: RunReport) -> RunReport:
    """Decide the result and the exit code, and collect the causes and fixes."""
    t = run.trials
    for trial in t:
        if trial.likely_cause and trial.likely_cause not in run.causes:
            run.causes.append(trial.likely_cause)
        if trial.recommended_fix and trial.recommended_fix not in run.fixes:
            run.fixes.append(trial.recommended_fix)
    safety = [x.number for x in t if x.safety_failure]
    unfired = [x.number for x in t if not x.fault_fired]
    mismatched = [x.number for x in t if not x.report_matches_sink]
    if safety:
        run.result, run.exit_code = "FAIL", EXIT_SAFETY
        dup = sum(x.refunds > 1 for x in t)
        run.summary = (
            f"{dup} of {len(t)} trials refunded more than once"
            if dup
            else f"{len(safety)} of {len(t)} trials did not hold exactly one correct refund"
        )
    elif unfired:
        run.result, run.exit_code = "INCONCLUSIVE", EXIT_FAULT_NOT_CONFIRMED
        run.summary = f"the fault did not fire in trial(s) {_list(unfired)}, so they tested nothing"
    elif all(x.verdict == "correct" for x in t):
        run.result, run.exit_code = "PASS", EXIT_PASS
        run.summary = f"{len(t)} of {len(t)} trials refunded exactly once (verdict correct)"
    else:
        run.result = "WARN"
        run.exit_code = EXIT_SAFETY if (mismatched and run.fail_on_report_mismatch) else EXIT_PASS
        run.summary = (
            "the sink holds exactly one refund per order, but not every verdict is correct "
            f"({_counts(Counter(x.verdict for x in t))})"
        )
        if mismatched:
            run.summary += (
                f"; in trial(s) {_list(mismatched)} what the agent said contradicts the sink"
            )
    return run


def _list(numbers: list[int]) -> str:
    return ", ".join(str(n) for n in numbers)


def _counts(c: Counter[str]) -> str:
    return ", ".join(f"{k} {v}" for k, v in c.most_common())


def run_refunds(
    *,
    agent: str,
    agent_cmd: list[str],
    mcp_url: str,
    sink_url: str,
    trials: int,
    client_timeout_s: float = 2.0,
    fault_delay_s: float = 5.0,
    amount_cents: int = 2500,
    agent_timeout_s: float = 60.0,
    fail_on_report_mismatch: bool = False,
    progress: bool = True,
    control_token: str | None = None,
) -> RunReport:
    done: list[Trial] = []
    for n in range(1, trials + 1):
        trial = run_trial(
            n,
            agent_cmd=agent_cmd,
            mcp_url=mcp_url,
            sink_url=sink_url,
            client_timeout_s=client_timeout_s,
            fault_delay_s=fault_delay_s,
            amount_cents=amount_cents,
            agent_timeout_s=agent_timeout_s,
            control_token=control_token,
        )
        done.append(trial)
        if progress:
            print(
                f"trial {n}/{trials}: {trial.refunds} refund(s), agent said "
                f"{trial.client_report}, verdict {trial.verdict}",
                file=sys.stderr,
                flush=True,
            )
    return summarise(
        RunReport(
            agent=agent,
            trials=done,
            client_timeout_s=client_timeout_s,
            fault_delay_s=fault_delay_s,
            amount_cents=amount_cents,
            fail_on_report_mismatch=fail_on_report_mismatch,
        )
    )


# --- selftest: the two example agents as controls ------------------------------

SELFTEST_PASSED = (
    "selftest passed: the harness detects a duplicate refund and passes a safe agent; "
    "your own agent's result can be trusted on this machine"
)


def check_controls(naive: RunReport, stable_key: RunReport) -> list[str]:
    """What is wrong with the two control runs; empty when both behave as expected.

    The naive agent must FAIL with a duplicate and the fault confirmed in every trial;
    the stable-key agent must PASS. Judged only from the runs' existing verdicts.
    """
    problems = []
    if naive.result != "FAIL":
        problems.append(f"the naive retry agent should FAIL but got {naive.result}")
    no_dup = [t.number for t in naive.trials if t.verdict != "duplicate"]
    if no_dup:
        problems.append(f"the naive retry agent had no duplicate in trial(s) {_list(no_dup)}")
    for name, run in (("naive retry", naive), ("stable-key", stable_key)):
        unfired = [t.number for t in run.trials if not t.fault_fired]
        if unfired:
            problems.append(
                f"the fault was not confirmed for the {name} agent in trial(s) {_list(unfired)}"
            )
    if stable_key.result != "PASS":
        problems.append(
            f"the stable-key agent should PASS but got {stable_key.result} ({stable_key.summary})"
        )
    return problems


# --- formats ---------------------------------------------------------------------

# What a run tests, and what it does not. Printed with every report.
SCOPE = (
    "What was tested: one simulated refund tool (issue_refund) over MCP and one fault, a "
    "lost response: the sink commits the refund, then withholds the reply past the "
    "agent's timeout. No other effect or fault is tested."
)
SANDBOX = (
    "The tool is a test double: it writes only to a local PostgreSQL database and moves "
    "no money. Never point it, or an agent under test, at real payment credentials or "
    "production systems."
)


def repeatability(run: RunReport) -> str:
    """N of N, as repeatability under the stated configuration (the trials are deterministic)."""
    n = len(run.trials)
    once = sum(t.refunds == 1 and t.payload_ok for t in run.trials)
    return (
        f"{once} of {n} trials refunded exactly once. This is repeatability under the "
        f"stated configuration (fault delay {run.fault_delay_s:g} s, agent timeout "
        f"{run.client_timeout_s:g} s), not a rate."
    )


COLUMNS = (
    "trial",
    "order",
    "refunds",
    "requests",
    "fault fired",
    "agent said",
    "agrees with sink",
    "verdict",
)


def _confirmed(t: Trial) -> bool:
    """Confirmed success: one correct refund in the sink, the agent said so, the fault fired."""
    return t.verdict == "correct" and t.fault_fired


def _row(t: Trial) -> list[str]:
    return [
        str(t.number),
        t.order_id,
        str(t.refunds),
        str(t.requests),
        "yes" if t.fault_fired else "NO",
        t.client_report,
        "yes" if t.report_matches_sink else "NO",
        t.verdict,
    ]


def _observed_lines(run: RunReport) -> list[str]:
    """The Observed facts, each tagged with its source: the sink, the agent, or the checker."""
    o = run.observed()
    n = o["trials"]
    return [
        f"trials: {n}",
        f"sink (ground truth): expected refunds {o['expected_refunds']}, actual refunds "
        f"(in the sink): {o['actual_refunds']}",
        f"sink (ground truth): extra refunds per trial: "
        f"{', '.join(map(str, o['duplicates_per_trial']))}",
        f"sink (ground truth): refund requests received: {o['requests_received']}",
        f"sink (ground truth): fault fired in {o['faults_fired']} of {n} trials: "
        f"{o['fault_description']}",
        f"agent-reported (not verified): what the agent told its caller: "
        f"{_counts(Counter(o['client_reports']))}",
        f"agent-reported (not verified): agrees with the sink in "
        f"{o['reports_agreeing_with_sink']} of {n}",
        f"checker: confirmed success (one correct refund in the sink, the agent said done, "
        f"the fault fired): {o['confirmed_success']} of {n}",
        f"checker: safety failures (a duplicate, a wrong amount, or a refund lost after the "
        f"fault fired): {o['safety_failures']} of {n}",
        f"checker: not confirmed (no safety failure, but the agent's answer was uncertain or "
        f"contradicted the sink, or the fault did not fire): {o['not_confirmed']} of {n}",
        f"checker: verdicts (crashtest.checker): {_counts(Counter(o['verdicts']))}",
    ]


def _notes(run: RunReport) -> list[str]:
    return [f"trial {t.number}: {t.agent_note}" for t in run.trials if t.agent_note]


def _title(run: RunReport) -> str:
    return f"crashtest {'run' if run.driver == 'run' else 'controller'} {run.scenario}"


def _identity(run: RunReport) -> str:
    how = "crashtest run" if run.driver == "run" else "the test controller"
    return f"run {run.run_id}; scenario {run.scenario} version {run.scenario_version}; via {how}"


def format_text(run: RunReport) -> str:
    rows = [list(COLUMNS), *(_row(t) for t in run.trials)]
    widths = [max(len(r[i]) for r in rows) for i in range(len(COLUMNS))]
    table = [
        ("  " + "  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True))).rstrip()
        for r in rows
    ]
    out = [
        f"{_title(run)}: {run.result} - {run.summary}",
        f"agent: {run.agent}; fault: lost response (held {run.fault_delay_s:g} s, "
        f"agent timeout {run.client_timeout_s:g} s); exit code {run.exit_code}",
        _identity(run),
        "",
        SCOPE,
        SANDBOX,
        "",
        "OBSERVED (from the sink and the harness)",
        *("  " + line for line in _observed_lines(run)),
        "  " + repeatability(run),
        "",
        *table,
    ]
    if notes := _notes(run):
        out += ["", *("  " + n for n in notes)]
    out += ["", "LIKELY CAUSE (inference from the observations above, not observed directly)"]
    out += [f"  - {c}" for c in run.causes] or ["  - none: every trial refunded exactly once"]
    out += ["", "RECOMMENDED FIX"]
    out += [f"  - {f}" for f in run.fixes] or ["  - nothing to fix in this scenario"]
    return "\n".join(out) + "\n"


def format_markdown(run: RunReport) -> str:
    out = [
        f"## {_title(run)}: **{run.result}**",
        "",
        f"{run.summary[0].upper()}{run.summary[1:]}. Agent `{run.agent}`; fault: lost "
        f"response (held {run.fault_delay_s:g} s, agent timeout {run.client_timeout_s:g} s); "
        f"exit code {run.exit_code}. {_identity(run)}.",
        "",
        f"{SCOPE} {SANDBOX}",
        "",
        "### Observed (from the sink and the harness)",
        "",
        *(f"- {line}" for line in _observed_lines(run)),
        f"- {repeatability(run)}",
        "",
        "| " + " | ".join(COLUMNS) + " |",
        "|" + "---|" * len(COLUMNS),
        *(
            "| " + " | ".join(f"`{c}`" if i == 1 else c for i, c in enumerate(_row(t))) + " |"
            for t in run.trials
        ),
    ]
    if notes := _notes(run):
        out += ["", *(f"- {n}" for n in notes)]
    out += ["", "### Likely cause (inference, not observed directly)", ""]
    out += [f"- {c}" for c in run.causes] or ["- None: every trial refunded exactly once."]
    out += ["", "### Recommended fix", ""]
    out += [f"- {f}" for f in run.fixes] or ["- Nothing to fix in this scenario."]
    return "\n".join(out) + "\n"


def format_junit(run: RunReport) -> str:
    t = run.trials
    failing = [
        x
        for x in t
        if x.safety_failure
        or not x.fault_fired
        or (run.fail_on_report_mismatch and not x.report_matches_sink)
    ]
    warned = [x for x in t if x not in failing and x.verdict != "correct"]
    suite = ET.Element(
        "testsuite",
        name="crashtest.refunds",
        tests=str(len(t)),
        failures=str(len(failing)),
        errors="0",
        skipped="0",
        time=f"{sum(x.elapsed_s for x in t):.3f}",
    )
    props = ET.SubElement(suite, "properties")
    for k, v in (
        ("run_id", run.run_id),
        ("scenario", run.scenario),
        ("scenario_version", run.scenario_version),
        ("driver", run.driver),
        ("agent", run.agent),
        ("result", run.result),
        ("exit_code", run.exit_code),
        ("scope", SCOPE),
        ("repeatability", repeatability(run)),
        ("fault", run.fault_description()),
        ("confirmed_success", run.observed()["confirmed_success"]),
        ("safety_failures", run.observed()["safety_failures"]),
        ("not_confirmed", run.observed()["not_confirmed"]),
    ):
        ET.SubElement(props, "property", name=k, value=str(v))
    for x in t:
        case = ET.SubElement(
            suite,
            "testcase",
            classname=f"crashtest.refunds.{Path(run.agent).stem}",
            name=f"trial {x.number} ({x.order_id})",
            time=f"{x.elapsed_s:.3f}",
        )
        observed = (
            f"Observed. Sink (ground truth): {x.refunds} refund(s), {x.requests} request(s), "
            f"fault fired: {x.fault_fired} (the sink committed the refund, then withheld the "
            f"response {run.fault_delay_s:g} s, past the agent's {run.client_timeout_s:g} s "
            f"timeout). Agent-reported (not verified): {x.client_report}, agrees with the "
            f"sink: {x.report_matches_sink}. Checker: verdict {x.verdict}, confirmed "
            f"success: {_confirmed(x)}." + (f" {x.agent_note}." if x.agent_note else "")
        )
        detail = (
            f"{observed}\nLikely cause (inference): {x.likely_cause}\n"
            f"Recommended fix: {x.recommended_fix}"
        )
        if x in failing:
            kind = "duplicate" if x.refunds > 1 else x.verdict
            if not x.safety_failure:
                kind = "fault_not_confirmed" if not x.fault_fired else "report_mismatch"
            ET.SubElement(case, "failure", message=f"{kind}: {observed}", type=kind).text = detail
        elif x in warned:
            ET.SubElement(case, "system-out").text = detail
    ET.indent(suite)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(suite, encoding="unicode") + "\n"
    )


def format_json(run: RunReport) -> str:
    return json.dumps(run.to_dict(), indent=2) + "\n"


FORMATS = {
    "text": format_text,
    "json": format_json,
    "markdown": format_markdown,
    "junit": format_junit,
}
