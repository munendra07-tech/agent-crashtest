"""The results tables: refund counts and verdicts, from the published results files.

    python -m crashtest.results.table --summary   # the table in docs/research.md
    python -m crashtest.results.table             # every cell: docs/results.md

Only the run files listed in `results/PUBLIC_RUNS.txt` are read, never a glob, and
only experiments 001 to 008. Results files are never edited.

What each column means:
- Refund counts (once, twice or more, none) are the sink's counts, as recorded.
- Verdicts are re-derived from each recorded trial with the current checker
  (`crashtest.checker.classify`) and the runners' status rules
  (`crashtest.results.reverdict`), taking every refund's amount as correct: the
  harness always refunds the same amount, and the amounts can be checked only
  against the sink database (`python -m crashtest.results.reverdict`), which is
  not part of the published results. "Differs" counts trials whose re-derived
  verdict is not the one the runner recorded.
- Experiment 006 has no status rule in `reverdict` (its subject is a real model),
  so its verdicts are the recorded ones, and its write-up gives the hand-checked
  corrections.
- Rows never pool different machines or run conditions: a row is one
  experiment, one operating system, one set of run conditions and, in the full
  table, one cell.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from crashtest.checker import classify
from crashtest.results.reverdict import _status_and_confirmed
from crashtest.results.store import CONDITIONS, FACTORS, cell_of, condition_of, is_run_file

REPO = Path(__file__).resolve().parents[3]
ALLOWLIST = Path("results") / "PUBLIC_RUNS.txt"

# The published experiments, their numbers and write-ups. Anything else is refused.
EXPERIMENTS: dict[str, tuple[str, str]] = {
    "mcp_refund_lost_response": ("001", "docs/experiments/001-mcp-refund-lost-response.md"),
    "mcp_refund": ("002", "docs/experiments/002-mcp-client-matrix.md"),
    "mcp_single_call": ("003", "docs/experiments/003-mcp-client-libraries.md"),
    "langgraph_crash": ("004", "docs/experiments/004-langgraph-crash-resume.md"),
    "dbos_crash": ("005", "docs/experiments/005-pydantic-ai-dbos.md"),
    "model_retry": ("006", "docs/experiments/006-model-retry.md"),
    "mcp_resume": ("007", "docs/experiments/007-mcp-resume.md"),
    "mcp_cut_points": ("008", "docs/experiments/008-mcp-cut-points.md"),
}
RECORDED_ONLY = {"model_retry"}  # no status rule to re-derive from

NOTES: dict[str, str] = {
    "mcp_refund": (
        "002's four transports were one on the wire (the reference client negotiated protocol "
        "2026-07-28 with every server); the rows keep the recorded transport labels. See the "
        "dated note in the write-up."
    ),
    "model_retry": (
        "Recorded verdicts. The hand check of the main run moved five of them (for example "
        "`correct` 478 to 480): see the write-up's hand-corrected counts. Pilots are listed "
        "apart from the main run."
    ),
}


class NotPublishable(Exception):
    """The allowlist names a file the public tables must not read."""


@dataclass
class Row:
    experiment: str
    os: str
    condition: dict
    cell: dict
    trials: int = 0
    once: int = 0
    more: int = 0
    none: int = 0
    verdicts: Counter = field(default_factory=Counter)
    differs: int = 0
    files: set = field(default_factory=set)


def read_allowlist(repo: Path = REPO) -> list[Path]:
    """The run files to read, checked: inside results/, present, unique."""
    lines = (repo / ALLOWLIST).read_text(encoding="utf-8").splitlines()
    paths = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    if len(set(paths)) != len(paths):
        raise NotPublishable("the allowlist names a file twice")
    out = []
    for rel in paths:
        p = Path(rel)
        if p.is_absolute() or p.parts[0] != "results" or ".." in p.parts or len(p.parts) != 3:
            raise NotPublishable(f"{rel}: not a results/<experiment>/<file> path")
        if not (repo / p).is_file():
            raise NotPublishable(f"{rel}: no such file")
        out.append(p)
    return out


def load_runs(repo: Path = REPO) -> tuple[list[dict], list[tuple[str, int]]]:
    """Every allowlisted run, and the run files with no verdict (listed, not counted)."""
    runs, unjudged = [], []
    for rel in read_allowlist(repo):
        doc = json.loads((repo / rel).read_text(encoding="utf-8"))
        if not is_run_file(doc):
            raise NotPublishable(f"{rel.as_posix()}: not a run file")
        if doc["experiment"] not in EXPERIMENTS:
            raise NotPublishable(
                f"{rel.as_posix()}: experiment {doc['experiment']!r} is not published"
            )
        if any("verdict" not in t for t in doc["trials"]):
            unjudged.append((rel.as_posix(), len(doc["trials"])))
            continue
        runs.append({"file": rel.as_posix(), **doc})
    return runs, unjudged


def _condition(run: dict) -> tuple[str, dict]:
    experiment = run["experiment"]
    cond = condition_of(experiment, run.get("parameters") or {}, run.get("environment") or {})
    os_name = cond.pop("os") or "unknown"
    if experiment == "model_retry":
        cond["run"] = "pilot" if "-pilot" in run["file"] else "main"
    if experiment == "dbos_crash":
        # DBOS's own state store; the Linux run records a full URL, reduced to its kind.
        database = str((run.get("parameters") or {}).get("system_database", ""))
        cond["system_database"] = "sqlite" if "sqlite" in database else "postgres"
    return os_name, cond


# Conditions this table adds to the result store's, per experiment.
EXTRA_CONDITIONS: dict[str, tuple[str, ...]] = {
    "model_retry": ("run",),
    "dbos_crash": ("system_database",),
}


def build_rows(runs: list[dict], per_cell: bool) -> list[Row]:
    rows: dict[str, Row] = {}
    for run in runs:
        experiment = run["experiment"]
        os_name, cond = _condition(run)
        for t in run["trials"]:
            cell = cell_of(experiment, t) if per_cell else {}
            key = json.dumps([experiment, os_name, cond, cell], sort_keys=True, default=str)
            row = rows.setdefault(key, Row(experiment, os_name, cond, cell))
            effects = t["effects_committed"]
            row.trials += 1
            row.once += effects == 1
            row.more += effects > 1
            row.none += effects == 0
            row.files.add(run["file"])
            if experiment in RECORDED_ONLY:
                row.verdicts[t["verdict"] or "no verdict recorded"] += 1
                continue
            status, confirmed = _status_and_confirmed(experiment, t)
            verdict = classify(
                effects=effects,
                intended=t.get("intended_effects", 1),
                status=status,
                payload_matches=True,
                confirmed=confirmed,
            )
            row.verdicts[verdict] += 1
            row.differs += verdict != t["verdict"]
    order = list(EXPERIMENTS)
    return sorted(
        rows.values(),
        key=lambda r: (
            order.index(r.experiment),
            r.os,
            json.dumps(r.condition, sort_keys=True, default=str),
            json.dumps(r.cell, sort_keys=True, default=str),
        ),
    )


def _verdicts(c: Counter) -> str:
    return ", ".join(f"{v} {n}" for v, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))


def _cond(d: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in d.items()) or "-"


def summary_table(repo: Path = REPO) -> str:
    runs, unjudged = load_runs(repo)
    rows = build_rows(runs, per_cell=False)
    lines = [
        "| Exp. | Machine | Run conditions | Files | Trials | Refunded once | Twice or more "
        "| None | Verdicts | Differs |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        number, doc = EXPERIMENTS[r.experiment]
        link = f"experiments/{Path(doc).name}"  # relative to docs/research.md
        lines.append(
            f"| [{number}]({link}) | {r.os} | {_cond(r.condition)} | {len(r.files)} | {r.trials} | "
            f"{r.once} | {r.more} | {r.none} | {_verdicts(r.verdicts)}"
            f"{' (recorded)' if r.experiment in RECORDED_ONLY else ''} | "
            f"{'-' if r.experiment in RECORDED_ONLY else r.differs} |"
        )
    lines += _footer(unjudged)
    lines.append("Per cell, with notes on 002 and 006: [results.md](results.md).")
    return "\n".join(lines) + "\n"


def full_table(repo: Path = REPO) -> str:
    runs, unjudged = load_runs(repo)
    rows = build_rows(runs, per_cell=True)
    out = [
        "# Results, cell by cell",
        "",
        "Generated by `python -m crashtest.results.table` from the run files listed in "
        "`results/PUBLIC_RUNS.txt`; do not edit by hand. How to read it: the module "
        "docstring of `src/crashtest/results/table.py`, [PROVENANCE.md](../PROVENANCE.md) "
        "and [contract.md](contract.md).",
    ]
    for experiment, (number, doc) in EXPERIMENTS.items():
        mine = [r for r in rows if r.experiment == experiment]
        if not mine:
            continue
        factors = FACTORS[experiment]
        conds = (*CONDITIONS.get(experiment, ()), *EXTRA_CONDITIONS.get(experiment, ()))
        out += ["", f"## {number}: [{Path(doc).stem}](experiments/{Path(doc).name})", ""]
        if experiment in NOTES:
            out += [NOTES[experiment], ""]
        head = ["Machine", *conds, *factors, "Trials", "Once", "Twice or more", "None", "Verdicts"]
        if experiment not in RECORDED_ONLY:
            head.append("Differs")
        out += ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
        for r in mine:
            cells = [
                r.os,
                *(str(r.condition.get(c)) for c in conds),
                *(str(r.cell.get(f)) for f in factors),
                str(r.trials),
                str(r.once),
                str(r.more),
                str(r.none),
                _verdicts(r.verdicts),
            ]
            if experiment not in RECORDED_ONLY:
                cells.append(str(r.differs))
            out.append("| " + " | ".join(cells) + " |")
    out += _footer(unjudged)
    return "\n".join(out) + "\n"


def _footer(unjudged: list[tuple[str, int]]) -> list[str]:
    lines = [
        "",
        "Verdicts are re-derived with the current checker from the recorded trials, taking every "
        'refund\'s amount as correct (only the sink database can check amounts); "Differs" counts '
        "trials whose recorded verdict is different. 006 shows its recorded verdicts.",
    ]
    if unjudged:
        listed = "; ".join(f"`{f}` ({n} trials)" for f, n in unjudged)
        lines += ["", f"Not judged (written before the checker, no verdicts recorded): {listed}."]
    lines.append("")
    return lines


README_START = (
    "<!-- results-table:start (generated by python -m crashtest.results.table --write) -->"
)
README_END = "<!-- results-table:end -->"
RESULTS_MD = Path("docs") / "results.md"
# The summary table lives in docs/research.md (it was in README.md until 28 Nov 2026).
SUMMARY_MD = Path("docs") / "research.md"


def readme_block(readme: str) -> str:
    """The generated table inside docs/research.md, between its two markers."""
    start = readme.index(README_START) + len(README_START)
    return readme[start : readme.index(README_END)].strip("\n") + "\n"


def write_all(repo: Path = REPO) -> None:
    """Regenerate docs/results.md and the table in docs/research.md."""
    (repo / RESULTS_MD).write_text(full_table(repo), encoding="utf-8", newline="\n")
    path = repo / SUMMARY_MD
    readme = path.read_text(encoding="utf-8")
    old = readme_block(readme)
    path.write_text(readme.replace(old, summary_table(repo), 1), encoding="utf-8", newline="")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--summary", action="store_true", help="the table in docs/research.md")
    parser.add_argument(
        "--write", action="store_true", help="update docs/research.md and docs/results.md"
    )
    parser.add_argument("--out", type=Path, help="write here instead of standard output")
    args = parser.parse_args()
    if args.write:
        write_all()
        return 0
    text = summary_table() if args.summary else full_table()
    if args.out:
        args.out.write_text(text, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
