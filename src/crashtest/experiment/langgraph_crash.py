"""Kill a LangGraph agent at a chosen point, resume it, and count the refunds.

The subject (subjects/langgraph_refund_agent.py) runs as a separate process in its
own Python environment. A trial is:

  1. start the agent on a new thread;
  2. kill the process (hard kill, no cleanup) at the kill point;
  3. start it again with --resume, as an application would after a restart;
  4. read from the sink how many refunds exist for the order.

Kill points:
  none         no kill; the control
  during_tool  the sink has committed the refund but is withholding its response,
               so the tool call is in flight and its result is unrecorded
  after_tool   the tool has returned and the agent is entering its next model call

    python -m crashtest.experiment.langgraph_crash --local \
        --python D:/envs/langgraph/Scripts/python.exe
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
from itertools import product
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
from crashtest.experiment.crash_common import finish as _finish
from crashtest.experiment.crash_common import provenance, shuffled_cells
from crashtest.experiment.crash_common import wait_for_marker as _wait_for_marker
from crashtest.experiment.crash_common import wait_for_refund as _wait_for_refund
from crashtest.experiment.single_call import SUBJECTS_DIR

AMOUNT_CENTS = 2500  # the refund the subject is asked to make

SCRIPT = SUBJECTS_DIR / "langgraph_refund_agent.py"
DURABILITIES = ("sync", "async", "exit")
KEY_STRATEGIES = ("none", "tool_call_id", "business")
KILL_POINTS = ("none", "during_tool", "after_tool")

COLUMNS = (
    "kill_point",
    "durability",
    "key_strategy",
    "trials",
    "refunded_once",
    "refunded_twice",
    "not_refunded",
    "verdicts",
    "fault_confirmed",
    "report_mismatch",
    "requests_at_sink",
    "resume_paths",
    "kill_delay_s",
    "next_at_resume",
)


@dataclass
class CrashTrial:
    kill_point: str
    durability: str
    key_strategy: str
    order_id: str
    thread_id: str
    killed: bool
    effects_committed: int
    requests_received: int
    verdict: str
    fault_confirmed: bool | None = None
    # What the subject told its caller agrees with the sink (contract v0.3, RA).
    report_matches_sink: bool | None = None
    first_run: dict = field(default_factory=dict)
    resume: dict = field(default_factory=dict)
    client: str = ""
    # Seconds between the subject announcing the kill point and the kill (audit action 4).
    kill_delay_s: float = 0.0


async def _spawn(python: str, *args: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        python,
        str(SCRIPT),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def run_crash_trial(
    *,
    python: str,
    database_url: str,
    sink_url: str,
    kill_point: str,
    durability: str,
    key_strategy: str,
    kill_delay_s: float = 0.0,
    hold_response_s: float = 20.0,
    step_limit_s: float = 90.0,
) -> CrashTrial:
    order_id = f"ord-{uuid.uuid4()}"
    thread_id = f"thread-{uuid.uuid4()}"
    common = [
        "--database-url",
        database_url,
        "--sink-url",
        sink_url,
        "--order-id",
        order_id,
        "--amount-cents",
        str(AMOUNT_CENTS),
        "--thread-id",
        thread_id,
        "--durability",
        durability,
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
            first_run = await _finish(await _spawn(python, *common), step_limit_s)
        else:
            if kill_point == "during_tool":
                # The sink commits, then sits on the response while the process is killed.
                r = await sink.put(
                    "/control/faults",
                    json={"order_id": order_id, "delay_s": hold_response_s, "times": 1},
                )
                r.raise_for_status()
                proc = await _spawn(python, *common)
                reached = await _wait_for_refund(sink, order_id, step_limit_s)
            else:
                proc = await _spawn(python, *common, "--hold-at", "model2")
                reached = await _wait_for_marker(proc, "AT_HOLD", step_limit_s)
            if kill_delay_s:
                # Give the subject's background work (an async checkpoint write) a
                # controlled time to finish before the kill.
                await asyncio.sleep(kill_delay_s)
            proc.kill()  # hard kill: no handlers run, nothing is flushed
            await proc.communicate()
            killed = True
            first_run = {"outcome": "killed", "reached_kill_point": reached}

            resume = await _finish(await _spawn(python, *common, "--resume"), step_limit_s)

        r = await sink.get("/refunds", params={"order_id": order_id})
        r.raise_for_status()
        truth = r.json()

    effects = len(truth["refunds"])
    # The run that matters is the last one: the resume after a kill, or the only run.
    last = resume if killed else first_run
    status: SubjectStatus = (
        "completed" if last.get("outcome") == "ok" and last.get("answered") else "failed"
    )
    verdict = classify(
        effects=effects,
        intended=1,
        status=status,
        payload_matches=payload_matches(
            truth["refunds"], order_id=order_id, amount_cents=AMOUNT_CENTS
        ),
        # The scripted model answers only after the tool has returned a result.
        confirmed=status == "completed",
    )
    # A subject that did not answer told its caller nothing ("none").
    reported_ok = report_matches_sink(
        effects=effects, intended=1, report="done" if status == "completed" else "none"
    )
    fault_confirmed: bool | None = None
    if kill_point == "during_tool":
        fault_confirmed = any(q.get("fault_applied") for q in truth["requests"])
    return CrashTrial(
        report_matches_sink=reported_ok,
        kill_point=kill_point,
        durability=durability,
        key_strategy=key_strategy,
        order_id=order_id,
        thread_id=thread_id,
        killed=killed,
        effects_committed=effects,
        requests_received=len(truth["requests"]),
        verdict=verdict,
        fault_confirmed=fault_confirmed,
        first_run=first_run,
        resume=resume,
        client=str((resume or first_run).get("client", "")),
        kill_delay_s=kill_delay_s,
    )


def summarise(trials: list[CrashTrial]) -> list[dict]:
    rows = []

    def key_of(t: CrashTrial) -> tuple:
        return (t.kill_point, t.durability, t.key_strategy, t.kill_delay_s)

    keys = sorted(dict.fromkeys(key_of(t) for t in trials), key=lambda k: (k[0], k[1], k[2], k[3]))
    for key in keys:
        cell = [t for t in trials if key_of(t) == key]
        paths = Counter(t.resume.get("path", "-") for t in cell)
        # What `get_state().next` showed the restarted process: "-" when there was
        # no restart, "[]" when the saved state listed nothing to run.
        seen_next = Counter(
            str(t.resume["resume_saw"]["next"]) if "resume_saw" in t.resume else "-" for t in cell
        )
        rows.append(
            {
                "kill_point": key[0],
                "durability": key[1],
                "key_strategy": key[2],
                "trials": len(cell),
                "refunded_once": sum(t.effects_committed == 1 for t in cell),
                "refunded_twice": sum(t.effects_committed > 1 for t in cell),
                "not_refunded": sum(t.effects_committed == 0 for t in cell),
                "verdicts": ", ".join(
                    f"{v} x{n}" for v in VERDICTS if (n := sum(t.verdict == v for t in cell))
                ),
                "fault_confirmed": (
                    "-"
                    if all(t.fault_confirmed is None for t in cell)
                    else sum(bool(t.fault_confirmed) for t in cell)
                ),
                # Trials whose report to the caller contradicts the sink (contract v0.3, RA).
                "report_mismatch": sum(t.report_matches_sink is False for t in cell),
                "requests_at_sink": sum(t.requests_received for t in cell),
                "resume_paths": ", ".join(f"{k} x{v}" for k, v in sorted(paths.items())),
                "kill_delay_s": key[3],
                "next_at_resume": ", ".join(f"{k} x{v}" for k, v in sorted(seen_next.items())),
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
    database_url: str,
    sink_url: str,
    kill_points: tuple[str, ...],
    durabilities: tuple[str, ...],
    key_strategies: tuple[str, ...],
    trials_per_cell: int,
    kill_delays: tuple[float, ...] = (0.0,),
    seed: int | None = None,
) -> list[CrashTrial]:
    setup = await _finish(
        await _spawn(
            python,
            "--database-url",
            database_url,
            "--sink-url",
            sink_url,
            "--order-id",
            "setup",
            "--thread-id",
            "setup",
            "--setup-only",
        ),
        120.0,
    )
    if setup.get("outcome") != "setup":
        raise RuntimeError(f"could not create the checkpoint tables: {setup}")

    delays = [str(d) for d in kill_delays]
    factors = (kill_points, durabilities, key_strategies, delays)
    cells: list[tuple[str, ...]]
    if seed is None:
        # Block order, as in the runs of 16 November.
        cells = [c for c in product(*factors) for _ in range(trials_per_cell)]
    else:
        cells = shuffled_cells(factors, trials_per_cell, seed)

    trials: list[CrashTrial] = []
    for kill_point, durability, key_strategy, delay in cells:
        trial = await run_crash_trial(
            python=python,
            database_url=database_url,
            sink_url=sink_url,
            kill_point=kill_point,
            durability=durability,
            key_strategy=key_strategy,
            kill_delay_s=float(delay),
        )
        trials.append(trial)
        print(
            f"{kill_point:12} {durability:6} {key_strategy:13} delay={delay:5} "
            f"refunds={trial.effects_committed} requests={trial.requests_received} "
            f"resume={trial.resume.get('path', '-')}/{trial.resume.get('outcome', '-')} "
            f"next={trial.resume.get('resume_saw', {}).get('next', '-')}",
            file=sys.stderr,
            flush=True,
        )
    return trials


def _names(value: str, known: tuple[str, ...]) -> tuple[str, ...]:
    names = known if value == "all" else tuple(value.split(","))
    if bad := [n for n in names if n not in known]:
        raise SystemExit(f"unknown values {bad}; choose from {list(known)}")
    return names


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--python", required=True, help="interpreter with langgraph installed")
    parser.add_argument("--local", action="store_true", help="start PostgreSQL and the sink here")
    parser.add_argument("--pgdata", default=None)
    parser.add_argument("--sink-url", default="http://localhost:8101")
    parser.add_argument("--database-url", default=None, help="checkpointer DB, without --local")
    parser.add_argument("--kill-points", default="all")
    parser.add_argument("--durabilities", default="all")
    parser.add_argument("--key-strategies", default="all")
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument(
        "--kill-delays",
        default="0",
        help="comma-separated seconds between the after_tool marker and the kill",
    )
    parser.add_argument("--seed", type=int, default=None, help="randomise cell order")
    parser.add_argument("--out-dir", default="results/langgraph_crash")
    args = parser.parse_args()

    kill_points = _names(args.kill_points, KILL_POINTS)
    durabilities = _names(args.durabilities, DURABILITIES)
    key_strategies = _names(args.key_strategies, KEY_STRATEGIES)
    kill_delays = tuple(float(d) for d in args.kill_delays.split(","))

    def go(database_url: str, sink_url: str) -> list[CrashTrial]:
        return asyncio.run(
            run_all(
                python=args.python,
                database_url=database_url,
                sink_url=sink_url,
                kill_points=kill_points,
                durabilities=durabilities,
                key_strategies=key_strategies,
                trials_per_cell=args.trials,
                kill_delays=kill_delays,
                seed=args.seed,
            )
        )

    started = datetime.now(UTC)
    # Recorded before the run: the code that ran, not whatever is checked out at the end.
    run_provenance = provenance(SUBJECTS_DIR.parent, args.python)
    if args.local:
        from crashtest.experiment.local_stack import local_stack

        with local_stack(args.pgdata) as stack:
            trials = go(stack.database_url, stack.sink_url)
    else:
        if not args.database_url:
            parser.error("--database-url is required without --local")
        trials = go(args.database_url, args.sink_url)

    rows = summarise(trials)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out_file.write_text(
        json.dumps(
            {
                "experiment": "langgraph_crash",
                "started_at": started.isoformat(),
                "parameters": {
                    "kill_points": list(kill_points),
                    "durabilities": list(durabilities),
                    "key_strategies": list(key_strategies),
                    "trials_per_cell": args.trials,
                    "kill_delays_s": list(kill_delays),
                    "seed": args.seed,
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
