# Point your own MCP client at the refund tool

This guide is for testing a client, framework or agent of your own against the same fault the experiments use. The fault: the sink commits a refund, then holds its response past your client's timeout, so the client never hears back. What decides the result is how many refunds the sink holds afterwards, not what your client says.

**Your client or agent script runs on your machine, with your environment.** Do not give it secrets or production credentials it does not need (payment-provider keys, production database URLs, cloud tokens); in CI the script runs inside the GitHub Actions job with whatever the workflow gives it, and the Action passes none. Test with a throwaway configuration.

**Nothing real is refunded.** The tool is a test double: `issue_refund` writes one row to the sink, a local PostgreSQL database that the stack starts for you. No payment provider, account or network service outside your machine is involved, and no real money moves. Everything runs locally; there is no hosted server.

## Two examples: an unsafe retry and a stable key

Before connecting your own client, see both outcomes with the project's own client. Each command starts the whole stack locally (no Docker needed), runs three trials with the fault armed, and prints a table:

```powershell
python -m crashtest.experiment.run --local --faults lost_response --arms no_dedup --trials 3 --out-dir demo-results
```

```powershell
python -m crashtest.experiment.run --local --faults lost_response --arms stable_key --trials 3 --out-dir demo-results
```

| Example | What the client does | What the sink holds | Verdict |
|---|---|---|---|
| Unsafe retry (`no_dedup`) | Times out, then calls `issue_refund` again with no idempotency key | Two refunds per order (`effects_committed` 6 for 3 trials) | `duplicate` 3, `report_mismatch` 3: each time the client said one refund was made |
| Stable key (`stable_key`) | Times out, then calls again with the same `idempotency_key` | One refund per order (`effects_committed` 3) | `correct` 3, `report_mismatch` 0 |

The verdicts come from the project's checker (`crashtest.checker`), applied to the sink's counts. Every trial, with its verdict, is in the JSON file written to `demo-results/`. Re-checked on Windows on 24 November 2026: the same counts.

## With the `crashtest` command

Install the project with its local extra (`pip install "agent-crashtest[local] @ git+https://github.com/munendra07-tech/agent-crashtest@v0.3.0"`, or `pip install -e ".[local]"` from a clone), then:

| Step | Command | What it does |
|---|---|---|
| 1 | `crashtest serve` | Starts PostgreSQL, the sink and the MCP server on free ports, prints their URLs, and runs until Ctrl+C. It writes the URLs and the sink's control token to `.crashtest-stack.json` in the current folder, so the commands below find the sink and may arm faults. Ctrl+C removes the file; if `serve` is killed instead, the next command that reads it notices that `serve` is gone, removes it and says so. For a client that launches a command, `crashtest mcp --stdio --stack-file <that file>` serves the same tool over stdio; per-client setup is in [clients/](clients/README.md) |
| 2 | `crashtest arm ORDER_ID --delay 5` | The next refund for `ORDER_ID` commits, then its response is held for 5 seconds |
| 3 | (your client) | Calls `issue_refund` on the printed MCP URL, with a timeout shorter than the delay |
| 4 | `crashtest verdict ORDER_ID --report done` | Reads the sink and prints the refunds committed, the checker's verdict, and whether what your client said (`--report`) agrees with the sink. Exits 0 only for `correct`; `--json` prints one JSON object. The sink is the ground truth; `--report` is the agent's own claim, taken as given (the harness cannot see inside your client), so `unconfirmed_success` cannot occur here |

`arm` and `verdict` take `--sink-url` (or the `CRASHTEST_SINK_URL` environment variable) to use another sink, such as the Docker Compose stack's `http://localhost:8101`. The sink refuses `/control/*` calls (arming, reset) without its control token; `arm` reads it from `.crashtest-stack.json` or from `CRASHTEST_CONTROL_TOKEN`. Reading the ground truth (`GET /refunds`, what `verdict` does) needs no token. The sections below do the same steps by hand, with `curl` and Python.

## 1. Start the stack

**With Docker:**

```sh
export CRASHTEST_CONTROL_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
docker compose up --build -d --wait
```

Compose refuses to start without `CRASHTEST_CONTROL_TOKEN`: the sink requires it on `/control/*`. Keep it set in the shell where you run `crashtest arm` or `curl`.

The sink listens on `http://localhost:8101`. The MCP server, stateless JSON over streamable HTTP, is at `http://localhost:8102/mcp`. Three more MCP servers with other session and response settings are on ports 8103 to 8105; see `docker-compose.yml`.

**Without Docker**, from Python, with the project installed (`pip install -e ".[dev]"`):

```python
from crashtest.experiment.local_stack import local_stack

with local_stack() as stack:
    print("sink:", stack.sink_url, "mcp:", stack.mcp_url)
    input("Stack running; press Enter to stop.")
```

It starts PostgreSQL, the sink and the MCP server on free ports, and prints them; `stack.control_token` is the sink's control token. The steps below use the Docker ports; use the printed URLs instead if you start it this way.

## 2. The tool

The MCP server has two tools. `issue_refund` makes the refund:

| Argument | Type | Meaning |
|---|---|---|
| `order_id` | string | The order to refund. Use a fresh one per trial, such as `ord-<uuid>` |
| `amount_cents` | integer | The amount, for example `2500` |
| `idempotency_key` | string, optional | A caller-chosen key for this one refund. A call that repeats the key gets the stored result back instead of a second refund |

The server adds no deduplication of its own. It passes the key to the sink, which enforces it in the database.

`check_refund_status(order_id)` is read-only: it returns `{"exists": true}` or `{"exists": false}`, from the sink's rows, and changes nothing (no refund, no request-log entry). An agent can use it to look a refund up after a timeout instead of retrying blindly; [`examples/check_status_agent.py`](../examples/check_status_agent.py) does that.

## 3. Run one trial

1. **Pick a fresh order ID**, for example `ord-3f2a…`.
2. **Arm the fault for that order.** The next refund for it commits, then its response is held for `delay_s` seconds:

   ```sh
   curl -X PUT http://localhost:8101/control/faults \
     -H "Authorization: Bearer $CRASHTEST_CONTROL_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"order_id": "ord-3f2a", "delay_s": 5, "times": 1}'
   ```

3. **Call `issue_refund` from your client** with that order ID, and set a client timeout shorter than `delay_s` (2 seconds against 5, as the experiments do). Let your client do whatever it does by default: retry, give up, or report.
4. **Wait out the delay**, then **read the truth from the sink**:

   ```sh
   curl "http://localhost:8101/refunds?order_id=ord-3f2a"
   ```

   It returns `refunds`, the effects committed, and `requests`, every refund request the sink received for the order. One refund is right; two is a duplicate.

5. **Compare with what your client reported.** If it said "refunded" and the sink holds two refunds, its report contradicts the sink: that is property RA in the [contract](contract.md), recorded as `report_matches_sink`.

To judge the trial with the project's own rules:

```python
from crashtest.checker import classify, report_matches_sink

effects = 2  # len(refunds) from the sink
print(classify(effects=effects, intended=1, status="completed",
               payload_matches=True, confirmed=True))          # duplicate
print(report_matches_sink(effects=effects, intended=1, report="done"))  # False
```

`status` is how your client's run ended: `completed`, `unknown` (it reported that it does not know) or `failed`. `report` is what it told its caller: `done`, `unknown`, `not_done` or `none`. The [contract](contract.md), section 3, defines both.

## 4. A worked example

[`subjects/mcp_sdk.py`](../subjects/mcp_sdk.py) is the reference `mcp` client wrapped this way. It makes one call, never retries itself, and prints one JSON line: `ok`, `error` or `raised`. The other files in `subjects/` do the same for CrewAI, Microsoft agent-framework, LangGraph and pydantic-ai on DBOS. To add your client to the experiment that drives them (`python -m crashtest.experiment.single_call`), save a script with the same arguments (`--mcp-url`, `--order-id`, `--amount-cents`, `--timeout`) and the same one-line JSON report as `subjects/<name>.py`, then pass `--subject <name>=<path to the Python that has your client installed>`. See [experiment 003](experiments/003-mcp-client-libraries.md) for the full command.

## Reset between runs

```sh
curl -X POST http://localhost:8101/control/reset -H "Authorization: Bearer $CRASHTEST_CONTROL_TOKEN"
```

This empties the sink's refunds, request log and armed faults, and restarts their IDs. Use it only on a test stack.

Checked on 20 November 2026 on Windows with the local stack: the armed fault held the response, the reference client timed out after 2 seconds (`raised`), and the sink held one refund.
