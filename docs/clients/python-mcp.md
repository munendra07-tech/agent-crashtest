# The Python `mcp` client

**Status: verified in CI.** `tests/test_connect_security.py` (the `test-local` job, Ubuntu) and `tests/check_transport_parity.py` (the `clean-install` job, on Ubuntu, Windows and macOS with the package installed from scratch) run an agent built like the code below over both transports, and check that a naive retry gets FAIL (`duplicate`) and a stable idempotency key gets PASS (`correct`), the same over stdio as over HTTP. The SDK version is the one the package pins (`mcp==2.3.0`).

Start the stack first with `crashtest serve` (see [README.md](README.md)).

## Streamable HTTP

```python
import asyncio
from mcp import Client

MCP_URL = "http://127.0.0.1:PORT/mcp"  # printed by `crashtest serve`

async def main() -> None:
    async with Client(MCP_URL) as client:
        tools = await client.list_tools()
        print([t.name for t in tools.tools])  # ['issue_refund']
        result = await client.call_tool(
            "issue_refund",
            {"order_id": "ord-1", "amount_cents": 2500, "idempotency_key": "refund-ord-1"},
            read_timeout_seconds=2,
        )
        print(result.structured_content)

asyncio.run(main())
```

## stdio

The client launches `crashtest mcp --stdio`, which serves the same tool and forwards to the running sink:

```python
import asyncio
from mcp import Client, StdioServerParameters

SERVER = StdioServerParameters(
    command="crashtest",
    args=["mcp", "--stdio", "--stack-file", "/absolute/path/to/.crashtest-stack.json"],
)

async def main() -> None:
    async with Client(SERVER) as client:
        result = await client.call_tool(
            "issue_refund", {"order_id": "ord-2", "amount_cents": 2500}, read_timeout_seconds=2
        )
        print(result.structured_content)

asyncio.run(main())
```

Instead of `--stack-file` you can pass `--sink-url http://127.0.0.1:PORT` (the sink URL `serve` printed) or set `CRASHTEST_SINK_URL`. Without a running sink, `crashtest mcp --stdio` prints an error on stderr and exits with code 2.

## Run it as an agent under `crashtest run`

To be judged by `crashtest run refunds --agent your_agent.py`, the script takes `--mcp-url URL --order-id ID --amount-cents N --timeout SECONDS` and prints one JSON line `{"report": "done" | "unknown" | "not_done"}`. Copy [`examples/stable_key_agent.py`](../../examples/stable_key_agent.py) to start. `crashtest run` gives the agent an HTTP URL; the test agent in [`tests/data/transport_agent.py`](../../tests/data/transport_agent.py) shows the same logic over stdio.
