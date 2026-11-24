# Connect an MCP client

Copy-ready setup for connecting an MCP client to agent-crashtest's simulated refund tools (`issue_refund`, and the read-only `check_refund_status`). Every route reaches the same MCP server code and the same sink, so a client gets the same tools, the same fault and the same verdicts whichever transport it uses.

| Client | Transport | Guide | Status |
|---|---|---|---|
| Python `mcp` client | Streamable HTTP and stdio | [python-mcp.md](python-mcp.md) | **verified in CI** (Ubuntu, Windows, macOS) |
| MCP Inspector, CLI mode | Streamable HTTP URL and stdio command | [mcp-inspector.md](mcp-inspector.md) | **verified in CI** (Ubuntu) |
| MCP Inspector, web UI | Streamable HTTP URL and stdio command | [mcp-inspector.md](mcp-inspector.md) | not yet verified |
| Claude Desktop | stdio | [claude-desktop.md](claude-desktop.md) | not yet verified |
| Claude Code | stdio or Streamable HTTP (`claude mcp add`) | [claude-code.md](claude-code.md) | not yet verified |
| VS Code, Cursor | Streamable HTTP (`mcp.json`) | [vscode-cursor.md](vscode-cursor.md) | not yet verified |

"Verified in CI" means a CI job runs exactly these commands on every push and checks the result. "Not yet verified" means nobody has run the steps as written: they follow each client's documentation, but they are untested here. If you run one, please report what happened (an issue, or a pull request changing the status line with the date and versions).

## Before you connect

1. **Start the stack** in a folder of your own:

   ```powershell
   crashtest serve
   ```

   It prints the MCP server's HTTP URL (a free port, different each time) and the stdio command, and writes both, with the sink's control token, to `.crashtest-stack.json` in that folder. It runs until Ctrl+C.

2. **Pick a transport.**
   - **stdio**: the client launches `crashtest mcp --stdio --stack-file <absolute path to .crashtest-stack.json>`. That process serves the tool and forwards each call to the running sink. Because it reads the stack file when it starts, a client that launches it fresh follows a restarted `crashtest serve` automatically; a client that keeps it running must restart it after `serve` restarts.
   - **Streamable HTTP**: the client connects to the printed URL, `http://127.0.0.1:<port>/mcp`. The port changes every time `serve` starts, so update the client's configuration after a restart.

3. **Run a test** from another terminal in the same folder: `crashtest arm ORDER_ID --delay 5`, have your client call `issue_refund` for that order with a timeout under 5 seconds, then `crashtest verdict ORDER_ID --report done|unknown|not_done|none`. See [../connect-your-client.md](../connect-your-client.md).

## Keep the agent away from the controls

- The MCP server exposes only the simulated tools. Arming faults, reading verdicts and resetting are done by you, with the `crashtest` command or the [test controller](../controller.md) (a separate MCP server for your own client), never by the agent. Never add the controller to the agent's configuration.
- The sink's `/control/*` endpoints require the control token, which `serve` keeps in `.crashtest-stack.json`. The token is never sent over MCP and never given to an agent that `crashtest run` starts. An agent that can read your folder can read the file, so run the agent where it cannot, or accept that it could.
- Everything listens on `127.0.0.1` only. The HTTP MCP server checks the `Host` and `Origin` headers (DNS-rebinding protection): requests naming any host other than `127.0.0.1`, `localhost` or `[::1]` are refused (421 for `Host`, 403 for `Origin`), so a web page in your browser cannot call it.
- `crashtest mcp --stdio` runs on your machine with your permissions. It is the `crashtest` from your own install; the guides show its exact arguments.
- The tool is a test double that writes to a local PostgreSQL database and moves no money. Never point it, or the agent under test, at real payment credentials or production systems.
