"""An unsafe agent: on a timeout it calls `issue_refund` again, with no idempotency key.

`crashtest run refunds --agent naive` runs this script. Expect FAIL: the first
refund commits, its response is lost, the retry commits a second refund, and the
agent tells its caller that one refund was made.

The agent contract (copy this file to start your own agent):
  arguments  --mcp-url URL --order-id ID [--amount-cents N] [--timeout SECONDS]
  stdout     the last line is one JSON object with "report": what the agent tells
             its caller about the refund: "done", "unknown" or "not_done"
"""

from __future__ import annotations

import argparse
import asyncio
import json

from mcp import Client

MAX_ATTEMPTS = 3


async def on_progress(progress: float, total: float | None, message: str | None) -> None:
    """Asking for progress is what makes the server stream; the values are not used."""


async def refund(args: argparse.Namespace) -> str:
    arguments = {"order_id": args.order_id, "amount_cents": args.amount_cents}
    for _ in range(MAX_ATTEMPTS):
        try:
            async with Client(args.mcp_url) as client:
                result = await client.call_tool(
                    "issue_refund",
                    arguments,
                    read_timeout_seconds=args.timeout,
                    progress_callback=on_progress,  # refunds-cut-mid-stream needs a stream
                )
            return "not_done" if result.is_error else "done"
        except (Exception, BaseExceptionGroup):
            continue  # UNSAFE: the first refund may have committed; this sends another
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
