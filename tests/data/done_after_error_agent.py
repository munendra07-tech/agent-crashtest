"""A test agent that reports "done" even when its one refund call failed.

It calls `issue_refund` once, never retries, and tells its caller "done" whatever
happened. Under the refunds scenario the refund commits before the response is
lost, so the sink holds one refund and this agent PASSes. Under
refunds-cut-before-request the connection is cut before the request, so no refund
exists, and it FAILs (verdict lost). The Action's CI job uses it to show why
`scenario: all` matters.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json

from mcp import Client


async def refund(args: argparse.Namespace) -> str:
    arguments = {"order_id": args.order_id, "amount_cents": args.amount_cents}
    with contextlib.suppress(Exception, BaseExceptionGroup):
        async with Client(args.mcp_url) as client:
            await client.call_tool("issue_refund", arguments, read_timeout_seconds=args.timeout)
    return "done"  # WRONG: says done without knowing


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
