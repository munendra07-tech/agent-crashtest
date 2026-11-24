# Framework examples

Agents built with an agent framework, each driven by a **scripted model** (plain Python that plays the model), so they run without an API key and give the same result every time. Each follows the agent contract of [`../stable_key_agent.py`](../stable_key_agent.py) and runs under `crashtest run`.

| Framework | Example | Its MCP client | Status |
|---|---|---|---|
| Pydantic AI 2.68.0 | [`pydantic_ai_agent.py`](pydantic_ai_agent.py) | `MCPToolset` (Streamable HTTP) | **verified in CI** (`framework-examples` job, Ubuntu): naive FAIL, stable key PASS |
| LangGraph 1.2.22 | [`langgraph_agent.py`](langgraph_agent.py) | `langchain-mcp-adapters` 0.3.2 (`mcp` 1.30.0) | **verified in CI** (`framework-examples` job, Ubuntu): naive FAIL, stable key PASS |

Not included: frameworks whose agents need a model provider and key to run (no scripted-model route was built for them here), such as CrewAI and the OpenAI Agents SDK. Issue reports or pull requests with a keyless example are welcome.

## Run one

Install the framework in a Python of its own (it does not need agent-crashtest itself):

```powershell
python -m venv .venv-pydantic-ai
```

```powershell
.venv-pydantic-ai\Scripts\python -m pip install -r examples/frameworks/requirements-pydantic-ai.txt
```

Then, from the environment that has agent-crashtest:

```powershell
crashtest run refunds --agent examples/frameworks/pydantic_ai_agent.py --python .venv-pydantic-ai\Scripts\python.exe
```

`REFUND_KEYING=naive` (in PowerShell, `$env:REFUND_KEYING = "naive"`) makes the scripted model leave out the idempotency key; expect FAIL with a duplicate. The default, `stable_key`, sends `refund-<order id>` on every call; expect PASS.

## What they show, and what they do not

- They exercise the framework's real tool-call path: its MCP client, its timeout, and what it does with the timeout (Pydantic AI raises out of `agent.run`, and the script retries the run; the LangGraph example's tool node passes the error to the model, which asks again).
- **With LangGraph's default tool node** (no `handle_tool_errors=True`), the MCP timeout was not caught: the run raised and the agent stopped. That is in the example's docstring as observed on 25 November 2026, with the versions above; it is not a claim about other versions or configurations.
- The scripted model decides whether a key is sent. A real model may or may not send one; these examples do not measure model behaviour, only that the harness judges a framework's agent correctly either way.
