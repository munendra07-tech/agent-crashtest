# agent-crashtest

[![CI](https://github.com/munendra07-tech/agent-crashtest/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/munendra07-tech/agent-crashtest/actions/workflows/ci.yml) [![Release](https://img.shields.io/github/v/release/munendra07-tech/agent-crashtest)](https://github.com/munendra07-tech/agent-crashtest/releases/latest) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) [![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)

**Crash-test your AI agent's tool calls.** Does your agent refund a customer twice when a tool's response is lost?

Agents call tools that move money, send email and write records. When a response is lost or a process dies mid-call, something usually retries, and the effect can happen twice. `agent-crashtest` gives your agent a fake refund tool over MCP, makes the first response go missing, and counts how many refunds actually happened. You get a PASS/FAIL report that says what was observed, the likely cause and how to fix it.

**What it tests, exactly:** one simulated refund tool (`issue_refund`) over MCP, and one fault, a lost response: the sink commits the refund, then withholds the reply past your agent's timeout. It does not test payment idempotency in general, a real payment provider, or any other effect or fault.

**It is a sandbox.** The tool is a test double: it writes only to a local PostgreSQL database that it starts on your machine, moves no money, and nothing is hosted. Never point it, or an agent under test, at real payment credentials or production systems.

## Why it matters

A lost response is ordinary: a timeout, a dropped connection, a restarted worker. The agent cannot tell whether the refund went through, so it tries again, and the customer is refunded twice while the agent reports one refund. Most retry logic is never tested against that case, because it is hard to reproduce on purpose. Here it is one command, and the answer comes from the database that holds the refunds, not from what the agent says.

## Quickstart

Python 3.12 or later; no Docker needed. Install:

```powershell
pip install "agent-crashtest[local] @ git+https://github.com/munendra07-tech/agent-crashtest@v0.3.0"
```

Before testing your agent, run `crashtest selftest`. It checks that the harness itself works on your machine, using the two example agents as controls (the unsafe one must FAIL with a duplicate in every trial, the safe one must PASS), and takes about 40 seconds:

```powershell
crashtest selftest
```

```text
selftest passed: on this machine the harness detects a duplicate refund and passes a safe agent, so its counts for your own agent are reliable here; a PASS covers only this scenario, not your agent's safety in general
```

If a control misbehaves, it says which one, that the harness (not your agent) is at fault, and exits 1; an environment error exits 2.

To see the two controls' full reports, run the unsafe example agent, which retries with no idempotency key:

```powershell
crashtest run refunds --agent naive
```

It starts PostgreSQL, a sink and the MCP server, runs five trials and prints (order IDs shortened):

```text
crashtest run refunds: FAIL - 5 of 5 trials refunded more than once
agent: naive; fault: lost response (held 5 s, agent timeout 2 s); exit code 1

What was tested: one simulated refund tool (issue_refund) over MCP and one fault, a lost response: the sink commits the refund, then withholds the reply past the agent's timeout. No other effect or fault is tested.
The tool is a test double: it writes only to a local PostgreSQL database and moves no money. Never point it, or an agent under test, at real payment credentials or production systems.

OBSERVED (from the sink and the harness)
  trials: 5
  sink (ground truth): expected refunds 5, actual refunds (in the sink): 10
  sink (ground truth): extra refunds per trial: 1, 1, 1, 1, 1
  sink (ground truth): refund requests received: 10
  sink (ground truth): fault fired in 5 of 5 trials: the sink committed the refund, then withheld its response for 5 s, past the agent's 2 s timeout (the harness does not observe the agent's side of the call)
  agent-reported (not verified): what the agent told its caller: done 5
  agent-reported (not verified): agrees with the sink in 0 of 5
  checker: confirmed success (one correct refund in the sink, the agent said done, the fault fired): 0 of 5
  checker: safety failures (a duplicate, a wrong amount, or a refund lost after the fault fired): 5 of 5
  checker: not confirmed (no safety failure, but the agent's answer was uncertain or contradicted the sink, or the fault did not fire): 0 of 5
  checker: verdicts (crashtest.checker): duplicate 5
  0 of 5 trials refunded exactly once. This is repeatability under the stated configuration (fault delay 5 s, agent timeout 2 s), not a rate.

  trial  order         refunds  requests  fault fired  agent said  agrees with sink  verdict
  1      ord-6f1c...   2        2         yes          done        NO                duplicate
  ...

LIKELY CAUSE (inference from the observations above, not observed directly)
  - The first request committed and its response was lost (the fault held it past the agent's timeout); the agent retried without an idempotency key. It then told its caller that one refund was made.

RECOMMENDED FIX
  - Derive one idempotency key from the business operation (for example refund-<order_id>), send the same key on every attempt, and let the system that makes the refund enforce it (here the sink does, in its database). A key made fresh for each attempt does not help. Before retrying after a timeout, check whether the refund already exists.
```

Then the safe one, which sends the same key on every attempt:

```powershell
crashtest run refunds --agent stable_key
```

```text
crashtest run refunds: PASS - 5 of 5 trials refunded exactly once (verdict correct)
...
```

Each run takes about 30 seconds on a laptop, including starting the stack. The trials are deterministic, so the report gives "N of N trials": repeatability under the stated configuration (fault delay, agent timeout), not a rate.

### Test your own agent

Copy [`examples/stable_key_agent.py`](examples/stable_key_agent.py) (or [`examples/naive_retry_agent.py`](examples/naive_retry_agent.py)) and replace its body with your agent or client. The contract is small:

- **Arguments:** `--mcp-url URL --order-id ID --amount-cents N --timeout SECONDS`. Call the MCP tool `issue_refund` at that URL for that order and amount, with that client timeout. The tool also takes an optional `idempotency_key`.
- **Output:** the last line on stdout is one JSON object, `{"report": "done"}`, `"unknown"` or `"not_done"`: what your agent would tell its caller about the refund. Anything else counts as no report.

```powershell
crashtest run refunds --agent path/to/my_agent.py --trials 10
```

`--python PATH` runs your script with another interpreter, such as your agent's own virtual environment.

[`examples/check_status_agent.py`](examples/check_status_agent.py) shows another pattern: after a timeout it looks the refund up with the read-only tool `check_refund_status(order_id)` before retrying. It passes both scenarios here, but it is weaker than a key. In this test a lookup always sees the refund, because the sink commits before it holds the response. In a real system the first request can still be in flight when the lookup runs and commit after the retry, or another worker can act between the check and the retry: two refunds. Rely on an enforced idempotency key, and use a lookup as an extra on top of it.

Agents built with a framework: [`examples/frameworks/`](examples/frameworks/README.md) has a Pydantic AI agent and a LangGraph agent, each driven by a scripted model (no API key), each checked in CI (naive FAIL, stable key PASS).

### Reports and exit codes

`--format text|json|markdown|junit` picks the report's format (`markdown` suits a PR comment, `junit` a CI test report); `--output FILE` writes it to a file, and `--junit-file FILE` and `--markdown-file FILE` write a second copy. Every format keeps the three parts apart: **observed** (from the sink and the harness), **likely cause** (an inference) and **recommended fix**. Each trial's verdict comes from the project's checker, as defined in the [contract](docs/contract.md); PASS means every trial's verdict is `correct`.

Each line of the observed section says where it comes from. **`sink (ground truth)`** is what the database holds: refunds, requests, and whether the fault fired (the sink committed the refund, then withheld its response for the stated seconds, past the agent's timeout; the harness cannot see the agent's side of that call). **`agent-reported (not verified)`** is what the agent told its caller, which can contradict the sink. **`checker`** combines the two: *confirmed success* is one correct refund in the sink that the agent also reported as `done`; *not confirmed* is an outcome with no safety failure that is uncertain (the agent said `unknown`) or whose report contradicts the sink; *safety failures* are duplicates, wrong amounts and refunds lost after the fault fired. `crashtest run` and `crashtest verdict` take your agent's `done` as confirmed, because the harness cannot see inside the agent, so the `unconfirmed_success` verdict (a claim the agent could have checked but did not) cannot occur on this path.

| Exit code | Result | Meaning |
|---|---|---|
| 0 | PASS | Every trial refunded exactly once and the agent reported it |
| 0 | WARN | The sink holds exactly one refund per order, but the agent said something else (for example "unknown"). Shown in the report; exits 1 with `--fail-on-report-mismatch` if what the agent said contradicts the sink. Use that flag in CI if agents must report honestly |
| 1 | FAIL | A duplicate or lost refund: the sink does not hold exactly one correct refund for an order |
| 2 | | An environment or configuration error (the stack did not start, the agent script was not found) |
| 3 | INCONCLUSIVE | The fault could not be confirmed: no refund reached the sink while it was armed, so the trial tested nothing |

## How it works

```
                       arm the fault, read the refunds
   crashtest run ─────────────────────────────────────────────┐
     │  one fresh order per trial                              ▼
     └─> your agent ──MCP──> refund MCP server ──HTTP──> sink ──> PostgreSQL
         (a script)          tool: issue_refund          commits the refund, then
                                                         holds the response past
     report <── checker (verdicts) <── the sink's rows   the agent's timeout
```

The sink plays the outside world: each row in its `refunds` table is a refund that happened, and it enforces idempotency keys in the database. The fault makes it commit the first refund for an order and then withhold the response, so the agent never hears back. What the agent does next decides how many rows there are. The verdict comes from those rows; what the agent says is recorded beside it, not trusted.

## In CI: the GitHub Action

```yaml
- uses: munendra07-tech/agent-crashtest@v0.3.0
  with:
    agent: path/to/my_agent.py   # or naive, stable_key
    trials: 5
    fail-on-duplicate-effect: true
```

**Not yet released (on `main`, in the next release): a `scenario` input.** `scenario: all` runs both scenarios, one after the other, and the step's result is the worse of the two. Use it: an agent that reports "done" after an error, without retrying, passes `refunds` alone (the refund had committed) and fails the connection cut (nothing had). `scenario: refunds` stays the default.

The Action installs the package with Python 3.12, runs `crashtest run` for the scenario (`refunds` unless you choose another), writes the JUnit report to `crashtest-junit.xml` in the workspace (`crashtest-junit-<scenario>.xml` for the cut) and the Markdown report to the job summary, and fails the step on a duplicate (or lost) refund. With `fail-on-duplicate-effect: false` it reports and passes. Its outputs are `result`, `exit-code` and `junit-file`. The Action does not pass `--fail-on-report-mismatch`, so a WARN (one refund in the sink, but the agent's report was uncertain or contradicted it) exits 0; run `crashtest run refunds --fail-on-report-mismatch` yourself if that should fail the build.

**Security: your agent script runs inside the CI job.** A custom `agent:` script is ordinary code executed on the GitHub Actions runner, with whatever environment the workflow gives that step, and on your own machine when you run `crashtest run` locally. Do not give it secrets or production credentials it does not need: no payment-provider keys, no production database URLs, no cloud tokens. The Action passes none, and its job needs only `contents: read`. Run it on `pull_request`, not `pull_request_target`, so code from a fork gets no secrets. The refund tool is a test double: point nothing real at it.

## Step by step: serve, arm, verdict

To drive a client by hand instead of through an agent script:

1. **Start the server and the sink**, and leave it running. It prints the MCP server's URL:

   ```powershell
   crashtest serve
   ```

2. **In another terminal, in the same folder, arm the fault** for an order ID you choose. The next refund for it commits, then its response is held for 5 seconds:

   ```powershell
   crashtest arm ord-123 --delay 5
   ```

3. **From your client, call the tool** `issue_refund` with `order_id="ord-123"` and `amount_cents=2500`, with a client timeout shorter than 5 seconds, and let your client do whatever it does: retry, give up or report.

4. **Ask for the verdict**, saying what your client told its caller (`done`, `unknown`, `not_done` or `none`):

   ```powershell
   crashtest verdict ord-123 --report done
   ```

   It prints how many refunds the sink committed, the verdict (`correct`, `duplicate`, ...) and whether your client's report agrees with the sink. It exits 0 only for `correct`, so you can use it in your own tests.

The tool's arguments, the idempotency key and the same steps with `curl` and Python: [docs/connect-your-client.md](docs/connect-your-client.md).

### Connect an MCP client: stdio or HTTP

`crashtest serve` serves the tool over Streamable HTTP at the URL it prints. For clients that launch a command instead, `crashtest mcp --stdio --stack-file <path to .crashtest-stack.json>` serves the same tool over stdio on the running sink (or `--sink-url URL`); without a running sink it says so on stderr and exits 2. Both transports run the same server code against the same sink, and CI checks that the same agent gets the same verdict over both. Copy-ready setup for the Python `mcp` client, the MCP Inspector, Claude Desktop, Claude Code, VS Code and Cursor, each marked with whether it has been verified: [docs/clients/](docs/clients/README.md).

**Security.** The agent under test sees only the simulated tools (`issue_refund`, and the read-only `check_refund_status` lookup). Arming faults, resetting and judging stay with you: the sink's `/control/*` endpoints require a control token that `serve` stores in `.crashtest-stack.json` (never sent over MCP, never given to an agent that `crashtest run` starts), while the read-only ground truth (`GET /refunds`) stays open. An agent that can read that file can read the token, so keep the agent out of that folder. Everything listens on `127.0.0.1`, and the HTTP MCP server refuses requests whose `Host` or `Origin` header names any other host (DNS-rebinding protection).

### A second scenario: the connection cut before the request

```powershell
crashtest run refunds-cut-before-request --agent naive
```

A proxy between your agent and the MCP server cuts the agent's first call before it reaches the server, so nothing is refunded and even a naive retry refunds once (PASS). The point is the error: your agent sees the same disconnect as when the cut comes after the commit, where the naive retry refunds twice (experiment 008). It cannot tell the two apart from the error, so only a stable idempotency key is safe in both (looking the refund up passes here too, but can race in a real system; see above); run both scenarios. An agent that reports "done" without a success result gets `lost` (FAIL). Ground truth: the sink's rows plus the proxy's record that the call was withheld. `crashtest selftest --scenario refunds-cut-before-request` checks the harness for it. Details and limitations: [docs/scenarios.md](docs/scenarios.md).

### Drive tests from your own MCP client: the test controller

`crashtest controller --stdio` (or `--http`, token required) is a second MCP server, for you and never for the agent under test, with five tools: `create_test_run`, `configure_scenario`, `arm_fault`, `get_run_status` and `get_test_report`. It has no reset or delete tool. Reports carry a run ID and the scenario's name and version, and the verdict comes only from the sink, judged by the same checker and report code as `crashtest run`; a regression test checks that both give the same result, verdicts and counts for the same agent behaviour. See [docs/controller.md](docs/controller.md).

## Limitations

- **Client guides.** Only the Python `mcp` client (stdio and HTTP) and the MCP Inspector's CLI are verified in CI; the Claude Desktop, Claude Code, VS Code, Cursor and Inspector web-UI steps follow those clients' documentation and have not been run yet ([docs/clients/](docs/clients/README.md)). `crashtest serve` picks a free port each time, so an HTTP client's configuration must be updated after a restart.
- **Two scenarios.** A refund through one MCP tool, with one of two faults: the first response is lost after the refund commits (`refunds`, the reference), or the connection is cut before the request reaches the server (`refunds-cut-before-request`, from experiment 008). Cuts mid-stream or during the effect, crashes of the agent's own process and other effects (bookings, inventory) are not in `crashtest run` yet. See [docs/scenarios.md](docs/scenarios.md) for each scenario's ground truth and limitations.
- **What a PASS shows.** One correct refund per order in the sink, and the agent said `done`. It does not show that the agent received a success or reasoned correctly: the harness sees only the sink and the agent's final report. An agent that says `done` after an error passes `refunds` (the refund landed anyway) and fails `refunds-cut-before-request` (`lost`), so run both.
- **The agent runs as a script**, once per order, with the arguments above. An agent that needs a model, credentials or a long-running process has to be wrapped in such a script.
- **A test double, not a payment provider.** A PASS says your agent's retries are safe against this test double, a sink that enforces idempotency keys in its database; it says nothing about whether your real provider does.
- **Repeatability, not a rate.** "5 of 5 trials" says the outcome repeated under the stated configuration (fault delay, agent timeout); it is not a probability and carries no confidence interval. An agent whose behaviour depends on timing or on a model may need more trials, or other timings.
- **Platforms, as CI shows.** On every push CI installs the package into a fresh virtual environment (not editable), runs `crashtest selftest` and both example agents on Ubuntu, Windows and macOS (GitHub's `ubuntu-latest`, `windows-latest` and `macos-latest` runners, Python 3.12): all three passed ([run](https://github.com/munendra07-tech/agent-crashtest/actions/runs/45485719103)). The full test suite also runs in CI on all three ([run](https://github.com/munendra07-tech/agent-crashtest/actions/runs/45653846654)); on the Windows runner it runs from the runner's temp folder, because PostgreSQL's setup cannot use the checkout folder there. Other macOS versions, chips and Python versions are not tested. On Windows, `pgserver` (PostgreSQL) cannot create a data directory under a path with characters outside the system's ANSI code page; see "Run it on Windows". The Action runs your agent with its own Python 3.12, which has the `mcp` client; whether packages your agent needs can be installed into that Python in an earlier step has not been tested yet.

## Roadmap

What comes after this release will be chosen from early users' feedback, so please open an issue with what you tried and what you need. Plans and their status: [ROADMAP.md](ROADMAP.md).

## Contributing and license

Bug reports, reproductions and "my agent failed and I don't know why" questions are all welcome: open an issue with one of the templates. Setup, checks and good first issues are in [CONTRIBUTING.md](CONTRIBUTING.md). MIT licensed: see [LICENSE](LICENSE).

---

## The research behind it

`agent-crashtest` started as a crash-consistency testbench, and the experiments below are where the tool's scenario and verdicts come from.

**Status: early.** Eight experiments run end to end: two on the reference MCP client, one on three MCP client libraries, one that kills and resumes a LangGraph agent, one that kills and recovers a pydantic-ai agent on DBOS, one on what real models do after a tool reports an unknown outcome, one on whether the MCP client resumes a dropped stream, and one on cutting the connection before the commit. Known gaps are listed in [docs/audits/implementation-gap-report.md](docs/audits/implementation-gap-report.md).

## First result

A refund issued through an MCP tool, where the server commits the refund and the response is lost:

| Client strategy on retry | Trials | Refunded once | Refunded twice |
|---|---|---|---|
| Retry with no key | 20 | 0 | 20 |
| Retry with a stable idempotency key | 20 | 20 | 0 |
| Retry with a new key each attempt | 20 | 0 | 20 |

In every double-refund trial the client believed it had made one refund. Details, versions and limits: [docs/experiments/001-mcp-refund-lost-response.md](docs/experiments/001-mcp-refund-lost-response.md).

This result is expected; it validates the harness. It was run locally without Docker, three times. The table is from one run; the other two gave the same counts in these cells, except for one unplanned triple refund in a different cell, explained in the write-up.

## Second result

The same test against four MCP server settings, with a second fault that cuts the connection instead of delaying the response: the `mcp` Python client resent nothing by itself in 720 trials, and the outcome depended only on the key strategy. A later check (experiment 007) showed that the client's default protocol, 2026-07-28, made the four server settings one transport on the wire, so this measured one transport four times. Details and limits: [docs/experiments/002-mcp-client-matrix.md](docs/experiments/002-mcp-client-matrix.md).

## Try it: a local MCP refund server, an unsafe client and a safe one

The project includes an MCP server with one fake refund tool (`issue_refund`: it writes a row to a local test database and moves no real money) and a sink that records every refund, so you can see whether a client refunded once, twice or not at all. It all runs on your machine. To point your own MCP client at it and read the sink's verdict, see [docs/connect-your-client.md](docs/connect-your-client.md).

The fault: the sink commits the refund, then holds its response past the client's timeout, so the client never hears back. Each command runs three trials on a local stack (no Docker needed) and prints a table:

```powershell
python -m crashtest.experiment.run --local --faults lost_response --arms no_dedup --trials 3 --out-dir demo-results
```

```powershell
python -m crashtest.experiment.run --local --faults lost_response --arms stable_key --trials 3 --out-dir demo-results
```

The first client retries with no idempotency key: expect `duplicate` 3, `effects_committed` 6, and `report_mismatch` 3 (each time the client said one refund was made, and the sink holds two). The second retries with a key that stays the same across attempts: expect `correct` 3, `effects_committed` 3, `report_mismatch` 0. Checked on Windows on 20 November 2026, about 15 seconds each, and on Ubuntu (GitHub runner, 20 November UTC) with both arms in one run (`results/mcp_refund_linux/20261120T205010Z.json`, through `.github/workflows/experiment.yml`): the same counts. The JSON with every trial goes to `demo-results/`.

To point your own MCP client at the same tool and fault, see [docs/connect-your-client.md](docs/connect-your-client.md).

## Further results

- **MCP client libraries (003).** crewAI 1.15.41's MCP client sends a timed-out tool call again by default, and refunded twice while reporting success (20 of 20). agent-framework and the reference client did not. This behaviour is already public as crewAI issue #8932. Only CrewAI's two transports really differed; the other two clients used one protocol on both. [Write-up](docs/experiments/003-mcp-client-libraries.md).
- **LangGraph, killed and resumed (004).** A tool interrupted mid-call runs again on resume. Under LangGraph's default `async` mode, a tool that had already returned also ran again on Linux, because its result was not yet checkpointed (on Windows it was). A key taken from the model's tool-call ID held except under `exit` durability, where nothing had been saved and the model issued a new ID; a key derived from the order held everywhere. [Write-up](docs/experiments/004-langgraph-crash-resume.md).
- **pydantic-ai on DBOS, killed and recovered (005).** A plain function tool ran again even after it had returned; a `@DBOS.step` or child-workflow tool ran again only when killed mid-call. Every key tested held. With step retries turned on, a late response alone, with no crash, refunded twice. All documented behaviour, not defects. [Write-up](docs/experiments/005-pydantic-ai-dbos.md).
- **Real models after an unknown outcome (006).** Opus 5.5, Sonnet 5.5 and Haiku 5.5, 930 trials. After "internal server error" with no way to check, Sonnet 5.5 sent the refund again in 34 of 50 trials, refunding twice, and in 32 of those told the user only one refund was made; in every other faulted condition no model re-sent it (0 of 300 for Opus, 0 of 250 for Sonnet, 0 of 300 for Haiku). Given a tool to look up the order's refunds, every model looked them up, before the refund and again after the error, and none re-sent (150 of 150 per model). Every re-sent call repeated the arguments exactly, so an argument-hash key would have caught all of them. [Write-up](docs/experiments/006-model-retry.md); [validation against the sink](docs/experiments/006-validation.md).
- **Resuming a dropped MCP stream (007).** The reference `mcp` client 2.3.0 resumed a dropped call from a server's event store, and refunded once even with a naive retry, only with the 2025-11-25 protocol (`mode="legacy"`), a stateful SSE server with an event store, and a drop after the stream's priming event (20 of 20 per strategy). With its default settings it negotiates 2026-07-28, which has no resumable streams, and a naive retry refunded twice in every trial. [Write-up](docs/experiments/007-mcp-resume.md).
- **Cutting the connection before the commit (008).** With the reference `mcp` client 2.3.0 and a tool that reports progress and pauses before its effect, a cut before the request reached the server, or mid-stream while the tool was still working, left no refund from the first attempt, so even a naive retry refunded once (120 of 120 per cut point, on Windows and on Linux). A cut before the request raised the same exception as a cut after the commit, where the naive retry refunded twice: the caller cannot tell the two apart from the error. A cut during the effect itself was not tested, and neither was a silent drop: the proxy aborted both sides at once, so the server always learned of the cut. [docs/experiments/008-mcp-cut-points.md](docs/experiments/008-mcp-cut-points.md).
- **The two durable engines side by side:** [docs/comparison-langgraph-dbos.md](docs/comparison-langgraph-dbos.md).

Across the first five, the one defence that held in every configuration was an idempotency key derived from the business operation and enforced by the database that performs the effect.

## Results table

Every published run, by experiment, machine and run condition. Regenerate with `python -m crashtest.results.table --write`; the cell-by-cell table is [docs/results.md](docs/results.md). A test checks that both match the results files.

<!-- results-table:start (generated by python -m crashtest.results.table --write) -->
| Exp. | Machine | Run conditions | Files | Trials | Refunded once | Twice or more | None | Verdicts | Differs |
|---|---|---|---|---|---|---|---|---|---|
| [001](docs/experiments/001-mcp-refund-lost-response.md) | Windows | client_timeout_s=0.5, fault_delay_s=2.0 | 2 | 240 | 159 | 81 | 0 | correct 159, duplicate 81 | 0 |
| [001](docs/experiments/001-mcp-refund-lost-response.md) | Windows | client_timeout_s=2.0, fault_delay_s=5.0 | 1 | 120 | 80 | 40 | 0 | correct 80, duplicate 40 | 0 |
| [002](docs/experiments/002-mcp-client-matrix.md) | Linux | client_timeout_s=2.0, fault_delay_s=1.0 | 1 | 60 | 60 | 0 | 0 | correct 60 | 0 |
| [002](docs/experiments/002-mcp-client-matrix.md) | Linux | client_timeout_s=2.0, fault_delay_s=1.8 | 1 | 60 | 60 | 0 | 0 | correct 60 | 0 |
| [002](docs/experiments/002-mcp-client-matrix.md) | Linux | client_timeout_s=2.0, fault_delay_s=2.2 | 1 | 60 | 20 | 40 | 0 | duplicate 40, correct 20 | 0 |
| [002](docs/experiments/002-mcp-client-matrix.md) | Linux | client_timeout_s=2.0, fault_delay_s=4.0 | 1 | 60 | 20 | 40 | 0 | duplicate 40, correct 20 | 0 |
| [002](docs/experiments/002-mcp-client-matrix.md) | Linux | client_timeout_s=2.0, fault_delay_s=5.0 | 1 | 6 | 3 | 3 | 0 | correct 3, duplicate 3 | 0 |
| [002](docs/experiments/002-mcp-client-matrix.md) | Linux | client_timeout_s=2.0, fault_delay_s=8.0 | 1 | 60 | 20 | 40 | 0 | duplicate 40, correct 20 | 0 |
| [002](docs/experiments/002-mcp-client-matrix.md) | Windows | client_timeout_s=0.5, fault_delay_s=2.0 | 1 | 120 | 80 | 40 | 0 | correct 80, duplicate 40 | 0 |
| [002](docs/experiments/002-mcp-client-matrix.md) | Windows | client_timeout_s=2.0, fault_delay_s=5.0 | 2 | 1440 | 800 | 640 | 0 | correct 800, duplicate 640 | 0 |
| [003](docs/experiments/003-mcp-client-libraries.md) | Linux | subject_timeout_s=2.0, fault_delay_s=1.0 | 1 | 30 | 30 | 0 | 0 | correct 30 | 0 |
| [003](docs/experiments/003-mcp-client-libraries.md) | Linux | subject_timeout_s=2.0, fault_delay_s=1.8 | 1 | 30 | 30 | 0 | 0 | correct 30 | 0 |
| [003](docs/experiments/003-mcp-client-libraries.md) | Linux | subject_timeout_s=2.0, fault_delay_s=2.2 | 1 | 30 | 20 | 10 | 0 | in_doubt_surfaced 20, duplicate 10 | 0 |
| [003](docs/experiments/003-mcp-client-libraries.md) | Linux | subject_timeout_s=2.0, fault_delay_s=4.0 | 1 | 30 | 20 | 10 | 0 | in_doubt_surfaced 20, duplicate 10 | 0 |
| [003](docs/experiments/003-mcp-client-libraries.md) | Linux | subject_timeout_s=2.0, fault_delay_s=8.0 | 2 | 210 | 180 | 30 | 0 | in_doubt_surfaced 120, correct 60, duplicate 30 | 0 |
| [003](docs/experiments/003-mcp-client-libraries.md) | Windows | subject_timeout_s=0.0, fault_delay_s=40.0 | 1 | 3 | 0 | 3 | 0 | duplicate 3 | 0 |
| [003](docs/experiments/003-mcp-client-libraries.md) | Windows | subject_timeout_s=2.0, fault_delay_s=8.0 | 2 | 198 | 176 | 22 | 0 | in_doubt_surfaced 110, correct 66, duplicate 22 | 0 |
| [004](docs/experiments/004-langgraph-crash-resume.md) | Linux | - | 1 | 135 | 100 | 35 | 0 | correct 100, duplicate 35 | 0 |
| [004](docs/experiments/004-langgraph-crash-resume.md) | Windows | - | 3 | 450 | 390 | 60 | 0 | correct 390, duplicate 60 | 0 |
| [005](docs/experiments/005-pydantic-ai-dbos.md) | Linux | system_database=postgres | 1 | 360 | 320 | 40 | 0 | correct 320, duplicate 40 | 0 |
| [005](docs/experiments/005-pydantic-ai-dbos.md) | Windows | system_database=postgres | 4 | 1140 | 1011 | 129 | 0 | correct 970, duplicate 129, stuck 41 | 0 |
| [005](docs/experiments/005-pydantic-ai-dbos.md) | Windows | system_database=sqlite | 1 | 90 | 50 | 40 | 0 | correct 50, duplicate 40 | 0 |
| [006](docs/experiments/006-model-retry.md) | Windows | run=main | 1 | 930 | 896 | 34 | 0 | correct 478, in_doubt_surfaced 406, duplicate 34, stuck 9, unconfirmed_success 3 (recorded) | - |
| [006](docs/experiments/006-model-retry.md) | Windows | run=pilot | 5 | 117 | 112 | 2 | 3 | correct 45, in_doubt_surfaced 39, stuck 21, unconfirmed_success 7, no verdict recorded 3, duplicate 2 (recorded) | - |
| [007](docs/experiments/007-mcp-resume.md) | Linux | client_timeout_s=5.0 | 1 | 720 | 440 | 280 | 0 | correct 440, duplicate 280 | 0 |
| [007](docs/experiments/007-mcp-resume.md) | Windows | client_timeout_s=5.0 | 1 | 720 | 440 | 280 | 0 | correct 440, duplicate 280 | 0 |
| [008](docs/experiments/008-mcp-cut-points.md) | Linux | client_timeout_s=10.0 | 1 | 480 | 400 | 80 | 0 | correct 400, duplicate 80 | 0 |
| [008](docs/experiments/008-mcp-cut-points.md) | Windows | client_timeout_s=10.0 | 1 | 480 | 400 | 80 | 0 | correct 400, duplicate 80 | 0 |

Verdicts are re-derived with the current checker from the recorded trials, taking every refund's amount as correct (only the sink database can check amounts); "Differs" counts trials whose recorded verdict is different. 006 shows its recorded verdicts.

Not judged (written before the checker, no verdicts recorded): `results/single_call/20261116T165233Z.json` (180 trials); `results/single_call/20261116T171353Z.json` (3 trials).

Per cell, with notes on 002 and 006: [docs/results.md](docs/results.md).
<!-- results-table:end -->

## How it works

```
experiment runner ──> MCP client ──> refund MCP server ──> sink (FastAPI) ──> PostgreSQL
        │                                                      ▲
        └────────── arm fault / read ground truth ─────────────┘
```

- The **sink** plays the outside world. Each row in its `refunds` table is an effect that happened.
- The **fault** makes the sink commit a refund and then withhold the response past the client's timeout.
- The **verdict** for a trial is the number of refund rows. What the client believes is recorded but not trusted.

More: [docs/architecture.md](docs/architecture.md), [docs/contract.md](docs/contract.md).

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

The same checks run in CI on every push: see [CONTRIBUTING.md](CONTRIBUTING.md).

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

## Related work

This project builds on, and must be read alongside, "Resume Means Resume" (arXiv 2608.03836), UndoBench (arXiv 2610.05622) and ACRFence (arXiv 2603.20625). What each covers and what this project adds: [docs/related-work.md](docs/related-work.md).

## Roadmap

See [ROADMAP.md](ROADMAP.md). Results files record the commit they were run from; see [PROVENANCE.md](PROVENANCE.md).

An adversarial review of the experiments' methods and conclusions is in [docs/audits/scientific-audit.md](docs/audits/scientific-audit.md).
