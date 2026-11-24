"""Run ONE tool call through an external MCP client and count what reached the sink.

Experiments 001 and 002 used a hand-written retry loop on the reference client.
Here there is no loop: a subject script makes a single call through some client
library, in its own Python environment, and the sink shows how many requests
that one call turned into. More than one request means the library resent.

    python -m crashtest.experiment.single_call --local \
        --subject mcp_sdk=.venv/Scripts/python.exe \
        --subject crewai=D:/envs/crewai/Scripts/python.exe

Each subject name must match a script in the `subjects/` directory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from crashtest import control_token
from crashtest.checker import (
    REPORT_OF_STATUS,
    VERDICTS,
    Report,
    SubjectStatus,
    classify,
    payload_matches,
    report_matches_sink,
)
from crashtest.experiment.local_stack import DEFAULT_TRANSPORT, TRANSPORTS
from crashtest.experiment.trial import FAULTS
from crashtest.faults.drop_proxy import DropProxy

SUBJECTS_DIR = Path(__file__).resolve().parents[3] / "subjects"
AMOUNT_CENTS = 2500  # the refund each subject is asked to make

COLUMNS = (
    "subject",
    "client",
    "transport",
    "fault",
    "trials",
    "one_request",
    "resent",
    "max_requests",
    "refunded_once",
    "refunded_twice_or_more",
    "not_refunded",
    "subject_outcomes",
    "verdicts",
    "client_requests",
    "fault_confirmed",
    "report_mismatch",
)


@dataclass
class SingleCallResult:
    subject: str
    client: str
    transport: str
    fault: str
    order_id: str
    subject_outcome: str  # "ok", "error", "raised", "hung" or "no_report"
    detail: str
    elapsed_s: float
    requests_received: int
    effects_committed: int
    stderr_tail: str = ""
    verdict: str = ""
    # Tool calls the client put on the wire, counted at the proxy.
    client_requests: int = 0
    # Whether the injected fault is known to have fired where its fault point says
    # (contract v0.4); for this runner's faults, that is after the commit.
    fault_confirmed: bool | None = None
    # What the subject told its caller agrees with the sink (contract v0.3, RA).
    report_matches_sink: bool | None = None
    # The proxy's sink reads retried after a dropped keep-alive connection, in this
    # trial. Results files written before this field existed do not have it.
    sink_retries: int | None = None


async def run_single_call(
    *,
    subject: str,
    python: str,
    transport: str,
    fault: str,
    mcp_url: str,
    sink_url: str,
    drop_proxy: DropProxy,
    subject_timeout_s: float,
    fault_delay_s: float,
    hang_limit_s: float,
    settle_s: float,
) -> SingleCallResult:
    order_id = f"ord-{uuid.uuid4()}"
    script = SUBJECTS_DIR / f"{subject}.py"
    # Every call goes through the proxy, which counts what the client sent. It
    # only interferes with the connection when armed.
    mcp_url = drop_proxy.url
    drop_proxy.watch(order_id)
    drops_before = len(drop_proxy.drops)
    sink_retries_before = drop_proxy.sink_retries

    async with httpx.AsyncClient(
        base_url=sink_url, timeout=30.0, headers=control_token.control_headers(sink_url)
    ) as sink:
        if fault == "lost_response":
            r = await sink.put(
                "/control/faults",
                json={"order_id": order_id, "delay_s": fault_delay_s, "times": 1},
            )
            r.raise_for_status()
        elif fault == "connection_drop":
            drop_proxy.arm(order_id)

        proc = await asyncio.create_subprocess_exec(
            python,
            str(script),
            "--mcp-url",
            mcp_url,
            "--order-id",
            order_id,
            "--amount-cents",
            str(AMOUNT_CENTS),
            "--timeout",
            str(subject_timeout_s),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        started = asyncio.get_running_loop().time()
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=hang_limit_s)
            report = _last_json_line(stdout.decode(errors="replace"))
            outcome = report.get("outcome", "no_report")
        except TimeoutError:
            proc.kill()
            stdout, stderr = await proc.communicate()
            report, outcome = {}, "hung"
        elapsed = asyncio.get_running_loop().time() - started

        # The subject has exited, so nothing more can be sent. A short pause lets
        # requests already in flight reach the sink before it is read.
        await asyncio.sleep(settle_s)
        r = await sink.get("/refunds", params={"order_id": order_id})
        r.raise_for_status()
        truth = r.json()

    client_requests = drop_proxy.unwatch(order_id)
    # The proxy's sink reads that were retried after a dropped connection.
    sink_retries = drop_proxy.sink_retries - sink_retries_before
    fault_confirmed: bool | None = None
    if fault == "lost_response":
        fault_confirmed = any(q.get("fault_applied") for q in truth["requests"])
    elif fault == "connection_drop":
        fault_confirmed = any(
            d.order_id == order_id and d.landed_as_intended()
            for d in drop_proxy.drops[drops_before:]
        )
    # A subject that raises has told its caller it does not know the outcome; one
    # that returns a tool error has reported a definite failure.
    status: SubjectStatus = (
        "completed" if outcome == "ok" else ("unknown" if outcome == "raised" else "failed")
    )
    told: Report = REPORT_OF_STATUS[status]

    return SingleCallResult(
        subject=subject,
        client=str(report.get("client", "")),
        transport=transport,
        fault=fault,
        order_id=order_id,
        subject_outcome=outcome,
        detail=str(report.get("detail", ""))[:300],
        elapsed_s=round(float(report.get("elapsed_s", elapsed)), 3),
        requests_received=len(truth["requests"]),
        effects_committed=len(truth["refunds"]),
        stderr_tail="" if outcome in ("ok",) else stderr.decode(errors="replace")[-400:],
        verdict=classify(
            effects=len(truth["refunds"]),
            intended=1,
            status=status,
            payload_matches=payload_matches(
                truth["refunds"], order_id=order_id, amount_cents=AMOUNT_CENTS
            ),
            # "ok" is reported only from a successful tool result.
            confirmed=outcome == "ok",
        ),
        client_requests=client_requests,
        fault_confirmed=fault_confirmed,
        sink_retries=sink_retries,
        report_matches_sink=report_matches_sink(
            effects=len(truth["refunds"]), intended=1, report=told
        ),
    )


def _last_json_line(text: str) -> dict:
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return {}


def summarise(results: list[SingleCallResult]) -> list[dict]:
    rows = []
    keys = list(dict.fromkeys((r.subject, r.transport, r.fault) for r in results))
    for subject, transport, fault in keys:
        cell = [
            r for r in results if (r.subject, r.transport, r.fault) == (subject, transport, fault)
        ]
        outcomes = Counter(r.subject_outcome for r in cell)
        rows.append(
            {
                "subject": subject,
                "client": next((r.client for r in cell if r.client), ""),
                "transport": transport,
                "fault": fault,
                "trials": len(cell),
                "one_request": sum(r.requests_received == 1 for r in cell),
                "resent": sum(r.requests_received > 1 for r in cell),
                "max_requests": max(r.requests_received for r in cell),
                "refunded_once": sum(r.effects_committed == 1 for r in cell),
                "refunded_twice_or_more": sum(r.effects_committed > 1 for r in cell),
                "not_refunded": sum(r.effects_committed == 0 for r in cell),
                "subject_outcomes": ", ".join(f"{k} x{v}" for k, v in sorted(outcomes.items())),
                "verdicts": ", ".join(
                    f"{v} x{n}" for v in VERDICTS if (n := sum(r.verdict == v for r in cell))
                ),
                "client_requests": sum(r.client_requests for r in cell),
                "fault_confirmed": (
                    "-"
                    if all(r.fault_confirmed is None for r in cell)
                    else sum(bool(r.fault_confirmed) for r in cell)
                ),
                # Trials whose report to the caller contradicts the sink (contract v0.3, RA).
                "report_mismatch": sum(r.report_matches_sink is False for r in cell),
            }
        )
    return rows


def format_table(rows: list[dict]) -> str:
    lines = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    lines += ["| " + " | ".join(str(r[c]) for c in COLUMNS) + " |" for r in rows]
    return "\n".join(lines)


async def run_all(
    *,
    subjects: dict[str, str],
    sink_url: str,
    mcp_urls: dict[str, str],
    faults: tuple[str, ...],
    trials_per_cell: int,
    subject_timeout_s: float,
    fault_delay_s: float,
    hang_limit_s: float,
    settle_s: float,
) -> list[SingleCallResult]:
    results: list[SingleCallResult] = []
    for transport, mcp_url in mcp_urls.items():
        async with DropProxy(upstream_url=mcp_url, sink_url=sink_url) as proxy:
            for subject, python in subjects.items():
                for fault in faults:
                    for _ in range(trials_per_cell):
                        result = await run_single_call(
                            subject=subject,
                            python=python,
                            transport=transport,
                            fault=fault,
                            mcp_url=mcp_url,
                            sink_url=sink_url,
                            drop_proxy=proxy,
                            subject_timeout_s=subject_timeout_s,
                            fault_delay_s=fault_delay_s,
                            hang_limit_s=hang_limit_s,
                            settle_s=settle_s,
                        )
                        results.append(result)
                        print(
                            f"{subject:16} {transport:15} {fault:16} "
                            f"requests={result.requests_received} "
                            f"refunds={result.effects_committed} {result.subject_outcome}",
                            file=sys.stderr,
                            flush=True,
                        )
    return results


def _subject_arg(value: str) -> tuple[str, str]:
    name, sep, python = value.partition("=")
    if not sep or not name or not python:
        raise argparse.ArgumentTypeError("expected NAME=PATH_TO_PYTHON")
    if not (SUBJECTS_DIR / f"{name}.py").is_file():
        raise argparse.ArgumentTypeError(f"no subject script {SUBJECTS_DIR / (name + '.py')}")
    return name, python


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--local", action="store_true", help="start the stack on this machine")
    parser.add_argument("--pgdata", default=None)
    parser.add_argument("--sink-url", default="http://localhost:8101")
    parser.add_argument("--mcp-url", default="http://localhost:8102/mcp")
    parser.add_argument(
        "--mcp-url-for",
        action="append",
        default=[],
        metavar="TRANSPORT=URL",
        help="without --local: the MCP server for a transport other than the default",
    )
    parser.add_argument("--subject", action="append", type=_subject_arg, required=True)
    parser.add_argument("--transports", default=DEFAULT_TRANSPORT, help="comma-separated or 'all'")
    parser.add_argument("--faults", default="all", help="comma-separated or 'all'")
    parser.add_argument("--trials", type=int, default=10)
    parser.add_argument("--subject-timeout", type=float, default=2.0)
    parser.add_argument("--fault-delay", type=float, default=8.0)
    parser.add_argument("--hang-limit", type=float, default=90.0)
    parser.add_argument("--settle", type=float, default=1.0)
    parser.add_argument("--out-dir", default="results/single_call")
    args = parser.parse_args()

    transports = (
        tuple(TRANSPORTS) if args.transports == "all" else tuple(args.transports.split(","))
    )
    faults = FAULTS if args.faults == "all" else tuple(args.faults.split(","))
    if bad := [t for t in transports if t not in TRANSPORTS]:
        parser.error(f"unknown transports: {bad}")
    if bad := [f for f in faults if f not in FAULTS]:
        parser.error(f"unknown faults: {bad}")
    external_urls = {DEFAULT_TRANSPORT: args.mcp_url}
    for item in args.mcp_url_for:
        name, sep, url = item.partition("=")
        if not sep or name not in TRANSPORTS or not url:
            parser.error(f"--mcp-url-for expects TRANSPORT=URL, got {item!r}")
        external_urls[name] = url
    if not args.local and (missing := [t for t in transports if t not in external_urls]):
        parser.error(f"no MCP server given for {missing}; pass --mcp-url-for or use --local")
    subjects = dict(args.subject)

    def go(sink_url: str, mcp_urls: dict[str, str]) -> list[SingleCallResult]:
        return asyncio.run(
            run_all(
                subjects=subjects,
                sink_url=sink_url,
                mcp_urls=mcp_urls,
                faults=faults,
                trials_per_cell=args.trials,
                subject_timeout_s=args.subject_timeout,
                fault_delay_s=args.fault_delay,
                hang_limit_s=args.hang_limit,
                settle_s=args.settle,
            )
        )

    started = datetime.now(UTC)
    # Recorded before the run: the code that ran, not whatever is checked out at the end.
    from crashtest.experiment.crash_common import provenance  # avoids an import cycle

    run_provenance = provenance(SUBJECTS_DIR.parent)
    run_provenance["subject_packages"] = {
        name: provenance(SUBJECTS_DIR.parent, python).get("subject_packages", [])
        for name, python in subjects.items()
    }
    if args.local:
        from crashtest.experiment.local_stack import local_stack

        with local_stack(args.pgdata, transports=transports) as stack:
            results = go(stack.sink_url, {t: stack.mcp_urls[t] for t in transports})
    else:
        results = go(args.sink_url, {t: external_urls[t] for t in transports})

    rows = summarise(results)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out_file.write_text(
        json.dumps(
            {
                "experiment": "mcp_single_call",
                "started_at": started.isoformat(),
                "parameters": {
                    "subjects": list(subjects),
                    "transports": list(transports),
                    "faults": list(faults),
                    "trials_per_cell": args.trials,
                    "subject_timeout_s": args.subject_timeout,
                    "fault_delay_s": args.fault_delay,
                    "hang_limit_s": args.hang_limit,
                },
                "environment": {
                    "python": platform.python_version(),
                    "platform": platform.platform(),
                },
                "provenance": run_provenance,
                "summary": rows,
                "trials": [asdict(r) for r in results],
            },
            indent=2,
        )
    )
    print(format_table(rows))
    print(f"\nwrote {out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
