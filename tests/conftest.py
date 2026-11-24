"""Shared fixtures.

By default the tests start their own stack (pip-installed PostgreSQL plus the
sink and MCP server as subprocesses). To test an already-running stack such as
Docker Compose, set both CRASHTEST_SINK_URL and CRASHTEST_MCP_URL. Servers for the
other transports are read from CRASHTEST_MCP_URL_<TRANSPORT>, for example
CRASHTEST_MCP_URL_STATEFUL_SSE; a transport with no URL is skipped. The sink's
control token then comes from CRASHTEST_CONTROL_TOKEN (Docker Compose requires it).
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crashtest import control_token  # noqa: E402
from crashtest.experiment.local_stack import SERVED_TRANSPORTS, Stack, local_stack  # noqa: E402


@pytest.fixture(scope="session")
def stack() -> Stack:
    sink_url = os.environ.get("CRASHTEST_SINK_URL")
    mcp_url = os.environ.get("CRASHTEST_MCP_URL")
    if sink_url and mcp_url:
        extra = {
            name: url
            for name in SERVED_TRANSPORTS
            if (url := os.environ.get(f"CRASHTEST_MCP_URL_{name.upper()}"))
        }
        yield Stack(
            sink_url=sink_url,
            mcp_url=mcp_url,
            database_url="",
            mcp_urls=extra,
            control_token=os.environ.get(control_token.ENV, ""),
        )
        return
    # Child processes must be able to import the package too.
    src = str(Path(__file__).resolve().parents[1] / "src")
    os.environ["PYTHONPATH"] = src + os.pathsep + os.environ.get("PYTHONPATH", "")
    with local_stack(transports=SERVED_TRANSPORTS) as s:
        yield s


@pytest_asyncio.fixture
async def sink(stack: Stack):
    # The harness's client: it carries the control token, as `crashtest arm` does.
    async with httpx.AsyncClient(
        base_url=stack.sink_url,
        timeout=30.0,
        headers=control_token.headers(stack.control_token),
    ) as client:
        yield client


@pytest.fixture
def order_id() -> str:
    """A fresh order per test, so tests never see each other's refunds."""
    return f"test-{uuid.uuid4()}"
