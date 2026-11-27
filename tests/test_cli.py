"""The `crashtest` command: serve, arm and verdict."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import httpx
import pytest

from crashtest import cli


def _truth(*amounts: int, requests: int | None = None) -> dict:
    refunds = [{"order_id": "o", "amount_cents": a} for a in amounts]
    return {
        "refunds": refunds,
        "requests": [{}] * (requests if requests is not None else len(refunds)),
    }


@pytest.mark.parametrize(
    ("truth", "report", "verdict", "agrees"),
    [
        (_truth(2500), "done", "correct", True),
        (_truth(2500, 2500), "done", "duplicate", False),
        (_truth(), "not_done", "stuck", True),  # a definite failure: the contract's `stuck`
        (_truth(2500), "unknown", "in_doubt_surfaced", True),
        (_truth(999), "done", "wrong_payload", True),
    ],
)
def test_judge_uses_the_checker(truth, report, verdict, agrees):
    result = cli.judge(truth, order_id="o", amount_cents=2500, report=report)
    assert (result["verdict"], result["report_matches_sink"]) == (verdict, agrees)
    assert result["refunds"] == len(truth["refunds"])


def test_the_sink_url_comes_from_the_flag_then_the_environment_then_the_state_file(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CRASHTEST_SINK_URL", raising=False)
    assert cli._sink_url(None) == cli.DOCKER_SINK_URL
    (tmp_path / cli.STATE_FILE).write_text(json.dumps({"sink_url": "http://s:1/"}))
    assert cli._sink_url(None) == "http://s:1"
    monkeypatch.setenv("CRASHTEST_SINK_URL", "http://e:2")
    assert cli._sink_url(None) == "http://e:2"
    assert cli._sink_url("http://f:3") == "http://f:3"


def _verdict(order_id: str, sink_url: str, capsys, *extra: str) -> tuple[int, dict]:
    code = cli.main(["verdict", order_id, "--sink-url", sink_url, "--json", *extra])
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _refund(sink_url: str, order_id: str, key: str | None = None) -> None:
    httpx.post(
        f"{sink_url}/refunds",
        json={"order_id": order_id, "amount_cents": 2500},
        headers={"Idempotency-Key": key} if key else {},
        timeout=30.0,
    ).raise_for_status()


def test_verdict_against_the_sink(stack, capsys):
    once, twice, keyed = (f"ord-{uuid.uuid4()}" for _ in range(3))
    _refund(stack.sink_url, once)
    _refund(stack.sink_url, twice)
    _refund(stack.sink_url, twice)  # an unsafe retry: no key
    _refund(stack.sink_url, keyed, key="k-" + keyed)
    _refund(stack.sink_url, keyed, key="k-" + keyed)  # the same key: deduplicated

    assert _verdict(once, stack.sink_url, capsys)[0] == 0
    code, result = _verdict(twice, stack.sink_url, capsys)
    assert (code, result["refunds"], result["verdict"]) == (1, 2, "duplicate")
    assert result["report_matches_sink"] is False
    code, result = _verdict(keyed, stack.sink_url, capsys)
    assert (code, result["refunds"], result["requests"], result["verdict"]) == (0, 1, 2, "correct")


def test_arm_holds_the_next_refund(stack, capsys):
    order_id = f"ord-{uuid.uuid4()}"
    assert cli.main(["arm", order_id, "--delay", "1.0", "--sink-url", stack.sink_url]) == 0
    assert "Armed" in capsys.readouterr().out

    started = time.perf_counter()
    _refund(stack.sink_url, order_id)
    assert time.perf_counter() - started >= 0.95  # committed, then the response was held
    assert _verdict(order_id, stack.sink_url, capsys)[1]["refunds"] == 1


def test_verdict_reports_an_unreachable_sink(capsys):
    assert cli.main(["verdict", "o", "--sink-url", "http://127.0.0.1:9"]) == 2
    assert "cannot reach the sink" in capsys.readouterr().err


def test_serve_starts_the_stack_and_writes_its_urls(tmp_path, monkeypatch):
    pytest.importorskip("pgserver")
    monkeypatch.chdir(tmp_path)
    seen: dict = {}

    def stop_after_checking() -> None:
        state = json.loads(Path(cli.STATE_FILE).read_text(encoding="utf-8"))
        seen.update(state)
        seen["health"] = httpx.get(f"{state['sink_url']}/health", timeout=10).status_code
        seen["anonymous_reset"] = httpx.post(
            f"{state['sink_url']}/control/reset", timeout=10
        ).status_code
        raise KeyboardInterrupt  # what Ctrl+C does

    monkeypatch.setattr(cli, "_idle", stop_after_checking)
    assert cli.main(["serve", "--data-dir", str(tmp_path / "pg")]) == 0

    assert seen["health"] == 200
    assert seen["mcp_url"].endswith("/mcp")
    # The mid-stream cut's server, for the test controller.
    assert seen["mcp_urls"]["stateful_sse_progress"].endswith("/mcp")
    assert seen["mcp_urls"]["stateless_json"] == seen["mcp_url"]
    assert len(seen["control_token"]) >= 32  # stored for the CLI, required by the sink
    assert seen["anonymous_reset"] == 401
    assert not (tmp_path / cli.STATE_FILE).exists()  # removed on the way out


def test_version_prints_the_installed_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"agent-crashtest {cli._version()}"


def _dead_pid() -> int:
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_a_stack_file_left_by_a_killed_serve_is_removed(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CRASHTEST_SINK_URL", raising=False)
    pid = _dead_pid()
    assert not cli._pid_alive(pid)
    (tmp_path / cli.STATE_FILE).write_text(json.dumps({"sink_url": "http://s:1/", "pid": pid}))
    assert cli._sink_url(None) == cli.DOCKER_SINK_URL  # as if no stack were running
    assert not (tmp_path / cli.STATE_FILE).exists()
    assert f"process {pid}" in capsys.readouterr().err


def test_a_stack_file_of_a_running_serve_is_kept(tmp_path, monkeypatch):
    import os

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CRASHTEST_SINK_URL", raising=False)
    (tmp_path / cli.STATE_FILE).write_text(
        json.dumps({"sink_url": "http://s:1/", "pid": os.getpid()})
    )
    assert cli._pid_alive(os.getpid())
    assert cli._sink_url(None) == "http://s:1"
    assert (tmp_path / cli.STATE_FILE).exists()
