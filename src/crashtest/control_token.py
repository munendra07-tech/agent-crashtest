"""The control token: what the sink requires on `/control/*`.

The agent under test is untrusted. If it learned the sink's URL it could re-arm or
clear faults, or call `/control/reset` and erase the evidence of a duplicate. So
the sink refuses every `/control/*` call that does not carry the token, and the
token is never given to the agent: not in MCP output, not in the agent's
environment, not on the command line. `GET /refunds` (read-only) stays open.

Where the token comes from:
  the sink       SINK_CONTROL_TOKEN (set by `crashtest serve` / the local stack;
                 Docker Compose takes it from CRASHTEST_CONTROL_TOKEN)
  the harness    a stack started in this process registers its token here;
                 otherwise CRASHTEST_CONTROL_TOKEN, or `.crashtest-stack.json`
                 for the CLI
"""

from __future__ import annotations

import os
import secrets

ENV = "CRASHTEST_CONTROL_TOKEN"
SINK_ENV = "SINK_CONTROL_TOKEN"
HEADER = "Authorization"
SCHEME = "Bearer"

# Tokens of stacks started in this process, by sink URL (see local_stack).
_REGISTERED: dict[str, str] = {}


def new_token() -> str:
    return secrets.token_urlsafe(32)


def register(sink_url: str, token: str) -> None:
    _REGISTERED[sink_url.rstrip("/")] = token


def unregister(sink_url: str) -> None:
    _REGISTERED.pop(sink_url.rstrip("/"), None)


def token_for(sink_url: str | None = None) -> str | None:
    """The token for this sink: a stack registered in this process, else the env."""
    if sink_url is not None and (token := _REGISTERED.get(sink_url.rstrip("/"))):
        return token
    return os.environ.get(ENV) or None


def headers(token: str | None) -> dict[str, str]:
    return {HEADER: f"{SCHEME} {token}"} if token else {}


def control_headers(sink_url: str | None = None) -> dict[str, str]:
    """Headers for a client that calls the sink's `/control/*` endpoints."""
    return headers(token_for(sink_url))


def agent_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment without the token, for a process under test."""
    source = os.environ if env is None else env
    return {k: v for k, v in source.items() if k not in (ENV, SINK_ENV)}


def matches(presented: str | None, expected: str | None) -> bool:
    """Constant-time check of an `Authorization` header value against the token."""
    if not expected or not presented:
        return False
    scheme, _, value = presented.partition(" ")
    if scheme.lower() != SCHEME.lower():
        return False
    return secrets.compare_digest(value.strip().encode(), expected.encode())
