# Run from a clone

For working on agent-crashtest itself, or running the experiments: from a clone of the repository instead of `pip install` from a tag. To test your own agent you don't need any of this; see the [README](../README.md). This was part of the README until 28 November 2026; the text is unchanged. The checks CI runs are explained in [testing.md](testing.md).

## Run it on Windows (PowerShell)

Requires Python 3.12. Keep the project at a short path such as `D:\agent-crashtest`; Windows path-length limits break virtual environments in deeply nested folders.

**If PostgreSQL fails to start with "could not create directory ... Invalid argument", "The current directory is invalid" or `postgres.exe` not found:** keep the folder that holds the data directory (`--data-dir`, default `.crashtest-pgdata` in the current folder) and the virtual environment on a path made only of characters in your system's ANSI code page, in practice plain ASCII (for example `C:\work\crashtest`, not under a user profile whose name contains other characters). The cause is upstream: `pgserver`'s bundled PostgreSQL `initdb` does not handle such paths on Windows; it is not specific to this project. For the test suite, pass `pytest --basetemp=C:\tmp\ct` (or set `TEMP`) to an ASCII path.

Set up once:

```powershell
python -m venv .venv
```

```powershell
.\.venv\Scripts\Activate.ps1
```

```powershell
pip install -e ".[dev]"
```

Run the tests (starts PostgreSQL and both services automatically, about 20 seconds):

```powershell
pytest
```

Run the experiment:

```powershell
python -m crashtest.experiment.run --local --trials 20
```

Run it across all four MCP transport settings (about 13 minutes):

```powershell
python -m crashtest.experiment.run --local --transports all --trials 20
```

Results are written to `results\mcp_refund\`.

In VS Code: open the folder, choose the interpreter at `.venv\Scripts\python.exe` (Ctrl+Shift+P → "Python: Select Interpreter"), and use the Testing panel to run the tests.

If activation is blocked by execution policy, run this once in the same window:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

### With Docker

Verified on 16 November 2026 (Docker Desktop 4.29, PostgreSQL 16.16): the stack builds and the 19 tests of that day pass against it. The suite has grown since; CI runs it against the Docker stack on every push.

The sink requires a control token on `/control/*`, and Compose refuses to start without one. Set it first, in the same shell you will run the tests or `crashtest arm` from:

```powershell
$env:CRASHTEST_CONTROL_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
```

```powershell
docker compose up --build -d
```

```powershell
python -m crashtest.experiment.run --sink-url http://localhost:8101 --mcp-url http://localhost:8102/mcp
```

To run the tests against the Docker stack:

```powershell
$env:CRASHTEST_SINK_URL = "http://localhost:8101"; $env:CRASHTEST_MCP_URL = "http://localhost:8102/mcp"; pytest
```

```powershell
docker compose down -v
```

### On Linux or macOS

The same steps, in a POSIX shell. CI installs and runs the tests this way on Ubuntu (Python 3.12) on every push, and the experiment workflow (`.github/workflows/experiment.yml`) has run the experiments on Ubuntu. On macOS, CI runs the clean install, `crashtest selftest` and both example agents (all passed on `macos-latest`); the full test suite and the experiments have not been run on macOS.

```sh
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest
python -m crashtest.experiment.run --local --trials 20
```

`--local` starts PostgreSQL from the pip-installed `pgserver` package, plus the sink and MCP server, so Docker is not needed.

### Checks

The same checks run in CI on every push: see [CONTRIBUTING.md](../CONTRIBUTING.md).

```powershell
ruff check src tests; ruff format --check src tests; mypy; pytest --cov
```

**Connection-drop tests and timing.** Five connection-drop tests in `tests/test_client_matrix.py` (the naive-retry test on each of the four transports and the stable-key test on stateful SSE) used to depend on the client's 1.5-second timeout (`FAST` in that file) racing the proxy's cut. On 21 November 2026 they failed once together in a full local run on a loaded Windows machine: the client timed out before the proxy cut the connection. The proxy now keeps one client to the sink for its whole life instead of creating one per commit wait, expires that client's idle connections after 1 second (before the sink's server closes them, after 5 seconds), and retries a sink read once if its connection was dropped. Trials of experiments 002, 003, 007 and 008 record those retries as `sink_retries` (results files written before the field existed, including 008's two runs of record and every 003 file, do not have it); `tests/test_drop_proxy_sink.py` covers the retry and the expiry. Since 0.2.0 the connection-drop tests give the client a 10-second timeout (`DROP` in that file). The proxy cuts the connection as soon as it sees the commit in the sink, so the cut, not the timeout, ends the client's wait, and the tests take no longer than before. Checked by delaying the proxy's commit check by 2 seconds on purpose: with the old timeout all 8 cases of these two tests (four transports each) failed, with the new one all 8 passed.

## Layout

| Path | Contents |
|---|---|
| `src/crashtest/sink/` | Sink service and its SQL schema |
| `src/crashtest/refund_mcp/` | MCP server with the `issue_refund` tool |
| `src/crashtest/faults/` | Fault proxy that cuts a connection after the commit, before the request, or mid-stream |
| `src/crashtest/experiment/` | Trial logic, runners, local stack launcher |
| `subjects/` | One script per system under test; each runs in its own Python environment |
| `tests/` | Sink tests, end-to-end MCP tests, unit tests |
| `results/` | Raw results files |
| `docs/` | Contract, architecture, experiments, related work, evidence check, research gaps |
