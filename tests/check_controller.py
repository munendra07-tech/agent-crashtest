"""CI check on an installed package: `crashtest controller --stdio` drives a run.

Run with the clean-install environment's Python, from any folder:
    python tests/check_controller.py DATA_DIR

It starts the local stack, launches `crashtest controller --stdio` as a developer's
MCP client would, drives one trial per example agent through it (create_test_run,
arm_fault, the agent, get_test_report), and checks that the naive agent FAILs and
the stable-key agent PASSes, as `crashtest run refunds` says for the same agents.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters

from crashtest import control_token
from crashtest import refund_run as rr
from crashtest.controller import CONTROLLER_TOOLS
from crashtest.experiment.local_stack import local_stack

EXPECTED = {"naive": "FAIL", "stable_key": "PASS"}


def _data(result) -> dict:
    if result.is_error:
        raise RuntimeError(result.content)
    return result.structured_content or json.loads(result.content[0].text)


def _run_agent(agent: str, mcp_url: str, order_id: str) -> str:
    proc = subprocess.run(
        [sys.executable, str(rr.find_builtin_agent(agent)), "--mcp-url", mcp_url,
         "--order-id", order_id, "--amount-cents", "2500", "--timeout", "2"],
        capture_output=True, text=True, timeout=120, env=control_token.agent_env(),
    )  # fmt: skip
    return rr._client_report(proc.stdout)


async def drive(stack, agent: str) -> dict:
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "crashtest.cli", "controller", "--stdio", "--sink-url", stack.sink_url,
              "--mcp-url", stack.mcp_url],
        # The developer's process holds the token; the agent below never gets it.
        env={**os.environ, control_token.ENV: stack.control_token},
    )  # fmt: skip
    async with Client(server) as dev:
        names = {t.name for t in (await dev.list_tools()).tools}
        assert names == set(CONTROLLER_TOOLS), names
        run = _data(await dev.call_tool("create_test_run", {"trials": 1, "agent": agent}))
        order_id = run["trials"][0]["order_id"]
        _data(await dev.call_tool("arm_fault", {"run_id": run["run_id"], "trial": 1}))
        said = await asyncio.to_thread(_run_agent, agent, run["agent_mcp_url"], order_id)
        return _data(
            await dev.call_tool(
                "get_test_report",
                {"run_id": run["run_id"], "agent_reports": {"1": said}, "format": "text"},
            )
        )


def main() -> int:
    data_dir = Path(sys.argv[1] if len(sys.argv) > 1 else ".crashtest-ctl-pgdata").resolve()
    failures = []
    with local_stack(data_dir) as stack:
        for agent, expected in EXPECTED.items():
            out = asyncio.run(drive(stack, agent))
            print(out["report"])
            if out["result"] != expected:
                failures.append(f"{agent}: expected {expected}, got {out['result']}")
    for f in failures:
        print(f"FAILED: {f}")
    print("controller check: " + ("FAILED" if failures else "both example agents as expected"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
