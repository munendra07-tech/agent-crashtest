"""A safe agent: one idempotency key per order, sent with every attempt.

`crashtest run refunds --agent stable_key` runs this script. Expect PASS: the
retry carries the same key as the lost first call, so the sink returns the stored
refund instead of making a second one.

The key is derived from the business operation (refunding this order), not from
the attempt, and the sink enforces it in its database. A key made fresh for each
attempt would not help.

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
    arguments = {
        "order_id": args.order_id,
        "amount_cents": args.amount_cents,
        "idempotency_key": f"refund-{args.order_id}",  # the same on every attempt
    }
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
            continue  # safe to retry: the key makes a repeat return the stored refund
    return "unknown"  # never "done" without a success result


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
