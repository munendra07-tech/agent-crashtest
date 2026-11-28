"""`crashtest serve` survives PostgreSQL's slow crash recovery (pgserver's 10 s pg_ctl limit)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytest.importorskip("pgserver")

from pgserver import postgres_server, utils  # noqa: E402

from crashtest.experiment import local_stack  # noqa: E402


class _Info:
    def __init__(self, status: str) -> None:
        self.status = status

    def is_running(self) -> bool:
        return True


class _FakePgserver:
    """get_server times out once (pg_ctl gave up), then returns a handle."""

    def __init__(self) -> None:
        self.calls = 0

    def get_server(self, pgdata: str, cleanup_mode: str):
        self.calls += 1
        if self.calls == 1:
            postgres_server.PostgresServer._instances[Path(pgdata)] = "half-started"
            raise subprocess.TimeoutExpired(["pg_ctl", "start"], 10)
        return "server"


def test_waits_for_recovery_then_connects(tmp_path, monkeypatch):
    statuses = iter(["starting", "starting", "ready"])
    monkeypatch.setattr(
        utils.PostmasterInfo, "read_from_pgdata", classmethod(lambda cls, p: _Info(next(statuses)))
    )
    monkeypatch.setattr(local_stack.time, "sleep", lambda s: None)
    fake = _FakePgserver()

    assert local_stack._start_postgres(fake, tmp_path, recovery_wait_s=30) == "server"
    assert fake.calls == 2
    assert tmp_path not in postgres_server.PostgresServer._instances


def test_gives_up_with_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(utils.PostmasterInfo, "read_from_pgdata", classmethod(lambda cls, p: None))
    with pytest.raises(RuntimeError, match="did not become ready"):
        local_stack._start_postgres(_FakePgserver(), tmp_path, recovery_wait_s=0)


def test_normal_start_is_unchanged(tmp_path):
    class _Ok:
        def get_server(self, pgdata, cleanup_mode):
            return "server"

    assert local_stack._start_postgres(_Ok(), tmp_path) == "server"
