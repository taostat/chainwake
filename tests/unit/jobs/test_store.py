"""Contracts for the SQLite-backed durable job store."""

from __future__ import annotations

import sqlite3
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any

import pytest

from chainwake.jobs import config
from chainwake.jobs.models import JobCreate, JobState
from chainwake.jobs.store import JobStore

pytestmark = pytest.mark.unit


def _create(*, context: str = "Wake the agent with the observed price.") -> JobCreate:
    return JobCreate(
        argv=["bt", "subnet", "19", "price", "--below", "0.05"],
        context=context,
        deadline_at=datetime.now(UTC) + timedelta(hours=4),
    )


def test_created_job_survives_store_reopen(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    first = JobStore(database)
    created = first.create(_create())

    reopened = JobStore(database).get(created.id)

    assert reopened is not None
    assert reopened.id == created.id
    assert reopened.state == JobState.PENDING
    assert reopened.argv == ["bt", "subnet", "19", "price", "--below", "0.05"]
    assert reopened.context == "Wake the agent with the observed price."
    assert reopened.deadline_at == created.deadline_at


def test_store_closes_each_short_lived_sqlite_connection(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transactions must not leave SQLite handles open in a long-lived daemon."""

    real_connect = sqlite3.connect
    opened: list[_TrackingConnection] = []

    def connect(*args, **kwargs) -> _TrackingConnection:
        connection = _TrackingConnection(real_connect(*args, **kwargs))
        opened.append(connection)
        return connection

    monkeypatch.setattr("chainwake.jobs.store.sqlite3.connect", connect)

    store = JobStore(tmp_path / "jobs.sqlite3")
    job = store.create(_create())
    assert store.get(job.id) is not None

    assert opened
    assert all(connection.closed for connection in opened)


class _TrackingConnection:
    """Record close calls while behaving like a sqlite connection context manager."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.closed = False

    def __enter__(self) -> _TrackingConnection:
        self._connection.__enter__()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._connection.__exit__(exception_type, exception, traceback)

    def close(self) -> None:
        self.closed = True
        self._connection.close()

    @property
    def row_factory(
        self,
    ) -> type[sqlite3.Row] | Callable[[sqlite3.Cursor, tuple[Any, ...]], object] | None:
        return self._connection.row_factory

    @row_factory.setter
    def row_factory(
        self,
        value: type[sqlite3.Row] | Callable[[sqlite3.Cursor, tuple[Any, ...]], object] | None,
    ) -> None:
        self._connection.row_factory = value

    def __getattr__(self, name: str) -> object:
        return getattr(self._connection, name)


def test_claim_is_atomic_across_store_instances(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    first = JobStore(database)
    second = JobStore(database)
    job = first.create(_create())

    assert first.claim(job.id, worker_pid=101, worker_identity="worker-101") is True
    assert second.claim(job.id, worker_pid=202, worker_identity="worker-202") is False

    claimed = second.get(job.id)
    assert claimed is not None
    assert claimed.state == JobState.RUNNING
    assert claimed.worker_pid == 101
    assert claimed.worker_identity == "worker-101"


def test_completion_preserves_context_and_structured_result(tmp_path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    job = store.create(_create(context="Compare the match with the original threshold."))
    assert store.claim(job.id, worker_pid=123, worker_identity="worker-123")
    result = {
        "status": "matched",
        "observed": {"value": 0.049, "block": 1234},
    }

    assert store.complete(job.id, result=result, exit_code=0) is True

    completed = store.get(job.id)
    assert completed is not None
    assert completed.state == JobState.MATCHED
    assert completed.context == "Compare the match with the original threshold."
    assert completed.result == result
    assert completed.exit_code == 0
    assert completed.completed_at is not None


def test_cancelled_job_cannot_be_overwritten_by_late_worker_result(tmp_path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    job = store.create(_create())
    assert store.claim(job.id, worker_pid=123, worker_identity="worker-123")

    assert store.cancel(job.id) is True
    assert (
        store.complete(
            job.id,
            result={"status": "stopped"},
            exit_code=1,
        )
        is False
    )

    cancelled = store.get(job.id)
    assert cancelled is not None
    assert cancelled.state == JobState.CANCELLED
    assert cancelled.worker_pid is None
    assert cancelled.result is None


def test_orphaned_running_job_is_requeued_without_resetting_deadline(tmp_path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    job = store.create(_create())
    assert store.claim(job.id, worker_pid=999_999, worker_identity="old-worker")
    running = store.get(job.id)
    assert running is not None
    original_deadline = running.deadline_at

    recovered = store.requeue_orphans(process_identity=lambda _pid: None)

    assert recovered == [job.id]
    requeued = store.get(job.id)
    assert requeued is not None
    assert requeued.state == JobState.PENDING
    assert requeued.worker_pid is None
    assert requeued.worker_identity is None
    assert requeued.deadline_at == original_deadline


def test_context_has_a_bounded_size(tmp_path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")

    with pytest.raises(ValueError, match="context"):
        store.create(_create(context="x" * 8_193))


def test_state_directory_and_sqlite_files_are_private(tmp_path) -> None:
    state = tmp_path / "state"
    database = state / "jobs.sqlite3"
    store = JobStore(database)
    store.create(_create(context="sensitive wake context"))

    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    for sidecar in (
        database.with_name(f"{database.name}-wal"),
        database.with_name(f"{database.name}-shm"),
    ):
        if sidecar.exists():
            assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600


def test_existing_shared_database_parent_is_rejected_without_chmod(tmp_path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)

    with pytest.raises(PermissionError, match="private"):
        JobStore(shared / "jobs.sqlite3")

    assert stat.S_IMODE(shared.stat().st_mode) == 0o755


def test_windows_private_directory_can_be_reopened_only_with_chainwake_marker(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config.sys, "platform", "win32")
    managed = tmp_path / "managed"

    assert config.ensure_private_directory(managed) == managed
    assert config.ensure_private_directory(managed) == managed

    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(PermissionError, match="existing"):
        config.ensure_private_directory(existing)


def test_reused_worker_pid_is_requeued_when_identity_changed(tmp_path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    job = store.create(_create())
    assert store.claim(job.id, worker_pid=321, worker_identity="old-worker")

    recovered = store.requeue_orphans(process_identity=lambda _pid: "new-worker")

    assert recovered == [job.id]
