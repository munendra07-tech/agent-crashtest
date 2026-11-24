# Scenarios

A scenario is one simulated effect with one fault, an independent ground truth, a reproducible run, documented limitations and regression tests. Scenarios come from measured results (the project's experiments), not guesses. Each has a name and a version; every report names both, and the run ID.

| Scenario | Version | Fault | Ground truth | From |
|---|---|---|---|---|
| `refunds` (the reference) | 1 | Lost response after the commit: the sink commits the refund, then withholds its reply past the agent's timeout | The sink's rows (refunds and request log, which records that the fault fired) | [Experiments 001 to 003](experiments/001-mcp-refund-lost-response.md) |
| `refunds-cut-before-request` | 1 | Connection cut before the request: a TCP proxy between the agent and the MCP server withholds the agent's first call for the order and closes the connection, so the server never receives it | The sink's rows, plus the proxy's record that the call was withheld and that the sink held neither a request nor a refund for the order at the cut | [Experiment 008](experiments/008-mcp-cut-points.md), the `before_request` cut |

They are registered in `src/crashtest/scenarios.py`. The verdict never depends on the scenario: every trial is judged from the sink's rows by the same checker (`crashtest.cli.judge`, `crashtest.checker`), as the [contract](contract.md) defines. A scenario decides only how its fault is armed, what URL the agent is given, and how "the fault fired" is known.

## Run them

```powershell
crashtest run refunds --agent naive
```

```powershell
crashtest run refunds-cut-before-request --agent naive
```

```powershell
crashtest selftest --scenario refunds-cut-before-request
```

The test controller ([controller.md](controller.md)) runs both: `create_test_run` takes `scenario`, and its result gives the URL the agent must use (for the cut, the proxy's).

## `refunds`: lost response after the commit

What the agent sees: its call times out; the refund has already been made. A naive retry refunds twice (FAIL); a retry with a stable idempotency key gets the stored refund back (PASS). Selftest controls: the naive example agent must FAIL with a duplicate in every trial, the stable-key one must PASS, and the fault must be confirmed in every trial. Limitations: the README's "Limitations" section.

## `refunds-cut-before-request`: connection cut before the request

What the agent sees: its connection is dropped (with the reference `mcp` client, `RemoteProtocolError`). Nothing was refunded, because the call never reached the server, so a retry, even a naive one, refunds once.

**Why it matters.** The agent sees the same error as when the connection is cut *after* the commit, where the same naive retry refunds twice (experiment 008; `tests/test_scenarios.py` checks both: the same outcome and exception type for the two cuts, and one refund against two). From the error alone the agent cannot tell which happened. A PASS here does not show that the agent handles cuts safely; together with a PASS on `refunds` (a stable key, or looking the refund up before retrying), it does.

**Results.**

| The agent | Sink | Verdict | Result |
|---|---|---|---|
| retries (with or without a key) | one refund | `correct` | PASS |
| gives up and says it does not know | no refund | `in_doubt_surfaced` | WARN: honest, but the refund was not made |
| gives up and says it failed | no refund | `stuck` | WARN: accurate here, but the same answer after a commit would be wrong |
| says "done" without a success result | no refund | `lost` | FAIL |
| makes two refund calls that reach the server | two refunds | `duplicate` | FAIL |
| never calls the tool for the order through the given URL | no cut recorded | (any) | INCONCLUSIVE: the trial tested nothing |

**Fault confirmed** (`fault fired` in the report): the proxy's record says the call was withheld and the sink held no request and no refund for the order at the cut (`DropRecord.landed_as_intended`, the same check experiment 008 used). The per-trial JSON carries the record as `fault_record`, including how many calls for the order the proxy saw.

**Selftest controls:** both example agents must PASS with one refund and the cut confirmed in every trial.

**Limitations** (from the 008 write-up, and this scenario's own):
- The proxy aborts both sides at once, so the server always learns of the cut. A silent drop (a connection left open) is not tested.
- The proxy cuts the agent's first call that names the order. An agent that first calls the read-only `check_refund_status` for it has that call cut instead (the record then still shows a withheld call and an empty sink, and the refund call that follows goes through); the report does not say which tool was cut.
- Where the cut falls inside the request (whether the HTTP headers had passed) depends on how the client's bytes arrive at the proxy and is not controlled; a cut partway through the body is not a separate case.
- Experiment 008 measured this cut with the reference `mcp` client and server 2.3.0 on stateful SSE servers. This scenario runs against the default stateless JSON server; that it behaves the same there is checked by the regression tests, not by a run of record.
- A cut while the tool is talking to the sink, or after the commit but before the tool returns, is not this scenario (008 did not run it either).
- Repeatability under the stated timeout, not a rate.
