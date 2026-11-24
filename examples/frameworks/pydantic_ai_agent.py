"""A Pydantic AI agent under test, with a scripted model: no API key, no network model.

    crashtest run refunds --agent examples/frameworks/pydantic_ai_agent.py \
        --python PATH/TO/A/PYTHON/WITH/pydantic-ai-slim[mcp]

The agent connects to the refund MCP server through Pydantic AI's own MCP client
(`MCPToolset`) and is driven by a `FunctionModel`: a plain Python function that plays
the model. It asks for one `issue_refund` call and, once the tool has returned,
answers "done". Nothing here calls a model provider, so it runs without a key.

What it tests is the framework's tool-call path (its MCP client, timeout and error
handling) plus the application's retry policy around `agent.run`, chosen by the
environment variable REFUND_KEYING, because `crashtest run` passes only the
standard agent arguments:
  stable_key (default)  the model sends idempotency_key "refund-<order id>" on every
                        call: a retry after the lost response gets the stored refund
  naive                 no key: a retry after the lost response refunds again
Expect PASS for stable_key and FAIL (a duplicate) for naive on `crashtest run refunds`.

Same contract as examples/stable_key_agent.py: --mcp-url, --order-id,
--amount-cents, --timeout; the last stdout line is {"report": ...}.
Pinned and checked in CI with pydantic-ai-slim[mcp]==2.68.0.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

MAX_RUNS = 3


def scripted_model(order_id: str, amount_cents: int, stable_key: bool) -> FunctionModel:
    """The 'model': call issue_refund once, then say done once a tool result is back."""
    arguments: dict[str, object] = {"order_id": order_id, "amount_cents": amount_cents}
    if stable_key:
        arguments["idempotency_key"] = f"refund-{order_id}"

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        if isinstance(last, ModelRequest) and any(
            isinstance(p, ToolReturnPart) and p.tool_name == "issue_refund" for p in last.parts
        ):
            return ModelResponse(parts=[TextPart("done")])
        return ModelResponse(parts=[ToolCallPart("issue_refund", arguments)])

    return FunctionModel(respond)


async def refund(args: argparse.Namespace) -> str:
    stable_key = os.environ.get("REFUND_KEYING", "stable_key") != "naive"
    agent = Agent(scripted_model(args.order_id, args.amount_cents, stable_key))
    for _ in range(MAX_RUNS):
        # A fresh toolset (and MCP connection) per run, with the timeout under test.
        toolset = MCPToolset(args.mcp_url, read_timeout=args.timeout, tool_error_behavior="error")
        try:
            result = await agent.run("Refund this order once.", toolsets=[toolset])
        except (Exception, BaseExceptionGroup):
            # The application's retry policy: run the agent again. Safe only with a
            # stable key: the first call may have committed.
            continue
        return "done" if result.output == "done" else "not_done"
    return "unknown"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp-url", required=True)
    parser.add_argument("--order-id", required=True)
    parser.add_argument("--amount-cents", type=int, default=2500)
    parser.add_argument("--timeout", type=float, default=2.0)
    args = parser.parse_args()
    print(json.dumps({"report": asyncio.run(refund(args))}))


if __name__ == "__main__":
    main()
