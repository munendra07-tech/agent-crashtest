"""Start the whole stack on this machine without Docker.

PostgreSQL comes from the `pgserver` package (real PostgreSQL binaries installed
by pip). The sink and the MCP server run as ordinary subprocesses. This is the
same code the Docker images run; only the way the processes are started differs.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from crashtest import control_token

# Transport name -> (stateless, json_response) for the MCP server.
TRANSPORTS: dict[str, tuple[bool, bool]] = {
    "stateless_json": (True, True),
    "stateless_sse": (True, False),
    "stateful_json": (False, True),
    "stateful_sse": (False, False),
}
DEFAULT_TRANSPORT = "stateless_json"

# Experiment 007: the stateful SSE server again, with an in-memory event store so
# its streams can be resumed. Kept apart from TRANSPORTS so that "all" in
# experiments 001 to 003 still means the four servers they were run on.
EVENT_STORE_TRANSPORTS: dict[str, tuple[bool, bool]] = {
    "stateful_sse_store": (False, False),
}

# Experiment 008: the stateful SSE servers of 007 again, with a tool that sends one
# progress notification and then pauses this long before it calls the sink, so
# that a connection can be cut mid-stream before the commit. Kept apart for the
# same reason as above.
PROGRESS_DELAY_S = 3.0
PROGRESS_TRANSPORTS: dict[str, tuple[bool, bool]] = {
    "stateful_sse_progress": (False, False),
    "stateful_sse_store_progress": (False, False),
}
SERVED_TRANSPORTS: dict[str, tuple[bool, bool]] = {
    **TRANSPORTS,
    **EVENT_STORE_TRANSPORTS,
    **PROGRESS_TRANSPORTS,
}


def _has_event_store(name: str) -> bool:
    return name in EVENT_STORE_TRANSPORTS or name == "stateful_sse_store_progress"


@dataclass
class Stack:
    sink_url: str
    mcp_url: str  # the default transport
    database_url: str
    mcp_urls: dict[str, str] = field(default_factory=dict)  # one MCP server per transport
    # What the sink requires on /control/*. Never given to the agent under test.
    control_token: str = ""

    def __post_init__(self) -> None:
        self.mcp_urls.setdefault(DEFAULT_TRANSPORT, self.mcp_url)


_ISSUED_PORTS: set[int] = set()


def _free_port() -> int:
    """A port that is free now and has not been handed out before in this process.

    The port is closed before a server binds it, so another process could still
    take it in between; but the five servers of one stack can no longer be given
    the same port, which the operating system may otherwise do because none of
    them has bound it yet.
    """
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in _ISSUED_PORTS:
            _ISSUED_PORTS.add(port)
            return port


def _wait_for_http(url: str, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=2.0).status_code < 500:
                return
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(0.2)
    raise RuntimeError(f"{url} did not come up within {timeout_s}s: {last_error}")


def _wait_for_port(port: int, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"port {port} did not open within {timeout_s}s")


@contextmanager
def local_stack(
    pgdata: str | os.PathLike[str] | None = None,
    transports: Iterable[str] = (DEFAULT_TRANSPORT,),
) -> Iterator[Stack]:
    """Start PostgreSQL, the sink, and one MCP server per requested transport."""
    import pgserver  # imported here so the Docker path does not need it

    names = list(dict.fromkeys([DEFAULT_TRANSPORT, *transports]))
    unknown = [n for n in names if n not in SERVED_TRANSPORTS]
    if unknown:
        raise ValueError(f"unknown transports: {unknown}")

    pgdata_path = Path(pgdata or os.environ.get("CRASHTEST_PGDATA", ".pgdata")).resolve()
    pgdata_path.mkdir(parents=True, exist_ok=True)
    pg = pgserver.get_server(str(pgdata_path), cleanup_mode="stop")
    database_url = pg.get_uri()

    sink_port = _free_port()
    sink_url = f"http://127.0.0.1:{sink_port}"
    token = control_token.new_token()
    # The MCP servers get no token: they only call POST /refunds.
    env = {
        **control_token.agent_env(),
        "DATABASE_URL": database_url,
        "SINK_URL": sink_url,
        "SINK_HOST": "127.0.0.1",
        "SINK_PORT": str(sink_port),
        "MCP_HOST": "127.0.0.1",
    }

    procs: list[subprocess.Popen] = []
    # Registered for this process only (crashtest.control_token.control_headers);
    # not put in os.environ, so agent subprocesses do not inherit it.
    control_token.register(sink_url, token)
    try:
        procs.append(
            subprocess.Popen(
                [sys.executable, "-m", "crashtest.sink"],
                env={**env, control_token.SINK_ENV: token},
            )
        )
        _wait_for_http(f"{sink_url}/health")
        mcp_ports: dict[str, int] = {}
        for name in names:
            stateless, json_response = SERVED_TRANSPORTS[name]
            mcp_ports[name] = _free_port()
            procs.append(
                subprocess.Popen(
                    [sys.executable, "-m", "crashtest.refund_mcp.server"],
                    env={
                        **env,
                        "MCP_PORT": str(mcp_ports[name]),
                        "MCP_STATELESS": str(stateless).lower(),
                        "MCP_JSON_RESPONSE": str(json_response).lower(),
                        "MCP_EVENT_STORE": str(_has_event_store(name)).lower(),
                        "MCP_PROGRESS_DELAY_S": (
                            str(PROGRESS_DELAY_S) if name in PROGRESS_TRANSPORTS else ""
                        ),
                    },
                )
            )
        for port in mcp_ports.values():
            _wait_for_port(port)
        mcp_urls = {name: f"http://127.0.0.1:{port}/mcp" for name, port in mcp_ports.items()}
        yield Stack(
            sink_url=sink_url,
            mcp_url=mcp_urls[DEFAULT_TRANSPORT],
            database_url=database_url,
            mcp_urls=mcp_urls,
            control_token=token,
        )
    finally:
        control_token.unregister(sink_url)
        for p in reversed(procs):
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        pg.cleanup()
