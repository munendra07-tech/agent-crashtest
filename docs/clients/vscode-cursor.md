# VS Code and Cursor

**Status: not yet verified.** The files follow each editor's documented `mcp.json` format; nobody has run them against agent-crashtest yet.

Both connect to the Streamable HTTP server that `crashtest serve` prints (`http://127.0.0.1:PORT/mcp`). The port changes every time `serve` starts, so update the URL after a restart. (Both editors can also launch a stdio command; the `command`/`args` pair from [claude-desktop.md](claude-desktop.md) applies.)

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

Start it from the file's code lens or the MCP Servers view; the tool `issue_refund` appears in the chat's tool picker.

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
