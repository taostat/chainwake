"""Small process-safe SQLite store for durable Chainwake jobs."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chainwake.jobs.config import ensure_private_directory, ensure_private_file
from chainwake.jobs.models import JobCreate, JobRecord, JobState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    argv_json TEXT NOT NULL,
    context TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deadline_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    worker_pid INTEGER,
    worker_identity TEXT,
    exit_code INTEGER,
    result_json TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS jobs_state_created_idx ON jobs(state, created_at);
"""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _required_datetime(value: str) -> datetime:
    parsed = _datetime(value)
    if parsed is None:
        raise ValueError("stored job timestamp is missing")
    return parsed


def _job_id() -> str:
    return f"cw_{uuid.uuid4().hex}"


def _terminal_state(result: dict[str, Any]) -> JobState:
    status = result.get("status")
    if status == "matched":
        return JobState.MATCHED
    if status == "timeout":
        return JobState.EXPIRED
    if status == "budget_exhausted":
        return JobState.BUDGET_EXHAUSTED
    if status == "stopped":
        return JobState.STOPPED
    return JobState.FAILED


class JobStore:
    """Open short SQLite transactions so workers and clients can share the store."""

    def __init__(self, path: Path) -> None:
        self.path = path
        ensure_private_directory(self.path.parent)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
        self._secure_sqlite_files()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield one committed-or-rolled-back transaction and always close it."""
        connection = sqlite3.connect(self.path, timeout=5.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            self._secure_sqlite_files()
            with connection:
                yield connection
        finally:
            connection.close()

    def _secure_sqlite_files(self) -> None:
        ensure_private_file(self.path)
        ensure_private_file(self.path.with_name(f"{self.path.name}-wal"))
        ensure_private_file(self.path.with_name(f"{self.path.name}-shm"))

    def create(self, request: JobCreate) -> JobRecord:
        """Persist a new pending job."""
        created_at = _now()
        identifier = _job_id()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, state, argv_json, context, created_at, updated_at, deadline_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    JobState.PENDING.value,
                    json.dumps(request.argv, separators=(",", ":"), allow_nan=False),
                    request.context,
                    _iso(created_at),
                    _iso(created_at),
                    _iso(request.deadline_at),
                ),
            )
        record = self.get(identifier)
        if record is None:
            raise RuntimeError("newly persisted job could not be read")
        return record

    def get(self, job_id: str) -> JobRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._record(row) if row is not None else None

    def list_jobs(self, *, states: set[JobState] | None = None) -> list[JobRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM jobs ORDER BY created_at").fetchall()
        if states:
            allowed = {state.value for state in states}
            rows = [row for row in rows if row["state"] in allowed]
        return [self._record(row) for row in rows]

    def claim(self, job_id: str, *, worker_pid: int, worker_identity: str) -> bool:
        """Atomically move one pending job to running."""
        now = _iso(_now())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET state = ?, worker_pid = ?, worker_identity = ?,
                    started_at = COALESCE(started_at, ?), updated_at = ?, error = NULL
                WHERE id = ? AND state = ?
                """,
                (
                    JobState.RUNNING.value,
                    worker_pid,
                    worker_identity,
                    now,
                    now,
                    job_id,
                    JobState.PENDING.value,
                ),
            )
        return cursor.rowcount == 1

    def complete(self, job_id: str, *, result: dict[str, Any], exit_code: int) -> bool:
        """Store a worker result unless cancellation already won the race."""
        now = _iso(_now())
        state = _terminal_state(result)
        encoded = json.dumps(result, separators=(",", ":"), allow_nan=False)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET state = ?, result_json = ?, exit_code = ?, completed_at = ?,
                    updated_at = ?, worker_pid = NULL, worker_identity = NULL
                WHERE id = ? AND state = ?
                """,
                (
                    state.value,
                    encoded,
                    exit_code,
                    now,
                    now,
                    job_id,
                    JobState.RUNNING.value,
                ),
            )
        return cursor.rowcount == 1

    def fail(self, job_id: str, *, error: str, exit_code: int = 4) -> bool:
        now = _iso(_now())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET state = ?, error = ?, exit_code = ?, completed_at = ?,
                    updated_at = ?, worker_pid = NULL, worker_identity = NULL
                WHERE id = ? AND state IN (?, ?)
                """,
                (
                    JobState.FAILED.value,
                    error,
                    exit_code,
                    now,
                    now,
                    job_id,
                    JobState.PENDING.value,
                    JobState.RUNNING.value,
                ),
            )
        return cursor.rowcount == 1

    def expire(self, job_id: str, *, error: str = "job deadline reached") -> bool:
        now = _iso(_now())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET state = ?, error = ?, exit_code = 1, completed_at = ?,
                    updated_at = ?, worker_pid = NULL, worker_identity = NULL
                WHERE id = ? AND state IN (?, ?)
                """,
                (
                    JobState.EXPIRED.value,
                    error,
                    now,
                    now,
                    job_id,
                    JobState.PENDING.value,
                    JobState.RUNNING.value,
                ),
            )
        return cursor.rowcount == 1

    def cancel(self, job_id: str) -> bool:
        now = _iso(_now())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET state = ?, completed_at = ?, updated_at = ?, worker_pid = NULL,
                    worker_identity = NULL
                WHERE id = ? AND state IN (?, ?)
                """,
                (
                    JobState.CANCELLED.value,
                    now,
                    now,
                    job_id,
                    JobState.PENDING.value,
                    JobState.RUNNING.value,
                ),
            )
        return cursor.rowcount == 1

    def requeue_orphans(
        self,
        *,
        process_identity: Callable[[int], str | None],
    ) -> list[str]:
        """Requeue running jobs whose isolated worker no longer exists."""
        recovered: list[str] = []
        for job in self.list_jobs(states={JobState.RUNNING}):
            if (
                job.worker_pid is not None
                and job.worker_identity is not None
                and process_identity(job.worker_pid) == job.worker_identity
            ):
                continue
            now = _iso(_now())
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE jobs
                    SET state = ?, worker_pid = NULL, worker_identity = NULL, updated_at = ?,
                        error = 'worker disappeared; requeued'
                    WHERE id = ? AND state = ?
                    """,
                    (
                        JobState.PENDING.value,
                        now,
                        job.id,
                        JobState.RUNNING.value,
                    ),
                )
            if cursor.rowcount == 1:
                recovered.append(job.id)
        return recovered

    @staticmethod
    def _record(row: sqlite3.Row) -> JobRecord:
        result_raw = row["result_json"]
        result = json.loads(result_raw) if result_raw is not None else None
        return JobRecord(
            id=str(row["id"]),
            state=JobState(str(row["state"])),
            argv=json.loads(str(row["argv_json"])),
            context=row["context"],
            created_at=_required_datetime(str(row["created_at"])),
            updated_at=_required_datetime(str(row["updated_at"])),
            deadline_at=_datetime(row["deadline_at"]),
            started_at=_datetime(row["started_at"]),
            completed_at=_datetime(row["completed_at"]),
            worker_pid=row["worker_pid"],
            worker_identity=row["worker_identity"],
            exit_code=row["exit_code"],
            result=result,
            error=row["error"],
        )


__all__ = ["JobStore"]
