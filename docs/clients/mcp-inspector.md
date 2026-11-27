# The MCP Inspector

The [MCP Inspector](https://github.com/modelcontextprotocol/inspector) is the protocol's own debugging client. Use it to see the tool and call it by hand. These steps use Inspector **2.14.0**, which needs Node `^22.23.4 || ^24.22.1 || >=26.12.1`.

Start the stack first with `crashtest serve` and note the HTTP URL it prints (see [README.md](README.md)). In the commands below, replace `PORT` and the stack file path with yours.

> **Safety, for every client.**
> - **Sandbox only.** The refund tool is a test double writing to a local PostgreSQL. Never connect real payment credentials or a production system.
> - **The agent gets the refund server only** (`crashtest serve` or `crashtest mcp --stdio`). Never add the test controller (`crashtest controller`) to the agent's configuration.
> - **Keep `.crashtest-stack.json` private.** It holds the sink's control token. Run `crashtest serve` in a folder the agent's file tools cannot read.

## CLI mode

**Status: verified in CI** (the `inspector` job, Ubuntu, Node 22): the job runs these commands against a `crashtest serve` stack, checks that `tools/list` returns `issue_refund` over both transports, calls the tool once over each, and checks with `crashtest verdict` that each order holds exactly one refund.

Over Streamable HTTP (a URL ending in `/mcp` selects that transport):

```bash
npx -y @modelcontextprotocol/inspector@2.14.0 --cli http://127.0.0.1:PORT/mcp --method tools/list
```

```bash
npx -y @modelcontextprotocol/inspector@2.14.0 --cli http://127.0.0.1:PORT/mcp --method tools/call --tool-name issue_refund --tool-arg order_id=ord-insp-1 --tool-arg amount_cents=2500
```

Over stdio (everything before `--` is the server command):

```bash
npx -y @modelcontextprotocol/inspector@2.14.0 --cli crashtest mcp --stdio --stack-file /absolute/path/to/.crashtest-stack.json -- --method tools/list
```

```bash
npx -y @modelcontextprotocol/inspector@2.14.0 --cli crashtest mcp --stdio --stack-file /absolute/path/to/.crashtest-stack.json -- --method tools/call --tool-name issue_refund --tool-arg order_id=ord-insp-2 --tool-arg amount_cents=2500
```

Then `crashtest verdict ord-insp-1` (in the folder where `serve` runs) shows one refund, verdict `correct`.

To see the fault: `crashtest arm ord-insp-3 --delay 30` first, then call the tool for `ord-insp-3`. The sink commits the refund and holds the response for 30 seconds, so the Inspector waits (or times out, depending on its request timeout). `crashtest verdict ord-insp-3` shows the refund already committed while the call is still waiting: that is the lost-response window an agent has to handle.

## Web UI

**Status: not yet verified.** Following the Inspector's documentation:

```bash
npx -y @modelcontextprotocol/inspector@2.14.0 crashtest mcp --stdio --stack-file /absolute/path/to/.crashtest-stack.json
```

opens the web UI (by default on `http://127.0.0.1:6274`) connected to the stdio command. For the HTTP server, start the web UI with `npx -y @modelcontextprotocol/inspector@2.14.0`, choose the Streamable HTTP transport and enter `http://127.0.0.1:PORT/mcp`.

The Inspector's own web UI runs in your browser and calls the MCP server from its Node backend, so the server's DNS-rebinding check (it accepts only `127.0.0.1`, `localhost` and `[::1]` in `Host` and `Origin`) should not get in the way; this has not been checked.

The Inspector stores stdio `env:` values in your keychain or, without one, in `~/.mcp-inspector/secrets.json`. The commands above pass no secrets.
