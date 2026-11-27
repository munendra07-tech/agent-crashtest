# Scenarios

A scenario is one simulated effect with one fault, an independent ground truth, a reproducible run, documented limitations and regression tests. Scenarios come from measured results (the project's experiments), not guesses. Each has a name and a version; every report names both, and the run ID.

| Scenario | Version | Fault | Ground truth | From |
|---|---|---|---|---|
| `refunds` (the reference) | 1 | Lost response after the commit: the sink commits the refund, then withholds its reply past the agent's timeout | The sink's rows (refunds and request log, which records that the fault fired) | [Experiments 001 to 003](experiments/001-mcp-refund-lost-response.md) |
| `refunds-cut-before-request` | 1 | Connection cut before the request: a TCP proxy between the agent and the MCP server withholds the agent's first call for the order and closes the connection, so the server never receives it | The sink's rows, plus the proxy's record that the call was withheld and that the sink held neither a request nor a refund for the order at the cut | [Experiment 008](experiments/008-mcp-cut-points.md), the `before_request` cut |
| `refunds-cut-mid-stream` | 1 | Connection cut mid-stream, before the commit: the tool sends a progress notification and pauses 3 s before calling the sink; a TCP proxy lets the notification through and closes the connection before the response; the server (stateful, SSE responses, no event store) cancels the tool | The sink's rows, plus the proxy's record that at least one notification and no response reached the agent, and that the sink held no refund for the order at the cut | [Experiment 008](experiments/008-mcp-cut-points.md), the `mid_stream` cut |

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

```powershell
crashtest run refunds-cut-mid-stream --agent naive
```

The test controller ([controller.md](controller.md)) runs `refunds` and `refunds-cut-before-request` (not yet `refunds-cut-mid-stream`, whose fault needs an MCP server `crashtest serve` does not start; it says so and points to `crashtest run`): `create_test_run` takes `scenario`, and its result gives the URL the agent must use (for the cut, the proxy's).

## `refunds`: lost response after the commit

What the agent sees: its call times out; the refund has already been made. A naive retry refunds twice (FAIL); a retry with a stable idempotency key gets the stored refund back (PASS). Selftest controls: the naive example agent must FAIL with a duplicate in every trial, the stable-key one must PASS, and the fault must be confirmed in every trial. Limitations: the README's "Limitations" section.

## `refunds-cut-before-request`: connection cut before the request

What the agent sees: its connection is dropped (with the reference `mcp` client, `RemoteProtocolError`). Nothing was refunded, because the call never reached the server, so a retry, even a naive one, refunds once.

**Why it matters.** The agent sees the same error as when the connection is cut *after* the commit, where the same naive retry refunds twice (experiment 008; `tests/test_scenarios.py` checks both: the same outcome and exception type for the two cuts, and one refund against two). From the error alone the agent cannot tell which happened. A PASS here does not show that the agent handles cuts safely; together with a PASS on `refunds` (a stable key), it does. An agent that looks the refund up before retrying also passes both, but only because this sink commits before the lookup; in a real system the first request can commit after the lookup, so a lookup is an extra on top of an enforced key, not a replacement.

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

## `refunds-cut-mid-stream`: connection cut mid-stream, before the commit

What the agent sees: its call starts, a progress notification arrives, then the connection is dropped. Nothing was refunded: the cut came while the tool was still paused before the sink call, and the server cancelled the tool when the connection closed. A retry, even a naive one, refunds once.

**The server configuration is fixed and stated**, because experiment 008 showed the outcome depends on it: stateful, SSE responses, **no event store**, and a tool that reports progress and then pauses 3 s before calling the sink (`stateful_sse_progress` in `crashtest.experiment.local_stack`). `crashtest run` and `selftest` start that server alongside the default one. With an event store, a client on protocol 2025-11-25 can resume the stream and get the original result instead; that configuration is not this scenario.

**Your agent's client must ask for progress** on the `issue_refund` call (with the Python `mcp` client, pass `progress_callback` to `call_tool`; both example agents do). Without a progress token the server sends no notification, the response passes the proxy uncut, and the trial is INCONCLUSIVE, with that fix in the report. The default agent timeout is 10 s, longer than the 3 s pause, so a retry is not cut off by its own timeout.

**Why it matters.** Like the cut before the request, it looks to the agent like a cut after the commit, but here the agent has already seen the call *start* (a progress notification). Seeing progress is not evidence that the effect happened. A stable idempotency key is safe either way.

**Results:** the same table as `refunds-cut-before-request` above (PASS for one refund; WARN for giving up honestly; FAIL for `lost` or `duplicate`; INCONCLUSIVE when no cut was made). A duplicate here means the agent made more than one refund call that reached the server, or that the server did not cancel the cut call (the sink is read only after the pause has passed, so a late commit would show as a second refund).

**Fault confirmed:** the proxy's record says at least one notification reached the agent, the response did not, and the sink held no refund for the order at the cut (`DropRecord.landed_as_intended`, as in 008).

**Selftest controls:** both example agents must PASS with one refund and the cut confirmed in every trial.

**Limitations:**
- One server configuration (above). The cancellation is inferred from the sink (the cut call never reaches it), not observed in the server.
- The proxy aborts both sides at once, so the server learns of the cut at once; a silent drop could let the effect commit.
- A cut while the tool's sink request is in flight, or after the commit but before the tool returns, is not this scenario.
- Measured in experiment 008 with the reference `mcp` client and server 2.3.0; this scenario runs the same server, checked by its regression tests and selftest, not by a new run of record.
- Not in the test controller yet, and not in the GitHub Action's `scenario` input.
- Repeatability under the stated timeout, not a rate.

## Considered and not added: a slow response

Experiment 003's timeout sweep (the response arrives, but after the agent's timeout) was considered as `refunds-slow-response`. It is not a separate scenario: `refunds` already commits, holds the response past the agent's timeout and then sends it, which is that fault. A slow response that arrives *within* the timeout is not a fault. See [decisions.md](decisions.md).
