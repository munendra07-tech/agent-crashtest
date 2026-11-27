"""End-to-end: MCP client -> MCP server -> sink -> PostgreSQL.

Each test runs one real trial and asserts on what the sink recorded.
"""

from __future__ import annotations

import pytest
from mcp import Client

from crashtest.experiment.trial import ARMS, run_trial

FAST = {"client_timeout_s": 1.5, "fault_delay_s": 4.0, "max_attempts": 3}


async def test_tool_is_listed_with_an_optional_idempotency_key(stack):
    async with Client(stack.mcp_url) as client:
        tools = (await client.list_tools()).tools

    tool = next(t for t in tools if t.name == "issue_refund")
    assert set(tool.input_schema["required"]) == {"order_id", "amount_cents"}
    assert "idempotency_key" in tool.input_schema["properties"]


async def test_status_tool_reports_sink_state_without_creating_a_request(stack, sink, order_id):
    """The read-only status tool mirrors sink state without creating a request.

    @param stack: The local MCP and sink test stack.
    @param sink: The HTTP client connected to the test sink.
    @param order_id: A unique order ID for this test.
    @returns: None.
    """
    async with Client(stack.mcp_url) as client:
        absent = await client.call_tool("check_refund_status", {"order_id": order_id})
        await sink.post("/refunds", json={"order_id": order_id, "amount_cents": 2500})
        present = await client.call_tool("check_refund_status", {"order_id": order_id})

    truth = (await sink.get("/refunds", params={"order_id": order_id})).json()
    assert not absent.is_error and absent.structured_content == {"exists": False}
    assert not present.is_error and present.structured_content == {"exists": True}
    assert len(truth["refunds"]) == len(truth["requests"]) == 1


@pytest.mark.parametrize("arm", ARMS)
async def test_without_a_fault_every_arm_refunds_exactly_once(stack, arm):
    result = await run_trial(
        arm=arm, fault="none", mcp_url=stack.mcp_url, sink_url=stack.sink_url, **FAST
    )

    assert result.verdict == "correct"
    assert result.effects_committed == 1
    assert result.requests_received == 1
    assert [a.outcome for a in result.attempts] == ["ok"]


async def test_lost_response_with_naive_retry_refunds_twice(stack):
    result = await run_trial(
        arm="no_dedup",
        fault="lost_response",
        mcp_url=stack.mcp_url,
        sink_url=stack.sink_url,
        **FAST,
    )

    assert [a.outcome for a in result.attempts] == ["timeout", "ok"]
    assert result.client_saw_success is True  # the client believes it did one refund
    assert result.effects_committed == 2
    assert result.verdict == "duplicate"


async def test_lost_response_with_a_stable_key_refunds_once(stack):
    result = await run_trial(
        arm="stable_key",
        fault="lost_response",
        mcp_url=stack.mcp_url,
        sink_url=stack.sink_url,
        **FAST,
    )

    assert [a.outcome for a in result.attempts] == ["timeout", "ok"]
    assert result.attempts[0].idempotency_key == result.attempts[1].idempotency_key
    assert result.attempts[1].replayed is True  # the retry got the original refund back
    assert result.requests_received == 2
    assert result.effects_committed == 1
    assert result.verdict == "correct"


async def test_lost_response_with_a_new_key_per_attempt_refunds_twice(stack):
    result = await run_trial(
        arm="fresh_key_per_attempt",
        fault="lost_response",
        mcp_url=stack.mcp_url,
        sink_url=stack.sink_url,
        **FAST,
    )

    assert result.attempts[0].idempotency_key != result.attempts[1].idempotency_key
    assert result.effects_committed == 2
    assert result.verdict == "duplicate"


async def test_end_of_run_recheck_flags_a_refund_that_landed_after_the_trial(stack, sink):
    from crashtest.experiment.run import recheck_at_end

    result = await run_trial(
        arm="stable_key", fault="none", mcp_url=stack.mcp_url, sink_url=stack.sink_url, **FAST
    )
    untouched = await run_trial(
        arm="stable_key", fault="none", mcp_url=stack.mcp_url, sink_url=stack.sink_url, **FAST
    )
    # A refund for the first order arrives after its trial has finished.
    await sink.post("/refunds", json={"order_id": result.order_id, "amount_cents": 100})

    assert await recheck_at_end(stack.sink_url, [result, untouched]) == 1
    assert result.changed_by_end_of_run is True
    assert untouched.changed_by_end_of_run is False


def test_an_expected_disconnect_is_logged_as_one_line_without_a_traceback():
    import logging

    from starlette.requests import ClientDisconnect

    from crashtest.refund_mcp import server

    def record(exc: BaseException) -> logging.LogRecord:
        return logging.LogRecord(
            "uvicorn.error",
            logging.ERROR,
            __file__,
            1,
            "Exception in ASGI application",
            (),
            (type(exc), exc, None),
        )

    f = server.ExpectedDisconnectFilter()
    quiet = record(ClientDisconnect())
    assert f.filter(quiet)
    assert (quiet.getMessage(), quiet.exc_info, quiet.levelname) == (
        server.DISCONNECT_NOTE,
        None,
        "INFO",
    )
    loud = record(ValueError("a real error"))
    assert f.filter(loud)
    assert loud.exc_info is not None and loud.levelname == "ERROR"
