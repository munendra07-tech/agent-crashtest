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
pip install "agent-crashtest[local] @ git+https://github.com/munendra07-tech/agent-crashtest@v0.3.1"
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
- uses: munendra07-tech/agent-crashtest@v0.3.1
  with:
    agent: path/to/my_agent.py   # or naive, stable_key
    scenario: all                # both scenarios; the default is refunds
    trials: 5
    fail-on-duplicate-effect: true
```

**Run both scenarios** with `scenario: all` (since 0.3.1). It runs both scenarios, one after the other, and the step's result is the worse of the two. Use it: an agent that reports "done" after an error, without retrying, passes `refunds` alone (the refund had committed) and fails the connection cut (nothing had). `scenario: refunds` stays the default. `scenario: refunds-cut-mid-stream` runs the third scenario on its own; it is not in `all`, because it needs an MCP client that asks for progress, and adding it would change existing builds.

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

## Connect an MCP client

Three steps: start the stack with `crashtest serve`, add the refund server to your client, then use `crashtest arm` and `crashtest verdict` as in the step-by-step section above. For an HTTP client, `crashtest serve --mcp-port 8102` keeps the URL at `http://127.0.0.1:8102/mcp` across restarts (on `main`, in the next release).

| Client | How it connects | Status |
|---|---|---|
| Python `mcp` client | stdio or HTTP | Verified in CI ([guide](docs/clients/python-mcp.md)) |
| MCP Inspector, CLI mode | stdio or HTTP | Verified in CI ([guide](docs/clients/mcp-inspector.md)); its web UI has not been run |
| Claude Desktop | stdio | Not yet verified ([guide](docs/clients/claude-desktop.md)) |
| Claude Code | stdio or HTTP | Not yet verified ([guide](docs/clients/claude-code.md)) |
| VS Code, Cursor | HTTP, or stdio | Not yet verified ([guide](docs/clients/vscode-cursor.md)) |

"Not yet verified" means the setup follows that client's documentation but nobody has run it against agent-crashtest. If you try one, please say whether it worked, with a [Tester report](https://github.com/munendra07-tech/agent-crashtest/issues/new?template=tester_report.yml).

`crashtest serve` serves the tool over Streamable HTTP at the URL it prints. For clients that launch a command instead, `crashtest mcp --stdio --stack-file <path to .crashtest-stack.json>` serves the same tool over stdio on the running sink (or `--sink-url URL`); without a running sink it says so on stderr and exits 2. Both transports run the same server code against the same sink, and CI checks that the same agent gets the same verdict over both. Copy-ready setup for the Python `mcp` client, the MCP Inspector, Claude Desktop, Claude Code, VS Code and Cursor, each marked with whether it has been verified: [docs/clients/](docs/clients/README.md).

**Security.** The agent under test sees only the simulated tools (`issue_refund`, and the read-only `check_refund_status` lookup). Arming faults, resetting and judging stay with you: the sink's `/control/*` endpoints require a control token that `serve` stores in `.crashtest-stack.json` (never sent over MCP, never given to an agent that `crashtest run` starts), while the read-only ground truth (`GET /refunds`) stays open. An agent that can read that file can read the token, so keep the agent out of that folder. Everything listens on `127.0.0.1`, and the HTTP MCP server refuses requests whose `Host` or `Origin` header names any other host (DNS-rebinding protection).

### Drive tests from your own client: the test controller

`crashtest controller --stdio` (or `--http`, token required) is a second MCP server, for you and never for the agent under test, with five tools: `create_test_run`, `configure_scenario`, `arm_fault`, `get_run_status` and `get_test_report`. It has no reset or delete tool. Reports carry a run ID and the scenario's name and version, and the verdict comes only from the sink, judged by the same checker and report code as `crashtest run`; a regression test checks that both give the same result, verdicts and counts for the same agent behaviour. See [docs/controller.md](docs/controller.md).

## More scenarios

`refunds` (a lost response after the commit) is the reference scenario. Two more come from the project's experiment 008; run them too, because an agent can pass one and fail another.

### The connection cut before the request

```powershell
crashtest run refunds-cut-before-request --agent naive
```

A proxy between your agent and the MCP server cuts the agent's first call before it reaches the server, so nothing is refunded and even a naive retry refunds once (PASS). The point is the error: your agent sees the same disconnect as when the cut comes after the commit, where the naive retry refunds twice (experiment 008). It cannot tell the two apart from the error, so only a stable idempotency key is safe in both (looking the refund up passes here too, but can race in a real system; see above); run both scenarios. An agent that reports "done" without a success result gets `lost` (FAIL). Ground truth: the sink's rows plus the proxy's record that the call was withheld. `crashtest selftest --scenario refunds-cut-before-request` checks the harness for it. Details and limitations: [docs/scenarios.md](docs/scenarios.md).

### The connection cut mid-stream

```powershell
crashtest run refunds-cut-mid-stream --agent naive
```

The tool sends a progress notification, then the proxy cuts the connection before the response, while the tool is still paused before the refund; the server cancels the tool, so nothing is refunded and a retry refunds once. Seeing the call start is not evidence that it finished. Your agent's MCP client must ask for progress (`progress_callback` with the Python client), or the trial is INCONCLUSIVE. One stated server configuration (stateful SSE, no event store), from experiment 008. The test controller runs it too, and the Action takes `scenario: refunds-cut-mid-stream` (not part of `scenario: all`). Details: [docs/scenarios.md](docs/scenarios.md).

## Limitations

- **Client guides.** Only the Python `mcp` client (stdio and HTTP) and the MCP Inspector's CLI are verified in CI; the Claude Desktop, Claude Code, VS Code, Cursor and Inspector web-UI steps follow those clients' documentation and have not been run yet ([docs/clients/](docs/clients/README.md)). `crashtest serve` picks a free port each time unless you fix one with `--mcp-port` (since the next release); otherwise an HTTP client's configuration must be updated after a restart.
- **Three scenarios.** A refund through one MCP tool, with one of three faults: the first response is lost after the refund commits (`refunds`, the reference), the connection is cut before the request reaches the server (`refunds-cut-before-request`), or it is cut mid-stream, before the commit (`refunds-cut-mid-stream`, one server configuration); both cuts are from experiment 008. Cuts during the effect, crashes of the agent's own process and other effects (bookings, inventory) are not in `crashtest run` yet. See [docs/scenarios.md](docs/scenarios.md) for each scenario's ground truth and limitations.
- **What a PASS shows.** One correct refund per order in the sink, and the agent said `done`. It does not show that the agent received a success or reasoned correctly: the harness sees only the sink and the agent's final report. An agent that says `done` after an error passes `refunds` (the refund landed anyway) and fails `refunds-cut-before-request` (`lost`), so run both.
- **The agent runs as a script**, once per order, with the arguments above. An agent that needs a model, credentials or a long-running process has to be wrapped in such a script.
- **A test double, not a payment provider.** A PASS says your agent's retries are safe against this test double, a sink that enforces idempotency keys in its database; it says nothing about whether your real provider does.
- **Repeatability, not a rate.** "5 of 5 trials" says the outcome repeated under the stated configuration (fault delay, agent timeout); it is not a probability and carries no confidence interval. An agent whose behaviour depends on timing or on a model may need more trials, or other timings.
- **Platforms, as CI shows.** On every push CI installs the package into a fresh virtual environment (not editable), runs `crashtest selftest` and both example agents on Ubuntu, Windows and macOS (GitHub's `ubuntu-latest`, `windows-latest` and `macos-latest` runners, Python 3.12): all three passed ([run](https://github.com/munendra07-tech/agent-crashtest/actions/runs/45485719103)). The full test suite also runs in CI on all three ([run](https://github.com/munendra07-tech/agent-crashtest/actions/runs/45653846654)); on the Windows runner it runs from the runner's temp folder, because PostgreSQL's setup cannot use the checkout folder there. Other macOS versions, chips and Python versions are not tested. On Windows, `pgserver` (PostgreSQL) cannot create a data directory under a path with characters outside the system's ANSI code page; see [Run it on Windows](docs/development.md#run-it-on-windows-powershell). The Action runs your agent with its own Python 3.12, which has the `mcp` client; whether packages your agent needs can be installed into that Python in an earlier step has not been tested yet.

## Roadmap

What comes after this release will be chosen from early users' feedback, so please open an issue with what you tried and what you need. Plans and their status: [ROADMAP.md](ROADMAP.md).

## Contributing and license

Bug reports, reproductions and "my agent failed and I don't know why" questions are all welcome: open an issue with one of the templates. Setup, checks and good first issues are in [CONTRIBUTING.md](CONTRIBUTING.md). MIT licensed: see [LICENSE](LICENSE).

## Further reading

- [docs/research.md](docs/research.md): the experiments behind the tool, their results table and related work.
- [docs/development.md](docs/development.md): running everything from a clone (Windows, Docker, Linux or macOS), the checks and the repository layout.
- [docs/contract.md](docs/contract.md): what each verdict means. [docs/scenarios.md](docs/scenarios.md): each scenario's ground truth and limits.
