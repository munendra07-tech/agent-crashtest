# The test controller

`crashtest controller` is a second MCP server, for **you**, the developer: from your own MCP client (the MCP Inspector, an IDE, a script) you create a test run, arm the fault, watch the sink and get the report. The agent under test never connects to it.

```
 agent under test ──MCP──> refund MCP server (simulated tools only) ──> sink ──> PostgreSQL
                                                                         ▲
 you ──MCP──> test controller ── control token ──────────────────────────┘
              create_test_run, configure_scenario, arm_fault,
              get_run_status, get_test_report
```

**Never configure the controller into the agent under test.** With it the agent could arm faults and read the ground truth. Give the agent only the refund MCP server.

## Start it

1. Start the stack in a folder of your own: `crashtest serve`. It writes `.crashtest-stack.json` (the URLs and the sink's control token).
2. Start the controller from the same folder, or point it at that file with `--stack-file`:

   ```powershell
   crashtest controller --stdio
   ```

   for a client that launches a command, or

   ```powershell
   crashtest controller --http --port 8765
   ```

   for Streamable HTTP at `http://127.0.0.1:8765/mcp`. Over HTTP every request must carry the control token from the stack file, as `Authorization: Bearer <token>`; requests without it get 401. It listens on `127.0.0.1` only and checks the `Host` and `Origin` headers, like the refund server.

Without a stack file (or `--sink-url` and `--mcp-url` with `CRASHTEST_CONTROL_TOKEN` set), or without a running sink, it prints an error on stderr and exits 2.

## The tools

| Tool | What it does |
|---|---|
| `create_test_run` | Creates a run of `trials` trials (default 1), each with a fresh order ID, for a scenario (default `refunds`). Returns the run ID, the scenario's name and version, its settings, the order IDs and the agent-facing MCP URL |
| `configure_scenario` | Changes the run's settings before any fault is armed: `client_timeout_s` (the timeout your agent should use), `fault_delay_s` (how long the sink holds the response; must be longer), `amount_cents` |
| `arm_fault` | Arms the scenario's fault for one trial's order. Then run your agent on that order |
| `get_run_status` | Read-only: for each trial, whether its fault is armed, and the refunds and requests the sink holds now, and whether the fault fired |
| `get_test_report` | The report, in `json`, `text`, `markdown` or `junit`, with the run ID and the scenario's name and version. Pass `agent_reports`, what your agent told its caller for each trial (`{"1": "done"}`; `done`, `unknown`, `not_done` or `none`, the default) |

There is no reset or delete tool: the controller cannot erase evidence.

## Where the verdict comes from

Only from the sink. `get_test_report` reads each trial's rows from the sink (`GET /refunds`), waits until a held response can no longer be in flight, and judges them with the same code as `crashtest run`: `crashtest.cli.judge` (the checker, `crashtest.checker`) per trial and the same summary and report formats. `agent_reports` is your statement of what the agent said. It is shown as "agent-reported (not verified)" and used only the way the [contract](contract.md) allows: it tells one correct refund apart from one the agent was unsure about, and never turns a duplicate into anything else. A regression test (`tests/test_controller.py`) runs the two example agents through the controller and through `crashtest run` and checks that the result, exit code, summary, verdicts and every observed count agree.

## A run, step by step

The controller keeps runs in memory, so use a client that keeps one controller running for the whole run: an IDE or the Inspector's web UI over stdio, a script with the Python `mcp` client (as `tests/check_controller.py` does), or the HTTP mode. With the HTTP controller and the MCP Inspector's CLI:

```bash
TOKEN=$(python -c "import json; print(json.load(open('.crashtest-stack.json'))['control_token'])")
C="npx -y @modelcontextprotocol/inspector@2.14.0 --cli http://127.0.0.1:8765/mcp --header"
$C "Authorization: Bearer $TOKEN" --method tools/call --tool-name create_test_run --tool-arg trials=1
$C "Authorization: Bearer $TOKEN" --method tools/call --tool-name arm_fault --tool-arg run_id=RUN_ID --tool-arg trial=1
# run your agent on the order ID create_test_run returned, against the agent-facing MCP URL
$C "Authorization: Bearer $TOKEN" --method tools/call --tool-name get_test_report --tool-arg run_id=RUN_ID --tool-arg 'agent_reports={"1": "done"}' --tool-arg format=text
```

Status: the Inspector commands above are **not yet verified**. The controller itself is **verified in CI** through the Python `mcp` client: in-process and over HTTP with the token (`tests/test_controller.py`, Ubuntu), and `crashtest controller --stdio` from a clean install on Ubuntu, Windows and macOS (`tests/check_controller.py`).

## Limitations

- Runs live in the controller's memory: restarting it forgets them (the sink keeps every row).
- The controller does not run your agent. You run it, on the order ID it gives you, and say what it reported; `crashtest run` is the way to have the harness run an agent script itself.
- One scenario so far, `refunds` (version 1); `create_test_run` lists the scenarios it knows.
