"""The published results tables match the results files and read only publishable runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crashtest.results import table

REPO = Path(__file__).resolve().parents[1]


def test_the_summary_table_matches_the_results_files():
    research = (REPO / table.SUMMARY_MD).read_text(encoding="utf-8")
    assert table.readme_block(research) == table.summary_table(REPO), (
        "docs/research.md's results table is out of date: "
        "run python -m crashtest.results.table --write"
    )


def test_the_readme_no_longer_carries_the_results_table():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert table.README_START not in readme


def test_docs_results_matches_the_results_files():
    text = (REPO / "docs" / "results.md").read_text(encoding="utf-8")
    assert text == table.full_table(REPO), (
        "docs/results.md is out of date: run python -m crashtest.results.table --write"
    )


def test_every_allowlisted_file_is_a_published_run():
    runs, unjudged = table.load_runs(REPO)
    assert {r["experiment"] for r in runs} <= set(table.EXPERIMENTS)
    assert len(runs) + len(unjudged) == len(table.read_allowlist(REPO))


def _repo(tmp_path: Path, lines: list[str], files: dict[str, dict]) -> Path:
    (tmp_path / "results").mkdir(parents=True)
    (tmp_path / "results" / "PUBLIC_RUNS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for rel, doc in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc), encoding="utf-8")
    return tmp_path


def _run(experiment: str) -> dict:
    return {
        "experiment": experiment,
        "started_at": "2026-11-23T00:00:00Z",
        "parameters": {},
        "environment": {"platform": "Linux-x"},
        "trials": [],
    }


def test_an_unpublished_experiment_is_refused(tmp_path):
    rel = "results/some_unpublished_experiment/x.json"
    repo = _repo(tmp_path, [rel], {rel: _run("some_unpublished_experiment")})
    with pytest.raises(table.NotPublishable, match="not published"):
        table.load_runs(repo)


@pytest.mark.parametrize(
    "line",
    [
        "docs/x.json",
        "results/x.json",
        "results/a/b/c.json",
        "results/../x/y.json",
        "/results/a/b.json",
    ],
)
def test_a_path_outside_results_is_refused(tmp_path, line):
    repo = _repo(tmp_path, [line], {})
    with pytest.raises(table.NotPublishable):
        table.read_allowlist(repo)


def test_a_missing_or_repeated_file_is_refused(tmp_path):
    rel = "results/mcp_refund/x.json"
    with pytest.raises(table.NotPublishable, match="no such file"):
        table.read_allowlist(_repo(tmp_path / "a", [rel], {}))
    with pytest.raises(table.NotPublishable, match="twice"):
        table.read_allowlist(_repo(tmp_path / "b", [rel, rel], {rel: _run("mcp_refund")}))


def test_a_file_that_is_not_a_run_is_refused(tmp_path):
    rel = "results/model_retry/export.json"
    repo = _repo(tmp_path, [rel], {rel: {"rows": []}})
    with pytest.raises(table.NotPublishable, match="not a run file"):
        table.load_runs(repo)


def test_rows_keep_machines_and_conditions_apart():
    def run(os_name: str, timeout: float) -> dict:
        trial = {"transport": "t", "fault": "none", "arm": "no_dedup", "effects_committed": 1,
                 "client_saw_success": True, "verdict": "correct", "order_id": "o"}  # fmt: skip
        return {"file": "results/mcp_refund/x.json", "experiment": "mcp_refund",
                "parameters": {"client_timeout_s": timeout, "fault_delay_s": 5.0},
                "environment": {"platform": f"{os_name}-x"}, "trials": [trial]}  # fmt: skip

    rows = table.build_rows(
        [run("Linux", 2.0), run("Windows", 2.0), run("Linux", 0.5)], per_cell=True
    )
    assert len(rows) == 3
    assert all(r.trials == 1 and r.differs == 0 for r in rows)
