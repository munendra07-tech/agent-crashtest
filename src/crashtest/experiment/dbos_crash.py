"""Kill a pydantic-ai + DBOS agent at a chosen point, restart it, and count the refunds.

The subject (subjects/pydantic_ai_dbos_refund_agent.py) runs as a separate process
in its own Python environment. A trial is:

  1. start the agent inside a DBOS workflow with a fresh workflow id;
  2. hard-kill the process at the kill point;
  3. start it again. The restarted process only launches DBOS and waits: DBOS
     decides what to run again. The harness makes no resubmit decision;
  4. read from the sink how many refunds exist for the order.

Kill points:
  none         no kill; the control
  during_tool  the sink has committed the refund but is withholding its response
  after_tool   the tool has returned and the agent is entering its next model call

Not part of "all" (the plan's secondary question, run separately):
  late_response  no kill; the sink withholds its response for longer than the
                 tool's HTTP timeout, and the DBOS step is allowed to retry

Cells are run in a seeded random order.

    python -m crashtest.experiment.dbos_crash --local \
        --python D:/envs/pydanticai/Scripts/python.exe
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from crashtest import control_token
from crashtest.checker import (
    VERDICTS,
    SubjectStatus,
    classify,
    payload_matches,
    report_matches_sink,
)
from crashtest.experiment.crash_common import (
    finish,
    provenance,
    shuffled_cells,
    spawn,
    wait_for_marker,
    wait_for_refund,
)
from crashtest.experiment.single_call import SUBJECTS_DIR

SCRIPT = SUBJECTS_DIR / "pydantic_ai_dbos_refund_agent.py"
REPO = SUBJECTS_DIR.parent
TOOL_MODES = ("plain", "step", "child_workflow")
KEY_STRATEGIES = ("none", "tool_call_id", "workflow_id", "business")
KILL_POINTS = ("none", "during_tool", "after_tool")
AMOUNT_CENTS = 2500  # the refund the subject is asked to make
EXTRA_POINTS = ("late_response",)  # selectable by name, never part of "all"
LATE_RESPONSE_ARGS = ("--http-timeout", "2", "--step-retries")
LATE_RESPONSE_HOLD_S = 5.0

COLUMNS = (
    "kill_point",
    "tool_mode",
    "key_strategy",
    "trials",
    "refunded_once",
    "refunded_twice",
    "not_refunded",
    "verdicts",
    "fault_confirmed",
    "report_mismatch",
    "requests_at_sink",
    "tool_calls_on_recovery",
    "distinct_keys",
)


@dataclass
class DbosCrashTrial:
    kill_point: str
    tool_mode: str
    key_strategy: str
    sequence: int  # position in the randomised run order
    order_id: str
    workflow_id: str
    killed: bool
    effects_committed: int
    requests_received: int
    verdict: str
    fault_confirmed: bool | None = None
    # What the subject told its caller agrees with the sink (contract v0.3, RA).
    report_matches_sink: bool | None = None
    keys_at_sink: list[str | None] = field(default_factory=list)
    first_run: dict = field(default_factory=dict)
    resume: dict = field(default_factory=dict)
    client: str = ""


async def run_trial(
    *,
    python: str,
    system_database_url: str,
    sink_url: str,
    kill_point: str,
    tool_mode: str,
    key_strategy: str,
    sequence: int = 0,
    hold_response_s: float = 20.0,
    step_limit_s: float = 90.0,
    settle_s: float = 0.3,
) -> DbosCrashTrial:
    order_id = f"ord-{uuid.uuid4()}"
    workflow_id = f"wf-{uuid.uuid4()}"
    common = [
        "--system-database-url",
        system_database_url,
        "--sink-url",
        sink_url,
        "--order-id",
        order_id,
        "--amount-cents",
        str(AMOUNT_CENTS),
        "--workflow-id",
        workflow_id,
        # One DBOS executor per trial: a process recovers only its own trial's
        # workflow, never one left pending by an earlier trial.
        "--executor-id",
        f"exec-{workflow_id}",
        "--tool-mode",
        tool_mode,
        "--key-strategy",
        key_strategy,
    ]
    killed = False
    first_run: dict = {}
    resume: dict = {}

    async with httpx.AsyncClient(
        base_url=sink_url, timeout=30.0, headers=control_token.control_headers(sink_url)
    ) as sink:
        if kill_point == "none":
            first_run = await finish(await spawn(python, SCRIPT, *common), step_limit_s)
        elif kill_point == "late_response":
            r = await sink.put(
                "/control/faults",
                json={"order_id": order_id, "delay_s": LATE_RESPONSE_HOLD_S, "times": 1},
            )
            r.raise_for_status()
            first_run = await finish(
                await spawn(python, SCRIPT, *common, *LATE_RESPONSE_ARGS), step_limit_s
            )
        else:
            if kill_point == "during_tool":
                r = await sink.put(
                    "/control/faults",
                    json={"order_id": order_id, "delay_s": hold_response_s, "times": 1},
                )
                r.raise_for_status()
                proc = await spawn(python, SCRIPT, *common)
                reached = await wait_for_refund(sink, order_id, step_limit_s)
            else:
                proc = await spawn(python, SCRIPT, *common, "--hold-at", "model2")
                reached = await wait_for_marker(proc, "AT_HOLD", step_limit_s)
            proc.kill()  # hard kill: no handlers run, nothing is flushed
            _, stderr = await proc.communicate()
            killed = True
            first_run = {"outcome": "killed", "reached_kill_point": reached}
            if not reached:
                first_run["stderr_tail"] = stderr.decode(errors="replace")[-500:]

            resume = await finish(await spawn(python, SCRIPT, *common, "--resume"), step_limit_s)

        await asyncio.sleep(settle_s)
        r = await sink.get("/refunds", params={"order_id": order_id})
        r.raise_for_status()
        truth = r.json()

    effects = len(truth["refunds"])
    last = resume if killed else first_run
    status: SubjectStatus = (
        "completed" if last.get("outcome") == "ok" and last.get("answered") else "failed"
    )
    # A subject that did not answer told its caller nothing ("none").
    reported_ok = report_matches_sink(
        effects=effects, intended=1, report="done" if status == "completed" else "none"
    )
    fault_confirmed: bool | None = None
    if kill_point in ("during_tool", "late_response"):
        fault_confirmed = any(q.get("fault_applied") for q in truth["requests"])
    return DbosCrashTrial(
        kill_point=kill_point,
        tool_mode=tool_mode,
        key_strategy=key_strategy,
        sequence=sequence,
        order_id=order_id,
        workflow_id=workflow_id,
        killed=killed,
        effects_committed=effects,
        requests_received=len(truth["requests"]),
        verdict=classify(
            effects=effects,
            intended=1,
            status=status,
            payload_matches=payload_matches(
                truth["refunds"], order_id=order_id, amount_cents=AMOUNT_CENTS
            ),
            # The scripted model answers only after the tool has returned a result.
            confirmed=status == "completed",
        ),
        fault_confirmed=fault_confirmed,
        report_matches_sink=reported_ok,
        keys_at_sink=[q.get("idempotency_key") for q in truth["requests"]],
        first_run=first_run,
        resume=resume,
        client=str(last.get("client", "")),
    )


def summarise(trials: list[DbosCrashTrial]) -> list[dict]:
    rows = []
    for kill_point in KILL_POINTS + EXTRA_POINTS:
        for tool_mode in TOOL_MODES:
            for key_strategy in KEY_STRATEGIES:
                cell = [
                    t
                    for t in trials
                    if (t.kill_point, t.tool_mode, t.key_strategy)
                    == (kill_point, tool_mode, key_strategy)
                ]
                if not cell:
                    continue
                recovered = Counter(t.resume.get("tool_calls", "-") for t in cell)
                rows.append(
                    {
                        "kill_point": kill_point,
                        "tool_mode": tool_mode,
                        "key_strategy": key_strategy,
                        "trials": len(cell),
                        "refunded_once": sum(t.effects_committed == 1 for t in cell),
                        "refunded_twice": sum(t.effects_committed > 1 for t in cell),
                        "not_refunded": sum(t.effects_committed == 0 for t in cell),
                        "verdicts": ", ".join(
                            f"{v} x{n}"
                            for v in VERDICTS
                            if (n := sum(t.verdict == v for t in cell))
                        ),
                        "fault_confirmed": (
                            "-"
                            if all(t.fault_confirmed is None for t in cell)
                            else sum(bool(t.fault_confirmed) for t in cell)
                        ),
                        # Trials whose report contradicts the sink (contract v0.3, RA).
                        "report_mismatch": sum(t.report_matches_sink is False for t in cell),
                        "requests_at_sink": sum(t.requests_received for t in cell),
                        # How many times the tool body ran in the restarted process.
                        "tool_calls_on_recovery": ", ".join(
                            f"{k} x{v}" for k, v in sorted(recovered.items(), key=str)
                        ),
                        # Trials whose requests carried more than one distinct key:
                        # the key was not stable across recovery.
                        "distinct_keys": sum(
                            len({k for k in t.keys_at_sink if k}) > 1 for t in cell
                        ),
                    }
                )
    return rows


def format_table(rows: list[dict]) -> str:
    lines = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    lines += ["| " + " | ".join(str(r[c]) for c in COLUMNS) + " |" for r in rows]
    return "\n".join(lines)


async def run_all(
    *,
    python: str,
    system_database_url: str,
    sink_url: str,
    kill_points: tuple[str, ...],
    tool_modes: tuple[str, ...],
    key_strategies: tuple[str, ...],
    trials_per_cell: int,
    seed: int,
) -> list[DbosCrashTrial]:
    cells = shuffled_cells((kill_points, tool_modes, key_strategies), trials_per_cell, seed)
    trials: list[DbosCrashTrial] = []
    for sequence, (kill_point, tool_mode, key_strategy) in enumerate(cells):
        trial = await run_trial(
            python=python,
            system_database_url=system_database_url,
            sink_url=sink_url,
            kill_point=kill_point,
            tool_mode=tool_mode,
            key_strategy=key_strategy,
            sequence=sequence,
        )
        trials.append(trial)
        print(
            f"[{sequence + 1}/{len(cells)}] {kill_point:12} {tool_mode:14} {key_strategy:12} "
            f"refunds={trial.effects_committed} requests={trial.requests_received} "
            f"{trial.verdict} resume={trial.resume.get('outcome', '-')}",
            file=sys.stderr,
            flush=True,
        )
    return trials


def _names(value: str, known: tuple[str, ...], extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    names = known if value == "all" else tuple(value.split(","))
    if bad := [n for n in names if n not in known + extra]:
        raise SystemExit(f"unknown values {bad}; choose from {list(known)}")
    return names


def _sqlalchemy_url(database_url: str) -> str:
    """DBOS reads its system database URL with SQLAlchemy, which wants an explicit driver."""
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url[len("postgresql://") :]
    return database_url


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--python", required=True, help="interpreter with pydantic-ai and dbos")
    parser.add_argument("--local", action="store_true", help="start PostgreSQL and the sink here")
    parser.add_argument("--pgdata", default=None)
    parser.add_argument("--sink-url", default="http://localhost:8101")
    parser.add_argument(
        "--system-database",
        default="postgres",
        help="'postgres' (the stack's server), 'sqlite', or a full SQLAlchemy URL",
    )
    parser.add_argument("--kill-points", default="all")
    parser.add_argument("--tool-modes", default="all")
    parser.add_argument("--key-strategies", default="all")
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument("--seed", type=int, default=5)
    parser.add_argument("--out-dir", default="results/dbos_crash")
    args = parser.parse_args()

    kill_points = _names(args.kill_points, KILL_POINTS, EXTRA_POINTS)
    tool_modes = _names(args.tool_modes, TOOL_MODES)
    key_strategies = _names(args.key_strategies, KEY_STRATEGIES)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    # Recorded before the run: the code that ran, not whatever is checked out at the end.
    run_provenance = provenance(REPO, args.python)

    def system_db(stack_database_url: str | None) -> str:
        if args.system_database == "sqlite":
            return "sqlite:///" + (out_dir / f"{stamp}.dbos.sqlite").resolve().as_posix()
        if args.system_database == "postgres":
            if stack_database_url is None:
                parser.error("--system-database postgres needs --local, or pass a full URL")
            return _sqlalchemy_url(stack_database_url)
        return args.system_database

    def go(system_database_url: str, sink_url: str) -> list[DbosCrashTrial]:
        return asyncio.run(
            run_all(
                python=args.python,
                system_database_url=system_database_url,
                sink_url=sink_url,
                kill_points=kill_points,
                tool_modes=tool_modes,
                key_strategies=key_strategies,
                trials_per_cell=args.trials,
                seed=args.seed,
            )
        )

    if args.local:
        from crashtest.experiment.local_stack import local_stack

        with local_stack(args.pgdata) as stack:
            trials = go(system_db(stack.database_url), stack.sink_url)
    else:
        trials = go(system_db(None), args.sink_url)

    rows = summarise(trials)
    out_file = out_dir / f"{stamp}.json"
    out_file.write_text(
        json.dumps(
            {
                "experiment": "dbos_crash",
                "started_at": started.isoformat(),
                "parameters": {
                    "kill_points": list(kill_points),
                    "tool_modes": list(tool_modes),
                    "key_strategies": list(key_strategies),
                    "trials_per_cell": args.trials,
                    "seed": args.seed,
                    "system_database": args.system_database,
                },
                "environment": {
                    "python": platform.python_version(),
                    "platform": platform.platform(),
                    "subject": next((t.client for t in trials if t.client), ""),
                },
                "provenance": run_provenance,
                "summary": rows,
                "trials": [asdict(t) for t in trials],
            },
            indent=2,
        )
    )
    print(format_table(rows))
    print(f"\nwrote {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
