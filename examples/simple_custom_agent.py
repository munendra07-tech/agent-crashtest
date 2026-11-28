"""Example agent using a simple custom client for agent-crashtest.

Usage:
  python examples/simple_custom_agent.py --mcp-url URL --order-id ID [--amount-cents N] [--timeout SECONDS]
"""

import argparse
import json
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mcp-url", required=True)
    parser.add_argument("--order-id", required=True)
    parser.add_argument("--amount-cents", type=int, default=1000)
    parser.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args()

    # Simulate a simple agent that eventually reports done
    time.sleep(0.5)
    report = {"report": "done"}
    print(json.dumps(report))
    sys.exit(0)


if __name__ == "__main__":
    main()
