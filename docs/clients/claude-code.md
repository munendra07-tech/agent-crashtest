# Claude Code

**Status: not yet verified.** The commands follow Claude Code's documented `claude mcp add` syntax; nobody has run them against agent-crashtest yet.

> **Safety, for every client.**
> - **Sandbox only.** The refund tool is a test double writing to a local PostgreSQL. Never connect real payment credentials or a production system.
> - **The agent gets the refund server only** (`crashtest serve` or `crashtest mcp --stdio`). Never add the test controller (`crashtest controller`) to the agent's configuration.
> - **Keep `.crashtest-stack.json` private.** It holds the sink's control token. Run `crashtest serve` in a folder the agent's file tools cannot read.

Start the stack first in a folder of your own with `crashtest serve` (see [README.md](README.md)).

## stdio (recommended: follows a restarted stack)

```bash
claude mcp add crashtest-refunds -- crashtest mcp --stdio --stack-file /absolute/path/to/.crashtest-stack.json
```

Everything after `--` is the command Claude Code launches. If `crashtest` is not on the `PATH` Claude Code sees, give its full path (`which crashtest`, or `where crashtest` on Windows).

## Streamable HTTP

```bash
claude mcp add --transport http crashtest-refunds http://127.0.0.1:PORT/mcp
```

Use the URL `crashtest serve` printed. The port changes each time `serve` starts; then remove and add the server again (`claude mcp remove crashtest-refunds`).

## Check and use it

```bash
claude mcp list
```

should show `crashtest-refunds`. In a session, `/mcp` shows its status and the tools `issue_refund` and `check_refund_status` (read-only). Arm a fault from another terminal (`crashtest arm ORDER_ID --delay 30`), ask for the refund, then judge it with `crashtest verdict ORDER_ID --report ...`.

Keep the stack file out of the project Claude Code is working in, or Claude Code's file tools can read the control token. Running `crashtest serve` in a separate folder avoids that.
