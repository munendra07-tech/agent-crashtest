# Claude Desktop

**Status: not yet verified.** The configuration follows Claude Desktop's documented `claude_desktop_config.json` format for stdio servers; nobody has run it against agent-crashtest yet.

Claude Desktop launches local MCP servers as commands over stdio, so it uses `crashtest mcp --stdio`.

1. Start the stack in a folder of your own: `crashtest serve`. Keep it running.
2. Find the full path of the `crashtest` command (Claude Desktop may not see the `PATH` of your terminal or virtual environment):
   - Windows: `where crashtest`, for example `C:\Users\you\venvs\crashtest\Scripts\crashtest.exe`
   - macOS or Linux: `which crashtest`, for example `/Users/you/venvs/crashtest/bin/crashtest`
3. Open the configuration file (Settings, Developer, Edit Config):
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
4. Add the server, with your two paths:

   ```json
   {
     "mcpServers": {
       "crashtest-refunds": {
         "command": "C:\\Users\\you\\venvs\\crashtest\\Scripts\\crashtest.exe",
         "args": [
           "mcp",
           "--stdio",
           "--stack-file",
           "C:\\Users\\you\\crashtest-work\\.crashtest-stack.json"
         ]
       }
     }
   }
   ```

5. Restart Claude Desktop. The tools `issue_refund` and `check_refund_status` (read-only) should appear under the server `crashtest-refunds`. If the stack is not running, the server exits at once with "no sink is running" (Claude Desktop shows it in its MCP log).
6. Arm a fault from your terminal (`crashtest arm ORDER_ID --delay 30`), ask Claude to refund that order, then run `crashtest verdict ORDER_ID --report done|unknown|not_done|none`, choosing what Claude told you.

Notes:
- Claude Desktop starts the server when it starts. After restarting `crashtest serve`, restart Claude Desktop (or the server) so it reads the new stack file.
- This connects the simulated tool only. Do not add anything that can reach the sink's control endpoints, and keep `.crashtest-stack.json` out of folders the assistant can read through other tools.
- A model's timeout and retry behaviour belongs to the client, not to agent-crashtest; a single manual trial shows what happened once, not a rate.
