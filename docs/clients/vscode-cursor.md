# VS Code and Cursor

**Status: not yet verified.** The files follow each editor's documented `mcp.json` format; nobody has run them against agent-crashtest yet.

> **Safety, for every client.**
> - **Sandbox only.** The refund tool is a test double writing to a local PostgreSQL. Never connect real payment credentials or a production system.
> - **The agent gets the refund server only** (`crashtest serve` or `crashtest mcp --stdio`). Never add the test controller (`crashtest controller`) to the agent's configuration.
> - **Keep `.crashtest-stack.json` private.** It holds the sink's control token. Run `crashtest serve` in a folder the agent's file tools cannot read.

Both connect to the Streamable HTTP server that `crashtest serve` prints (`http://127.0.0.1:PORT/mcp`). The port changes every time `serve` starts, so start it with a fixed port, `crashtest serve --mcp-port 8102`, and use `http://127.0.0.1:8102/mcp` below; then the configuration survives a restart. (Both editors can also launch a stdio command; the `command`/`args` pair from [claude-desktop.md](claude-desktop.md) applies.)

## VS Code

`.vscode/mcp.json` in your workspace:

```json
{
  "servers": {
    "crashtest-refunds": {
      "type": "http",
      "url": "http://127.0.0.1:PORT/mcp"
    }
  }
}
```

Start it from the file's code lens or the MCP Servers view; the tools `issue_refund` and `check_refund_status` (read-only) appear in the chat's tool picker.

## Cursor

`.cursor/mcp.json` in your project (or `~/.cursor/mcp.json` for all projects):

```json
{
  "mcpServers": {
    "crashtest-refunds": {
      "url": "http://127.0.0.1:PORT/mcp"
    }
  }
}
```

Then enable it in Cursor's MCP settings.

## Then

Arm a fault from a terminal in the folder where `serve` runs (`crashtest arm ORDER_ID --delay 30`), ask the assistant to refund that order, and judge it with `crashtest verdict ORDER_ID --report ...`.

Run `crashtest serve` outside the workspace the assistant can read: its `.crashtest-stack.json` holds the sink's control token.
