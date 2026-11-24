"""One agent, two transports: the example agents' logic over HTTP or over stdio.

Used by the transport-parity test (tests/test_connect_security.py) and by CI's
clean-install job (tests/check_transport_parity.py): the same agent behaviour must
get the same verdict whichever transport carries its tool calls.

Same arguments as examples/stable_key_agent.py. The mode and transport come from
the environment, because `crashtest run` passes only the standard arguments:
  CRASHTEST_TEST_AGENT_MODE       naive (no key) or stable_key (refund-<order id>)
  CRASHTEST_TEST_AGENT_TRANSPORT  http (--mcp-url) or stdio
  CRASHTEST_TEST_AGENT_SINK_URL   for stdio: the sink `crashtest mcp --stdio` uses
Over stdio each attempt launches `crashtest mcp --stdio`, as a desktop client would.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from mcp import Client, StdioServerParameters

MAX_ATTEMPTS = 3


def _server(args: argparse.Namespace) -> str | StdioServerParameters:
    if os.environ.get("CRASHTEST_TEST_AGENT_TRANSPORT", "http") == "http":
        return args.mcp_url
    return StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "crashtest.cli",
            "mcp",
            "--stdio",
            "--sink-url",
            os.environ["CRASHTEST_TEST_AGENT_SINK_URL"],
        ],
        env=dict(os.environ),
    )


async def refund(args: argparse.Namespace) -> str:
    arguments: dict[str, object] = {"order_id": args.order_id, "amount_cents": args.amount_cents}
    if os.environ.get("CRASHTEST_TEST_AGENT_MODE") == "stable_key":
        arguments["idempotency_key"] = f"refund-{args.order_id}"
    for _ in range(MAX_ATTEMPTS):
        try:
            async with Client(_server(args)) as client:
                result = await client.call_tool(
                    "issue_refund", arguments, read_timeout_seconds=args.timeout
                )
            return "not_done" if result.is_error else "done"
        except (Exception, BaseExceptionGroup):
            continue
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
