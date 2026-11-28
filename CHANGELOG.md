# Changelog

## Unreleased

- **`crashtest serve --mcp-port N` and `--sink-port N`** fix the MCP server's HTTP port and the sink's port, so an HTTP client's configuration (VS Code, Cursor, Claude Code over HTTP) survives a restart. Without them, ports are free ones, as before. A port that is in use, out of range, or given for both is refused before anything starts (exit code 2, with the reason).
- **`crashtest serve` prints the test controller's command** beside the agent-facing server, labelled "for you, never for the agent".
- **A third scenario, `refunds-cut-mid-stream`** (experiment 008's `mid_stream` cut): the tool sends a progress notification and pauses; a proxy lets the notification through and cuts before the response; the server (stateful SSE, no event store, stated in the scenario) cancels the tool, so nothing is refunded. `crashtest run refunds-cut-mid-stream` and `crashtest selftest --scenario refunds-cut-mid-stream` (both example agents must PASS with the cut confirmed). The agent's client must ask for progress, or the trial is INCONCLUSIVE with that fix in the report. Its default agent timeout is 10 s. No verdict or checker change.
- **The test controller runs `refunds-cut-mid-stream`.** `crashtest serve` now also starts the mid-stream MCP server and lists every server's URL under `mcp_urls` in `.crashtest-stack.json` (`mcp_url` is unchanged). With a stack file from an older `serve`, `create_test_run` says to restart it.
- **The Action takes `scenario: refunds-cut-mid-stream`.** `scenario: all` is unchanged (refunds and the cut before the request), so existing builds do not change.
- **The example agents ask for progress** on `issue_refund` (a no-op `progress_callback`), which the mid-stream scenario needs; the other scenarios' servers send no progress, so their results are unchanged.
- `crashtest run --client-timeout` now defaults to the scenario's value (2 s, as before, for the first two scenarios).
- **`crashtest serve` survives a slow PostgreSQL recovery.** After an unclean shutdown (a closed terminal, sleep or power-off), PostgreSQL first recovers its data directory, which on Windows can take longer than the 10 s that pgserver gives `pg_ctl start`; `serve` then failed with a `TimeoutExpired` traceback while the server kept starting in the background. It now says it is waiting and connects once PostgreSQL is ready (up to 3 minutes). Found in a manual Claude Desktop trial.
- **Claude Desktop: verified by hand on Windows, one trial.** Claude Desktop (Microsoft Store) launched `crashtest mcp --stdio`, listed both tools and called `issue_refund`. The guide now gives the Store version's config location, says to merge `mcpServers` into a file that already has settings, and to switch the tool on in the chat. The trial's result, with what it does not show, is in `docs/clients/claude-desktop.md`. macOS has not been run.
- A slow-response scenario was considered and not added: `refunds` already is one (docs/scenarios.md, docs/decisions.md).

## 0.3.1 (27 November 2026): the Action runs both scenarios, honest PASS reports, quieter output

A patch release. No verdict, checker or contract change, and no change of result or exit code for any run: every trial is still judged from the sink's rows by `crashtest.checker` (the verdict-property tests check every case).

From a maintainer-side test run of v0.3.0 (not counted as an independent tester report):
- **The Action takes a `scenario` input:** `refunds` (the default, unchanged), `refunds-cut-before-request`, or `all`. With `all` it runs both scenarios one after the other; its `result` is the worst of the two, its `exit-code` the most serious, and `junit-file` lists both reports. CI shows why it matters: a test agent that reports "done" after an error passes `refunds` alone and fails with `all`.
- **A PASS that never sent an idempotency key says so.** When no request in any trial carried a key, the report's recommended-fix section carries a caveat instead of "nothing to fix". In the refunds scenario: the agent avoided the duplicate some other way (for example a lookup), which can race with a commit in a real system. In the cut scenario: nothing was committed before the retry. The caveat is in all four formats (`caveats` in JSON, a `caveat` property in JUnit).
- **`crashtest selftest`'s passing line no longer says your agent's result "can be trusted".** It now says the harness's counts are reliable on this machine, and that a PASS covers only the scenario.
- **The connection cut no longer prints a traceback.** In `refunds-cut-before-request` the refund MCP server used to log a full `ClientDisconnect` traceback ("Exception in ASGI application") on every trial, which read like a crash. It now logs one line: the client disconnected before its request was read, and no tool was called. Other errors keep their traceback.

Also:
- **`crashtest --version`** prints the installed version.
- **Reports say what ran the test**: the JSON report has `report_schema` (the layout's version; keys are only added within a version) and `environment` (the agent-crashtest, `mcp` and Python versions, the OS, its release and the machine type), and the JUnit report has the same as `env_*` properties. No host name, user name, path or environment variable is recorded. The text and Markdown reports are unchanged.
- **A stack file left by a killed `crashtest serve` is cleaned up.** `serve` now records its process ID in `.crashtest-stack.json`; when a later command finds that the process is gone, it removes the file and says so on stderr, instead of using URLs that point at nothing.
- **CI runs the full test suite on Windows and macOS**, not only on Ubuntu.
- Docs: a status lookup before retrying (`check_refund_status`) is no longer presented as equal to an idempotency key; the README, `docs/scenarios.md`, `docs/connect-your-client.md` and the example say it can race in a real system. The README's limitations say what a PASS does and does not show, and that the full suite runs in CI on all three systems.
- New: a **Tester report** issue form for sharing any result, pass or fail: self-test only or a completed run, both scenarios' results, setup, MCP client or framework, full output, suggestions, and permission to quote.
- New: `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), a pull-request template, [docs/testing.md](docs/testing.md), [docs/RELEASING.md](docs/RELEASING.md) and [docs/decisions.md](docs/decisions.md). `CONTRIBUTING.md`'s good first issues are brought up to date.
- For maintainers: `tools/triage_report.py` reads the issues labelled `tester-report` and prints the tracker, applying the counting rules and checking that the pasted output supports the chosen result. `.github/workflows/reproduce.yml` (started by hand) installs a release on a chosen OS and Python version and runs the self-tests and both example agents. Neither posts, labels or closes anything; see [docs/testing.md](docs/testing.md).

**Compatibility.** Nothing is removed and no default changes. If you parse output:
- `crashtest selftest`'s passing line has new wording (exit code 0, as before).
- A PASS report's recommended-fix section can show the caveat where it said "nothing to fix in this scenario" (text and Markdown).
- The JSON report has a new `caveats` key (a list, empty unless the caveat applies) under the same `report_schema`; keys are only ever added within a schema version.
- JUnit reports can carry a new `caveat` property.

## 0.3.0 (25 November 2026): connect over stdio, a control token, a test controller, a second scenario

Easier to connect, harder for the agent under test to tamper with, and a second fault taken from experiment 008. No verdict, checker or contract change: every trial is still judged from the sink's rows by `crashtest.checker`, and the reference scenario's reports are byte-identical to 0.2.1's apart from the additive run metadata below.

- **`crashtest mcp --stdio`** serves the same refund MCP server over stdio, for clients that launch a command (Claude Desktop, IDEs, the MCP Inspector). It finds the running sink from `--sink-url`, `CRASHTEST_SINK_URL` or the stack file (`--stack-file`), and exits 2 with a message on stderr when no sink answers. CI checks on Ubuntu, Windows and macOS that the same agent gets the same verdict over stdio and over HTTP.
- **A control token on the sink.** `/control/*` (arming faults, reset) now requires `Authorization: Bearer <token>`; `GET /refunds` (the read-only ground truth) stays open. `crashtest serve` generates the token and stores it in `.crashtest-stack.json`, never in MCP output; `arm`, `run`, `selftest`, the Action and the experiment runners pass it automatically, and agents started by `crashtest run` get an environment without it. **Docker Compose now requires `CRASHTEST_CONTROL_TOKEN`** to be set before `docker compose up`; scripts or `curl` commands that call `/control/*` directly must send the token.
- **DNS-rebinding protection** is on by default for the HTTP MCP server: the `Host` and `Origin` headers must name `127.0.0.1`, `localhost` or `[::1]` (421 / 403 otherwise). Docker keeps it on and lists its container names in `MCP_ALLOWED_HOSTS`.
- **Client guides** in `docs/clients/`, each marked with its verification status: the Python `mcp` client and the MCP Inspector's CLI are verified in CI (a new `inspector` job); Claude Desktop, Claude Code, VS Code, Cursor and the Inspector's web UI are not yet verified.
- **`crashtest controller --stdio | --http`**: a separate MCP server for the developer, never for the agent, with `create_test_run`, `configure_scenario`, `arm_fault`, `get_run_status` and `get_test_report`. No reset or delete tool; token-protected over HTTP; verdicts only from the sink through the same checker and report code as `crashtest run` (a regression test checks both give the same result, verdicts and counts). `docs/controller.md`.
- **Scenarios.** `crashtest run <scenario>` and `crashtest selftest --scenario <scenario>`: `refunds` (the reference, unchanged) and **`refunds-cut-before-request`** from experiment 008: a proxy cuts the agent's first call before it reaches the server, so nothing is refunded, yet the agent sees the same error as after a cut that follows the commit. Ground truth: the sink's rows plus the proxy's record, reported per trial as `fault_record`. `docs/scenarios.md` has its results table and limitations.
- **Reports** carry a run ID, the scenario's name and version, and which driver produced them (`crashtest run` or the controller): additive JSON keys and JUnit properties, and one extra line in the text and Markdown reports.
- **Framework examples** in `examples/frameworks/`: a Pydantic AI agent and a LangGraph agent, each driven by a scripted model (no API key), checked in CI (naive FAIL, stable key PASS). Frameworks that need a model key are not included.
- **A read-only lookup tool**, contributed in PR #15: `check_refund_status(order_id)` on the refund MCP server says whether a refund exists, changing nothing, and `examples/check_status_agent.py` looks the refund up after a timeout before retrying.
- `subjects/requirements/crewai.txt` warns that it pins a chromadb version with open critical and high advisories, used only to reproduce experiment 003.

## 0.2.1: the Docker images build again

The three Dockerfiles copy `examples/`, which the 0.2.0 package build needs; `docker compose up` failed at v0.2.0. No other change: `pip install`, `crashtest run refunds`, `crashtest selftest` and the GitHub Action behave as in 0.2.0. The README's install command and Action usage now name `@v0.2.1`.

## 0.2.0: crashtest run refunds, a PASS/FAIL report and a GitHub Action

One command now tells a developer whether their agent refunds twice when a tool's response is lost, and why. No experiment, results file, verdict or contract text changed; the server, sink, checker and the 0.1.0 commands are reused as they were.

- **`crashtest run refunds --agent naive|stable_key|PATH`** starts the local stack, arms the lost-response fault for a fresh order per trial (`--trials`, default 5), runs the agent once per order and prints the report.
- **`crashtest selftest`** checks the harness itself before you trust a result about your own agent: it runs `crashtest run refunds` with both example agents as controls (3 trials each) and passes only if the naive agent FAILs with a duplicate and the fault confirmed in every trial and the stable-key agent PASSes. Exit 0 when both controls behave, 1 when one misbehaves (the harness is at fault, not your agent), 2 for an environment error. No new verdicts.
- **Two example agents** in `examples/`, both short scripts on the plain `mcp` client and templates for your own: `naive_retry_agent.py` retries with no idempotency key (FAIL), `stable_key_agent.py` sends one key per order (PASS). They are installed with the package.
- **The report** keeps three parts apart: what was observed (from the sink and the harness), the likely cause (labelled as an inference) and the recommended fix. PASS means every trial's verdict from the checker is `correct`. Every report states what was tested (one simulated refund tool over MCP and one fault, a lost response), that the tool is a test double writing only to a local PostgreSQL, and the result as "N of N trials": repeatability under the stated configuration, not a rate. `--format text|json|markdown|junit`, `--output`, `--junit-file`, `--markdown-file`.
- **Exit codes:** 0 PASS; 1 a duplicate or lost refund; 2 an environment or configuration error; 3 the fault could not be confirmed. When the sink is correct but the agent's report contradicts it, the run shows WARN and exits 0, or 1 with `--fail-on-report-mismatch`.
- **A GitHub Action** (`action.yml`): `uses: munendra07-tech/agent-crashtest@v0.2.0` with `agent`, `trials` and `fail-on-duplicate-effect`. It writes a JUnit report and the Markdown report to the job summary, and fails on a duplicate refund.
- **CI** installs the package from scratch on a clean Ubuntu runner and runs `crashtest selftest` and both example agents (FAIL, then PASS), and runs the Action itself on both.
- **Tests:** the five connection-drop tests in `tests/test_client_matrix.py` that raced the client's timeout against the proxy's cut now give the client a 10-second timeout, so the cut always comes first. New tests cover `crashtest run refunds`, its formats and exit codes.
- **The README is a landing page** (what, why, quickstart with expected output, how it works, the Action, limitations); the research results follow, unchanged. Issue templates (bug, question, "my agent failed"), good first issues in `CONTRIBUTING.md`, and Dependabot for pip and GitHub Actions.

## 0.1.0: the crash-test connector

A developer can now install agent-crashtest and crash-test their own MCP client in four commands. No experiment, results file or contract text changed.

- **`crashtest serve`** starts PostgreSQL, the sink and the MCP server with its fake `issue_refund` tool on this machine, and prints their URLs.
- **`crashtest arm ORDER_ID`** makes the next refund for an order lose its response: the sink commits it, then holds the response past the client's timeout.
- **`crashtest verdict ORDER_ID`** reads the sink and prints how many refunds were committed, the checker's verdict, and whether the client's report agrees with the sink. It exits 0 only for `correct`.
- **`pip install "agent-crashtest[local]"`**: the `local` extra installs a pip-packaged PostgreSQL, so no Docker is needed.
- The README opens with a "Use it" section, and `docs/connect-your-client.md` leads with the commands.

## Fifth public snapshot

Documentation only: the local MCP refund server as a connector a developer can run. No code, experiment, results file or contract text changed.

- `docs/connect-your-client.md` says the refund tool is a test double that moves no real money and that everything runs locally, and opens with the two examples (an unsafe retry with no key, and a stable key) with their commands and the sink's verdicts. Both commands were re-run on 24 November 2026 with the documented counts.
- The README's "Try it" section names the local MCP refund server and links the guide.

## Fourth public snapshot

A results table and documentation fixes. No experiment, results file, verdict or contract text changed.

**Results tables.** `python -m crashtest.results.table --write` writes a summary table into the README (one row per experiment, machine and run conditions) and every cell into `docs/results.md`, from the run files listed in `results/PUBLIC_RUNS.txt` and only for experiments 001 to 008. Verdicts are re-derived with the current checker, taking every refund's amount as correct (amounts can be checked only against the sink database, with `python -m crashtest.results.reverdict`); they differ from the recorded verdicts in 0 trials. Experiment 006 shows its recorded verdicts and links its hand-checked corrections. Rows never pool machines or run conditions. A test checks that both tables match the results files.

**Documentation.** Status updates to `docs/architecture.md` (the result store and the DBOS subject exist), a dated note on `docs/research-gaps.md`, a status note on section 4 of the implementation gap report, the arXiv replication marked not scheduled in the roadmap, and in the README a note that 008 did not test a silent drop and a date on the 19-test check.

## Third public snapshot

Experiment 008, contract version 0.4, and `sink_retries` for experiment 003. No earlier verdict or results file changed.

**Experiment 008: a connection cut before the request, and mid-stream.** The write-up, its pre-registered plan, the runner (`src/crashtest/experiment/cut_points.py`), its tests (`tests/test_cut_points.py`), the experiment workflow option, and the two runs of record (Windows and Linux, 480 trials each; every one of 48 cells matched its prediction on both). The proxy gains two cut points, `before_request` and `mid_stream`; the MCP server can send a progress event and pause before its effect; the local stack serves two stateful SSE servers that do.

**Contract version 0.4.** `fault_confirmed` now means the fault fired where its fault point says, decided for every connection drop by `DropRecord.landed_as_intended`. For 008's two new cuts that is before the commit. For experiments 001 to 007 every fault point is after the commit, so no earlier value or verdict changes (`docs/audits/contract-v0.4-review.md`).

**`sink_retries` for experiment 003.** Its records now carry the field, as 002 and 007's do. A new test fails the proxy's first sink read and checks that a 003 trial counts the retry. Earlier 003 files do not have it.

**Evidence: 008 and the proxy change.** 008's runs of record used the proxy from before the second snapshot's fix. Two re-runs on the maintainer's development tree, one after each of the proxy's two changes, matched every one of the 48 cells at 5 trials per cell (the runs of record used 10). That shows no outcome changed at that sample size, not a rate. In the second re-run, two trials' second attempts hung for 130.8 s and 40.4 s while the laptop was in Windows Modern Standby; their outcomes were unaffected. The re-runs are not published (`PROVENANCE.md`).

## Second public snapshot

A harness fix and tests. No experiment, result or contract text changed: `docs/contract.md` is still v0.3, and every results file is unchanged.

**Fixed: the fault proxy's reads of the sink.** The connection-drop proxy (`src/crashtest/faults/drop_proxy.py`) waits for a refund to commit before it cuts a connection, by polling the sink.
- It now keeps one HTTP client to the sink for its whole life. Before, it created a client for each commit wait; that took about 150 ms, and up to about 750 ms on a loaded machine, and could lose the race with a client's timeout.
- That client drops idle connections after 1 second (`SINK_KEEPALIVE_S`), before the sink's server does (5 seconds). Without this, a read could reuse a connection the server had just closed.
- A read that fails at the connection level is retried once. This is a second race, between a read and the sink closing an idle connection; it was found by code review, not seen in a run. A timeout or an HTTP error status is not retried, so a slow or failing sink still shows as one.

**New trial field: `sink_retries`.** In the trial records of experiments 002 and 007: the number of sink reads the proxy retried during the trial, or `null` when the trial ran without the proxy. Results files written before this change do not have the field, and experiment 003's records (`single_call`) do not carry it yet, although its trials go through the proxy.

**Tests.**
- `tests/test_drop_proxy_sink.py`: the retry, what is not retried, the idle expiry, and a read across the expiry against the real sink.
- `tests/test_runner_smoke.py`: one trial through the 003, 004 and 005 runners' real code paths. The 004 and 005 tests run only when their subject environments are named (see the module's docstring).
- `tests/test_sink.py`: the fault-delay test allows 50 ms of slack, because a Windows timer can fire up to one clock tick (about 15.6 ms) early.

**Evidence.** Re-runs of experiments 002 and 007 with this fix, on the maintainer's development tree, matched their recorded outcomes in every cell, at 5 trials per cell (the recorded runs used 20). That shows no outcome changed at that sample size; it does not show either race is fixed, and rare changes are not ruled out. The evidence for the keep-alive fix is `tests/test_drop_proxy_sink.py` (the retry, what is not retried, and the idle expiry). No test measures the client-creation delay directly.
