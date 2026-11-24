"""A LangGraph agent under test, with a scripted chat model: no API key, no network model.

    crashtest run refunds --agent examples/frameworks/langgraph_agent.py \
        --python PATH/TO/A/PYTHON/WITH/langgraph-and-langchain-mcp-adapters

The agent is LangGraph's prebuilt ReAct agent (`create_react_agent`) with the refund
tool loaded through `langchain-mcp-adapters`. The model is `ScriptedChatModel`, a
few lines of Python that play the model: it asks for `issue_refund`; when the tool
call fails, it asks for the same call again, as a model typically does; once a call
succeeds it answers "done". Nothing here calls a model provider.

The tool node is `ToolNode(tools, handle_tool_errors=True)`, so a failed call (here
the MCP client's timeout) reaches the model as an error message. With LangGraph's
default tool node (langgraph 1.2.22, langchain-mcp-adapters 0.3.2, mcp 1.30.0) the
timeout was not caught: the run raised, this script reported "unknown", and the
sink held the one refund the first call made (checked by hand, 25 November 2026).

The environment variable REFUND_KEYING chooses what the scripted model sends,
because `crashtest run` passes only the standard agent arguments:
  stable_key (default)  idempotency_key "refund-<order id>" on every call: PASS
  naive                 no key: the repeated call refunds again: FAIL (a duplicate)

Same contract as examples/stable_key_agent.py: --mcp-url, --order-id,
--amount-cents, --timeout; the last stdout line is {"report": ...}.
Pinned and checked in CI with langgraph==1.2.22 and langchain-mcp-adapters==0.3.2.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import timedelta
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import ToolNode, create_react_agent

MAX_CALLS = 3


class ScriptedChatModel(BaseChatModel):
    """Plays the model: call issue_refund until one call succeeds, then say done."""

    arguments: dict[str, Any]

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedChatModel:
        return self  # the script already knows its one tool

    def _generate(self, messages: list[BaseMessage], *args: Any, **kwargs: Any) -> ChatResult:
        results = [m for m in messages if isinstance(m, ToolMessage)]
        if results and results[-1].status != "error":
            reply = AIMessage(content="done")
        elif len(results) >= MAX_CALLS:
            reply = AIMessage(content="unknown")  # never "done" without a success result
        else:
            call = {"name": "issue_refund", "args": self.arguments, "id": f"call-{len(results)}"}
            reply = AIMessage(content="", tool_calls=[call])
        return ChatResult(generations=[ChatGeneration(message=reply)])


async def refund(args: argparse.Namespace) -> str:
    arguments: dict[str, Any] = {"order_id": args.order_id, "amount_cents": args.amount_cents}
    if os.environ.get("REFUND_KEYING", "stable_key") != "naive":
        arguments["idempotency_key"] = f"refund-{args.order_id}"
    client = MultiServerMCPClient(
        {
            "refunds": {
                "transport": "streamable_http",
                "url": args.mcp_url,
                "session_kwargs": {"read_timeout_seconds": timedelta(seconds=args.timeout)},
            }
        }
    )
    tools = await client.get_tools()
    agent = create_react_agent(
        ScriptedChatModel(arguments=arguments), ToolNode(tools, handle_tool_errors=True)
    )
    try:
        state = await agent.ainvoke({"messages": [("user", "Refund this order once.")]})
    except (Exception, BaseExceptionGroup):
        return "unknown"
    answer = state["messages"][-1].content
    return answer if answer in ("done", "unknown") else "not_done"


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
