"""Side-effect sink: a fake payments API that records every refund it performs.

Run with:  python -m crashtest.sink
Needs:     DATABASE_URL (PostgreSQL connection string)
           SINK_CONTROL_TOKEN (the control token; without it every /control/*
           call is refused, see crashtest.control_token)

The sink is deliberately simple. Its job is to be the ground truth for
"how many times did the effect actually happen", and to inject one fault:
commit the refund, then hold the response long enough that the caller gives up.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
from importlib import resources
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from crashtest import control_token


class RefundRequest(BaseModel):
    order_id: str = Field(min_length=1, max_length=200)
    amount_cents: int = Field(gt=0)


class RefundResponse(BaseModel):
    refund_id: int
    order_id: str
    amount_cents: int
    replayed: bool


class FaultRequest(BaseModel):
    order_id: str = Field(min_length=1, max_length=200)
    mode: Literal["commit_then_delay"] = "commit_then_delay"
    delay_s: float = Field(ge=0, le=60)
    times: int = Field(default=1, ge=0, le=100)


def _request_hash(body: RefundRequest) -> str:
    """Fingerprint of the business payload, to detect a key reused for a different request."""
    canonical = json.dumps(body.model_dump(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = AsyncConnectionPool(
        os.environ["DATABASE_URL"],
        min_size=2,
        max_size=20,
        open=False,
        kwargs={"row_factory": dict_row},
    )
    await pool.open(wait=True)
    schema = resources.files("crashtest.sink").joinpath("schema.sql").read_text()
    async with pool.connection() as conn:
        await conn.execute(schema)
    app.state.pool = pool
    try:
        yield
    finally:
        await pool.close()


app = FastAPI(title="agent-crashtest sink", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    async with app.state.pool.connection() as conn:
        await conn.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/refunds", response_model=RefundResponse)
async def create_refund(
    body: RefundRequest,
    # An empty key is rejected: treated as a key, it would deduplicate every
    # request that sent one against every other.
    idempotency_key: Annotated[str | None, Header(min_length=1, max_length=200)] = None,
) -> RefundResponse:
    req_hash = _request_hash(body)
    conflict = False

    # One transaction: the effect row and its request-log row commit together.
    async with app.state.pool.connection() as conn, conn.transaction():
        row = None
        if idempotency_key is None:
            cur = await conn.execute(
                "INSERT INTO refunds (order_id, amount_cents, request_hash) "
                "VALUES (%s, %s, %s) RETURNING id",
                (body.order_id, body.amount_cents, req_hash),
            )
            row = await cur.fetchone()
            replayed = False
        else:
            # ON CONFLICT DO NOTHING is the atomic claim. If another request
            # with this key is mid-insert, this statement waits for it to
            # commit and then inserts nothing.
            cur = await conn.execute(
                "INSERT INTO refunds (order_id, amount_cents, idempotency_key, request_hash) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING "
                "RETURNING id",
                (body.order_id, body.amount_cents, idempotency_key, req_hash),
            )
            row = await cur.fetchone()
            replayed = row is None
            if replayed:
                cur = await conn.execute(
                    "SELECT id, request_hash FROM refunds WHERE idempotency_key = %s",
                    (idempotency_key,),
                )
                existing = await cur.fetchone()
                # Same key, different payload: refuse instead of silently
                # returning a refund for some other request.
                conflict = existing["request_hash"] != req_hash
                row = {"id": existing["id"]}

        outcome = "conflict" if conflict else ("replayed" if replayed else "created")

        # Only a request that creates an effect consumes an armed fault. A replay
        # or a conflict commits nothing new, so there is no "after the commit"
        # window for the fault to sit in, and the fault stays armed.
        fault = None
        if outcome == "created":
            cur = await conn.execute(
                "UPDATE faults SET remaining = remaining - 1 "
                "WHERE order_id = %s AND remaining > 0 RETURNING delay_s",
                (body.order_id,),
            )
            fault = await cur.fetchone()

        # The log row records whether this request had a fault applied, so a
        # results file can prove the fault fired instead of inferring it.
        await conn.execute(
            "INSERT INTO request_log "
            "(order_id, idempotency_key, outcome, fault_applied, refund_id) "
            "VALUES (%s, %s, %s, %s, %s)",
            (body.order_id, idempotency_key, outcome, fault is not None, row["id"]),
        )
    # The transaction has committed here. Everything below happens after the
    # effect is durable, which is exactly the "lost response" window.

    if conflict:
        raise HTTPException(409, "Idempotency-Key was already used with a different request body")

    if fault is not None:
        await asyncio.sleep(fault["delay_s"])

    return RefundResponse(
        refund_id=row["id"],
        order_id=body.order_id,
        amount_cents=body.amount_cents,
        replayed=replayed,
    )


@app.get("/refunds")
async def list_refunds(order_id: Annotated[str, Query(min_length=1)]) -> dict:
    """Ground truth for the checker: effects committed and requests received for one order."""
    async with app.state.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, amount_cents, idempotency_key, created_at FROM refunds "
            "WHERE order_id = %s ORDER BY id",
            (order_id,),
        )
        refunds = await cur.fetchall()
        cur = await conn.execute(
            "SELECT idempotency_key, outcome, fault_applied, refund_id, received_at "
            "FROM request_log "
            "WHERE order_id = %s ORDER BY id",
            (order_id,),
        )
        requests = await cur.fetchall()
    return {"order_id": order_id, "refunds": refunds, "requests": requests}


def require_control_token(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Refuse a /control/* call without the control token (the agent never has it)."""
    expected = os.environ.get(control_token.SINK_ENV)
    if not expected:
        raise HTTPException(
            503, "control is disabled: the sink was started without SINK_CONTROL_TOKEN"
        )
    if not control_token.matches(authorization, expected):
        raise HTTPException(
            401,
            "the control token is missing or wrong",
            headers={"WWW-Authenticate": control_token.SCHEME},
        )


@app.put("/control/faults", dependencies=[Depends(require_control_token)])
async def arm_fault(body: FaultRequest) -> dict:
    async with app.state.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO faults (order_id, mode, delay_s, remaining) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (order_id) DO UPDATE SET mode = EXCLUDED.mode, "
            "delay_s = EXCLUDED.delay_s, remaining = EXCLUDED.remaining",
            (body.order_id, body.mode, body.delay_s, body.times),
        )
    return {"armed": body.model_dump()}


@app.post("/control/reset", dependencies=[Depends(require_control_token)])
async def reset() -> dict:
    """Test helper: wipe all state."""
    async with app.state.pool.connection() as conn:
        await conn.execute("TRUNCATE refunds, request_log, faults RESTART IDENTITY")
    return {"reset": True}
