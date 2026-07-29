"""Contracts for durable watcher submission."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from chainwake.jobs import service
from chainwake.jobs.config import DURABLE_ARGV_ENV_VAR, NO_AUTOSTART_ENV_VAR
from chainwake.jobs.models import JobState
from chainwake.jobs.store import JobStore

pytestmark = pytest.mark.unit


def test_submission_is_failed_when_daemon_cannot_start(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHAINWAKE_HOME", str(tmp_path))
    monkeypatch.setenv(
        DURABLE_ARGV_ENV_VAR,
        '["bt","subnet","19","price","--below","0.05"]',
    )
    monkeypatch.delenv(NO_AUTOSTART_ENV_VAR, raising=False)
    monkeypatch.setattr(service, "ensure_daemon", lambda: False)
    monkeypatch.setattr(service, "emit_job_value", lambda *_args, **_kwargs: None)
    spec = cast(Any, SimpleNamespace(chain="bt", max_runtime_seconds=None))

    submitted = service.submit_durable_job(spec, out_uris=[])

    assert submitted.state == JobState.FAILED
    assert submitted.exit_code == 4
    assert submitted.error == "durable-job supervisor failed to start"
    persisted = JobStore(tmp_path / "jobs.sqlite3").get(submitted.id)
    assert persisted == submitted


def test_submission_rejects_literal_rpc_url(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHAINWAKE_HOME", str(tmp_path))
    monkeypatch.setenv(
        DURABLE_ARGV_ENV_VAR,
        '["bt","subnet","19","price","--below","0.05","--rpc-url","wss://token@rpc.test"]',
    )
    spec = cast(Any, SimpleNamespace(chain="bt", max_runtime_seconds=None))

    with pytest.raises(ValueError, match="CHAINWAKE_BT_RPC_URL"):
        service.submit_durable_job(spec, out_uris=[])
