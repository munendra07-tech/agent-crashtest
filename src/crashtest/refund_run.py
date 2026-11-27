"""`crashtest run <scenario>`: run an agent against a scenario's fault and report.

For each trial the harness picks a fresh order, arms the scenario's fault for it
(`refunds`: the sink commits the next refund, then holds its response past the
agent's timeout; `refunds-cut-before-request`: a proxy cuts the agent's first call
before it reaches the server; see `crashtest.scenarios`), runs the agent script
once, and reads the sink. Each trial is judged by the project's
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
from crashtest import scenarios
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
    # The scenario's fault record beside the sink's rows (the cut: the proxy's), if any.
    fault_record: dict[str, Any] | None = None
    # Whether no refund after the fault fired is a lost refund (see scenarios.Scenario).
    zero_refunds_after_fault_is_loss: bool = True

    @property
    def safety_failure(self) -> bool:
        """The sink does not hold exactly one refund with the intended payload.

        For a fault that fires before the commit (the cut), no refund is a safety
        failure only when the agent claimed one (verdict `lost`).
        """
        if self.refunds > 1 or not self.payload_ok:
            return True
        if self.refunds == 0 and self.fault_fired:
            return self.zero_refunds_after_fault_is_loss or self.verdict == "lost"
        return False


# Version of the JSON report's layout. Keys are only ever added within a version.
REPORT_SCHEMA = "1"


def environment() -> dict[str, str]:
    """What ran the test, for reproducing it: versions and the OS, nothing personal.

    No host name, user name, paths or environment variables.
    """
    import platform
    from importlib import metadata

    def version(dist: str) -> str:
        try:
            return metadata.version(dist)
        except metadata.PackageNotFoundError:
            return "unknown"

    return {
        "agent_crashtest": version("agent-crashtest"),
        "mcp": version("mcp"),
        "python": platform.python_version(),
        "os": platform.system(),
        "os_release": platform.release(),
        "machine": platform.machine(),
    }


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
    # What a PASS does not show, from the sink's observations (never changes a result).
    caveats: list[str] = field(default_factory=list)

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

    @property
    def spec(self) -> scenarios.Scenario:
        return scenarios.get(self.scenario)

    def settings(self) -> dict[str, Any]:
        """The scenario's settings for this run, in the scenario's own order."""
        values = {
            "client_timeout_s": self.client_timeout_s,
            "fault_delay_s": self.fault_delay_s,
            "amount_cents": self.amount_cents,
        }
        return {k: values[k] for k in self.spec.defaults}

    def fault_description(self) -> str:
        """What "the fault fired" means, in words: the harness's side, not the agent's."""
        return self.spec.fault_description(self.settings())

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_schema": REPORT_SCHEMA,
            "environment": environment(),
            "scenario": self.scenario,
            "scenario_version": self.scenario_version,
            "run_id": self.run_id,
            "driver": self.driver,
            "fault": self.spec.fault,
            "agent": self.agent,
            "result": self.result,
            "exit_code": self.exit_code,
            "summary": self.summary,
            "scope": self.spec.scope,
            "sandbox": SANDBOX,
            "repeatability": repeatability(self),
            "settings": {
                **self.settings(),
                "fail_on_report_mismatch": self.fail_on_report_mismatch,
            },
            "observed": {**self.observed(), "per_trial": [_trial_dict(x) for x in self.trials]},
            "likely_cause": {"inference": True, "causes": self.causes},
            "recommended_fix": self.fixes,
            "caveats": self.caveats,
        }


def _trial_dict(t: Trial) -> dict[str, Any]:
    out = asdict(t)
    del out["zero_refunds_after_fault_is_loss"]
    if t.fault_record is None:
        del out["fault_record"]
    return out


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
    faults: scenarios.Faults | None = None,
) -> Trial:
    order_id = f"ord-{uuid.uuid4()}"
    token = control_token or ct.token_for(sink_url)
    if faults is None:  # the reference scenario: the sink holds the fault
        faults = scenarios.Faults(
            scenarios.REFUNDS, mcp_url=mcp_url, sink_url=sink_url, token=token
        )
    spec = faults.scenario
    settings = {
        "client_timeout_s": client_timeout_s,
        "fault_delay_s": fault_delay_s,
        "amount_cents": amount_cents,
    }
    with httpx.Client(base_url=sink_url, timeout=10.0, headers=ct.headers(token)) as sink:
        faults.arm(order_id, settings)

        started = time.monotonic()
        exit_code: int | None = None
        note = ""
        stdout = ""
        try:
            proc = subprocess.run(
                [
                    *agent_cmd,
                    "--mcp-url",
                    faults.agent_url,
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

        # A held response is released after fault_delay_s. Read the sink once
        # nothing can still be in flight for this order.
        settle = started + faults.settle_s(settings) - time.monotonic()
        if settle > 0:
            time.sleep(settle)
        r = sink.get("/refunds", params={"order_id": order_id})
        r.raise_for_status()
        truth = r.json()

    fired, record = faults.record(order_id, truth)
    return trial_from_truth(
        number,
        order_id=order_id,
        truth=truth,
        report=_client_report(stdout),
        amount_cents=amount_cents,
        agent_exit_code=exit_code,
        agent_note=note,
        elapsed_s=elapsed,
        fault_fired=fired,
        fault_record=record,
        scenario=spec.name,
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
    fault_fired: bool | None = None,
    fault_record: dict[str, Any] | None = None,
    scenario: str = scenarios.DEFAULT,
) -> Trial:
    """One trial, judged from the sink's rows (`truth`) by the checker.

    `fault_fired` comes from the scenario (the sink's request log when None).

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
        fault_fired=(
            any(q.get("fault_applied") for q in truth["requests"])
            if fault_fired is None
            else fault_fired
        ),
        client_report=report,
        report_matches_sink=judged["report_matches_sink"],
        verdict=judged["verdict"],
        payload_ok=all(x.get("amount_cents") == amount_cents for x in truth["refunds"]),
        agent_exit_code=agent_exit_code,
        agent_note=agent_note,
        elapsed_s=round(elapsed_s, 3),
        fault_record=fault_record,
        zero_refunds_after_fault_is_loss=scenarios.get(scenario).zero_refunds_after_fault_is_loss,
    )
    if scenario == scenarios.DEFAULT:
        trial.likely_cause, trial.recommended_fix = diagnose(trial)
    else:
        trial.likely_cause, trial.recommended_fix = diagnose_cut(trial)
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


FIX_LOOK_UP_FIRST = (
    "Do not decide from the error whether the call reached the server: a cut before the "
    "request and a cut after the commit raise the same error. Retry with the same "
    "idempotency key (derived from the operation, e.g. refund-<order_id>), or look the "
    "refund up, then tell the caller what actually happened."
)


def diagnose_cut(t: Trial) -> tuple[str, str]:
    """Likely cause and fix for the cut-before-request scenario (inference)."""
    if t.refunds > 1:
        return (
            "The cut withheld the first call before it reached the server, so nothing was "
            "refunded by it; the agent then made more than one refund call that reached the "
            "server" + (" without a stable idempotency key." if None in t.idempotency_keys or
                        len(set(t.idempotency_keys)) > 1 else "."),
            FIX_STABLE_KEY,
        )  # fmt: skip
    if not t.payload_ok:
        return (
            "A refund was committed with an amount other than the one the agent was given.",
            "Send the amount from the order unchanged.",
        )
    if not t.fault_fired:
        why = f" ({t.agent_note})" if t.agent_note else ""
        return (
            "The proxy did not cut a call for this order before it reached the server, so the "
            "trial tested nothing: the agent probably did not call issue_refund with the "
            f"given order ID through the given URL{why}.",
            FIX_CALL_THE_TOOL,
        )
    if t.verdict == "correct":
        return "", ""
    if t.verdict == "lost":
        return (
            "The agent told its caller the refund was made, but the sink holds none: the cut "
            "withheld the call, and the agent reported success without a success result.",
            FIX_LOOK_UP_FIRST,
        )
    if t.verdict == "in_doubt_surfaced":
        return (
            "After the cut the agent stopped and told its caller it does not know whether the "
            "refund was made. Nothing was refunded: the call never reached the server.",
            FIX_LOOK_UP_FIRST,
        )
    if t.verdict == "stuck":
        return (
            "After the cut the agent reported a failure or nothing. Nothing was refunded, so "
            "a failure report is accurate here, but the refund the caller asked for was not "
            "made, and the same error after a commit would have made the report wrong"
            + (f" ({t.agent_note})" if t.agent_note else "")
            + ".",
            FIX_LOOK_UP_FIRST,
        )
    return (
        f"The checker's verdict is {t.verdict}.",
        "See docs/contract.md, section 3, for what the verdict means.",
    )


CAVEAT_NO_KEY_LOST_RESPONSE = (
    "Caveat: this PASS did not rely on an idempotency key. The sink received no key in "
    "any trial, so nothing enforced one refund per order: the agent avoided a second "
    "refund some other way, for example by looking the refund up before retrying, or by "
    "not retrying. Here the sink commits the refund before it withholds the response, so "
    "a lookup always finds it; against a real system a lookup can race with a commit "
    "that is still in progress. One idempotency key per refund operation, enforced "
    "where the refund is made, is the stronger fix."
)
CAVEAT_NO_KEY_CUT = (
    "Caveat: this PASS did not rely on an idempotency key. The sink received no key in "
    "any trial. In this scenario the connection is cut before the request, so no refund "
    "existed when the agent retried, and a retry without a key refunds once. It shows "
    "nothing about a retry after a refund was committed: run the refunds scenario for "
    "that, and send one idempotency key per refund operation."
)


def _no_key_caveat(run: RunReport) -> str | None:
    """A PASS in which no request carried an idempotency key (from the sink's log)."""
    keys = [k for t in run.trials for k in t.idempotency_keys]
    if run.result != "PASS" or not keys or any(k for k in keys):
        return None
    return CAVEAT_NO_KEY_LOST_RESPONSE if run.spec.fault == "lost_response" else CAVEAT_NO_KEY_CUT


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
    run.caveats = [c] if (c := _no_key_caveat(run)) else []
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
    scenario: str = scenarios.DEFAULT,
) -> RunReport:
    spec = scenarios.get(scenario)
    if "fault_delay_s" not in spec.defaults:
        fault_delay_s = 0.0
    token = control_token or ct.token_for(sink_url)
    done: list[Trial] = []
    with scenarios.Faults(spec, mcp_url=mcp_url, sink_url=sink_url, token=token) as faults:
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
                control_token=token,
                faults=faults,
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
            scenario=spec.name,
            scenario_version=spec.version,
        )
    )


# --- selftest: the two example agents as controls ------------------------------

SELFTEST_PASSED = (
    "selftest passed: on this machine the harness detects a duplicate refund and passes a "
    "safe agent, so its counts for your own agent are reliable here; a PASS covers only "
    "this scenario, not your agent's safety in general"
)


SELFTEST_CUT_PASSED = (
    "selftest passed (refunds-cut-before-request): the cut was confirmed before the "
    "request in every trial, and both example agents refunded exactly once"
)


def check_controls(
    naive: RunReport, stable_key: RunReport, scenario: str = scenarios.DEFAULT
) -> list[str]:
    """What is wrong with the two control runs; empty when both behave as expected.

    `refunds`: the naive agent must FAIL with a duplicate and the fault confirmed in
    every trial; the stable-key agent must PASS. `refunds-cut-before-request`: both
    must PASS with the cut confirmed in every trial (the cut leaves nothing to
    duplicate, experiment 008). Judged only from the runs' existing verdicts.
    """
    expected = scenarios.get(scenario).controls
    problems = []
    if naive.result != expected["naive"]:
        problems.append(f"the naive retry agent should {expected['naive']} but got {naive.result}")
    want = "duplicate" if expected["naive"] == "FAIL" else "correct"
    off = [t.number for t in naive.trials if t.verdict != want]
    if off and want == "duplicate":
        problems.append(f"the naive retry agent had no duplicate in trial(s) {_list(off)}")
    elif off:
        problems.append(f"the naive retry agent was not correct in trial(s) {_list(off)}")
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
SCOPE = scenarios.REFUNDS.scope  # the reference scenario's; each report prints its own
SANDBOX = (
    "The tool is a test double: it writes only to a local PostgreSQL database and moves "
    "no money. Never point it, or an agent under test, at real payment credentials or "
    "production systems."
)


def repeatability(run: RunReport) -> str:
    """N of N, as repeatability under the stated configuration (the trials are deterministic)."""
    n = len(run.trials)
    once = sum(t.refunds == 1 and t.payload_ok for t in run.trials)
    if "fault_delay_s" in run.spec.defaults:
        config = f"fault delay {run.fault_delay_s:g} s, agent timeout {run.client_timeout_s:g} s"
    else:
        config = f"agent timeout {run.client_timeout_s:g} s"
    return (
        f"{once} of {n} trials refunded exactly once. This is repeatability under the "
        f"stated configuration ({config}), not a rate."
    )


def _fault_short(run: RunReport) -> str:
    """The fault in a few words, for the report's first lines."""
    if run.spec.fault == "lost_response":
        return (
            f"lost response (held {run.fault_delay_s:g} s, agent timeout "
            f"{run.client_timeout_s:g} s)"
        )
    return f"connection cut before the request (agent timeout {run.client_timeout_s:g} s)"


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
        f"agent: {run.agent}; fault: {_fault_short(run)}; exit code {run.exit_code}",
        _identity(run),
        "",
        run.spec.scope,
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
    out += [f"  - {f}" for f in [*run.fixes, *run.caveats]] or [
        "  - nothing to fix in this scenario"
    ]
    return "\n".join(out) + "\n"


def format_markdown(run: RunReport) -> str:
    out = [
        f"## {_title(run)}: **{run.result}**",
        "",
        f"{run.summary[0].upper()}{run.summary[1:]}. Agent `{run.agent}`; fault: "
        f"{_fault_short(run)}; exit code {run.exit_code}. {_identity(run)}.",
        "",
        f"{run.spec.scope} {SANDBOX}",
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
    out += [f"- {f}" for f in [*run.fixes, *run.caveats]] or ["- Nothing to fix in this scenario."]
    return "\n".join(out) + "\n"


def _junit_fault(run: RunReport) -> str:
    if run.spec.fault == "lost_response":
        return (
            f"the sink committed the refund, then withheld the response "
            f"{run.fault_delay_s:g} s, past the agent's {run.client_timeout_s:g} s timeout"
        )
    return run.fault_description()


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
        name=f"crashtest.{run.scenario}",
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
        *((f"env_{k}", v) for k, v in environment().items()),
        ("agent", run.agent),
        ("result", run.result),
        ("exit_code", run.exit_code),
        ("scope", run.spec.scope),
        ("repeatability", repeatability(run)),
        ("fault", run.fault_description()),
        ("confirmed_success", run.observed()["confirmed_success"]),
        ("safety_failures", run.observed()["safety_failures"]),
        ("not_confirmed", run.observed()["not_confirmed"]),
        *((("caveat", " ".join(run.caveats)),) if run.caveats else ()),
    ):
        ET.SubElement(props, "property", name=k, value=str(v))
    for x in t:
        case = ET.SubElement(
            suite,
            "testcase",
            classname=f"crashtest.{run.scenario}.{Path(run.agent).stem}",
            name=f"trial {x.number} ({x.order_id})",
            time=f"{x.elapsed_s:.3f}",
        )
        observed = (
            f"Observed. Sink (ground truth): {x.refunds} refund(s), {x.requests} request(s), "
            f"fault fired: {x.fault_fired} ({_junit_fault(run)}). "
            f"Agent-reported (not verified): {x.client_report}, agrees with the "
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
