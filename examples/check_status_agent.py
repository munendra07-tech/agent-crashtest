"""Check refund status after a timeout, before retrying the operation.

This passes both scenarios, but a lookup is weaker than an enforced idempotency
key: in a real system the first request can still commit after the lookup says
"none" (or another worker can act in between), and the retry then refunds twice.
Use a lookup on top of a key, not instead of one.

The agent contract matches `stable_key_agent.py`:
  arguments  --mcp-url URL --order-id ID [--amount-cents N] [--timeout SECONDS]
  stdout     the last line is one JSON object with "report": "done", "unknown"
             or "not_done"
"""

from __future__ import annotations

import argparse
import asyncio
import json

from mcp import Client

MAX_ATTEMPTS = 3


async def _refund_exists(args: argparse.Namespace) -> bool | None:
    """Read refund existence through MCP, returning None when status is unknown.

    @param args: The agent arguments containing MCP URL, order ID, and timeout.
    @returns: True or False when status is known, otherwise None.
    """
    try:
        async with Client(args.mcp_url) as client:
            result = await client.call_tool(
                "check_refund_status",
                {"order_id": args.order_id},
                read_timeout_seconds=args.timeout,
            )
    except (Exception, BaseExceptionGroup):
        return None

    status = result.structured_content
    if (
        result.is_error
        or not isinstance(status, dict)
        or not isinstance(status.get("exists"), bool)
    ):
        return None
    return status["exists"]


async def refund(args: argparse.Namespace) -> str:
    """Try the refund and check its status before retrying after a timeout.

    @param args: The agent arguments containing MCP URL, order ID, amount, and timeout.
    @returns: The report for the caller: done, not_done, or unknown.
    """
    arguments = {"order_id": args.order_id, "amount_cents": args.amount_cents}
    for attempt in range(MAX_ATTEMPTS):
        try:
            async with Client(args.mcp_url) as client:
                result = await client.call_tool(
                    "issue_refund", arguments, read_timeout_seconds=args.timeout
                )
            return "not_done" if result.is_error else "done"
        except (Exception, BaseExceptionGroup):
            exists = await _refund_exists(args)
            if exists is None:
                return "unknown"
            if exists:
                return "done"
            if attempt == MAX_ATTEMPTS - 1:
                return "unknown"
    return "unknown"


def main() -> None:
    """Parse the command line and print the agent report.

    @param none: This entry point reads its arguments from the process command line.
    @returns: None.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp-url", required=True)
    parser.add_argument("--order-id", required=True)
    parser.add_argument("--amount-cents", type=int, default=2500)
    parser.add_argument("--timeout", type=float, default=2.0)
    args = parser.parse_args()
    print(json.dumps({"report": asyncio.run(refund(args))}))


if __name__ == "__main__":
    main()
