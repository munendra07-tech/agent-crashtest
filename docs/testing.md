# Testing guide

How to run the project's own tests, and what they cover. To test *your agent* with the tool, see the README's quickstart instead.

## Set up

```bash
python3.12 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
```

On Windows: `py -3.12 -m venv .venv`, then `.venv\Scripts\activate`. The `dev` extra brings pytest, ruff, mypy and a pip-installable PostgreSQL (`pgserver`), so no Docker is needed. On Windows, keep the checkout in a folder whose path has only ASCII characters: the bundled PostgreSQL tools fail on other paths.

## Run the checks

The same four as CI's `lint` and `test-local` jobs:

```bash
ruff check src tests examples
ruff format --check src tests examples
mypy
pytest
```

The full suite takes a few minutes, because many tests start their own PostgreSQL, sink and MCP server on free ports. Useful variants:

| Command | What it does |
|---|---|
| `pytest tests/test_verdict_properties.py` | Only the verdict guard: every verdict class, aggregation rule and exit code |
| `pytest -k cli` | Only tests whose names match |
| `pytest --basetemp <dir>` | Put the tests' temporary folders (PostgreSQL data among them) in `<dir>`, for example a drive with room or an ASCII-only path |
| `pytest --cov` | With coverage, as CI does |

Against the Docker Compose stack instead of the local one (Linux or macOS shell):

```bash
export CRASHTEST_CONTROL_TOKEN=local-test-token
docker compose up --build -d --wait
CRASHTEST_SINK_URL=http://localhost:8101 CRASHTEST_MCP_URL=http://localhost:8102/mcp pytest
docker compose down -v
```

## What the suite covers

| Area | Tests |
|---|---|
| Verdicts and results (PASS, WARN, FAIL, INCONCLUSIVE; exit codes 0 to 3) | `test_verdict_properties.py`, `test_checker.py`, `test_run_refunds.py` |
| The CLI (`serve`, `arm`, `verdict`, `run`, `selftest`, `--version`, stale stack files) | `test_cli.py`, `test_run_refunds.py` |
| Scenarios (`refunds`, `refunds-cut-before-request`) | `test_scenarios.py`, `test_cut_points.py`, `test_drop_proxy_sink.py` |
| The MCP server over stdio and HTTP, the control token, DNS-rebinding protection | `test_mcp_refund.py`, `test_connect_security.py` |
| The test controller | `test_controller.py` |
| The sink (the ground truth) | `test_sink.py` |
| Framework examples (Pydantic AI, LangGraph; scripted models) | `test_framework_examples.py`, skipped unless `CRASHTEST_EXAMPLE_PYDANTIC_AI_PYTHON` / `CRASHTEST_EXAMPLE_LANGGRAPH_PYTHON` point at a Python with that example's requirements (`examples/frameworks/requirements-*.txt`) |
| Experiment runners and results tables | `test_runner_smoke.py`, `test_crash_runners.py`, `test_results_*.py` and the rest |

## What CI runs

On every push and pull request (`.github/workflows/ci.yml`):
- lint and type checks;
- the full suite on Ubuntu, Windows and macOS;
- the full suite against the Docker stack;
- a clean (non-editable) install with both scenarios' selftests on all three systems;
- the MCP Inspector's CLI against the server;
- the framework examples;
- the GitHub Action.

Runs on pull requests from first-time contributors wait for the maintainer's approval.

## Rules for tests

- A verdict change must change `tests/test_verdict_properties.py` on purpose, in the same pull request, with the reason in its description.
- Tests judge from the sink's rows, as the tool does, never from what an agent reports.
- No network access beyond `127.0.0.1`, no API keys and no real payment systems. Framework examples use scripted models.
- Don't skip a failing test to get CI green: fix it, or explain it in the pull request.
