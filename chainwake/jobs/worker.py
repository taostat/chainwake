"""Isolated worker process for one durable watcher."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from chainwake.jobs.config import (
    DATABASE_ENV_VAR,
    DURABLE_ARGV_ENV_VAR,
    DURABLE_CONTEXT_ENV_VAR,
    DURABLE_ENV_VAR,
    job_database_path,
)
from chainwake.jobs.daemon import process_identity
from chainwake.jobs.models import JobState
from chainwake.jobs.store import JobStore

_CANCEL_CHECK_SECONDS = 0.2
_TERMINATE_GRACE_SECONDS = 3.0
_EXPECTED_ARGC = 2
_MAX_RUNTIME_FLAG = "--max-runtime"
_CHILD_OS_ENVIRONMENT_NAMES = frozenset(
    {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TMP",
        "TEMP",
        "TMPDIR",
    }
)


class JobDeadlineExpiredError(RuntimeError):
    """The persisted absolute deadline elapsed before a worker could start."""


def argv_with_remaining_runtime(
    argv: list[str],
    *,
    deadline_at: datetime | None,
    now: datetime | None = None,
) -> list[str]:
    """Replace the original relative runtime with the persisted time remaining."""
    result = list(argv)
    if deadline_at is None:
        return result
    current = now or datetime.now(UTC)
    remaining = (deadline_at - current).total_seconds()
    if remaining <= 0:
        raise JobDeadlineExpiredError("job deadline reached before watcher start")
    rendered = f"{remaining:.6f}".rstrip("0").rstrip(".")
    for index, argument in enumerate(result):
        if argument == _MAX_RUNTIME_FLAG:
            if index + 1 >= len(result):
                raise ValueError(f"stored {_MAX_RUNTIME_FLAG} is missing its value")
            result[index + 1] = rendered
            return result
        if argument.startswith(f"{_MAX_RUNTIME_FLAG}="):
            result[index] = f"{_MAX_RUNTIME_FLAG}={rendered}"
            return result
    return [*result, _MAX_RUNTIME_FLAG, rendered]


def state_for_result(result: dict[str, Any]) -> JobState:
    """Map the watcher output status onto the durable job lifecycle."""
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


def _clean_child_environment() -> dict[str, str]:
    """Return only the launch and Chainwake settings needed by the watcher child."""
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in _CHILD_OS_ENVIRONMENT_NAMES or name.startswith("CHAINWAKE_")
    }
    for name in (DURABLE_ENV_VAR, DURABLE_CONTEXT_ENV_VAR, DURABLE_ARGV_ENV_VAR):
        environment.pop(name, None)
    return environment


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _read_output(handle: BinaryIO) -> str:
    handle.seek(0)
    return handle.read().decode("utf-8", errors="replace").strip()


def run_job(job_id: str, *, database: Path | None = None) -> int:  # noqa: PLR0911
    """Claim and execute one job, recording its terminal watcher payload."""
    store = JobStore(database or job_database_path())
    identity = process_identity(os.getpid())
    if identity is None:
        store.fail(job_id, error="worker process identity could not be established")
        return 4
    if not store.claim(job_id, worker_pid=os.getpid(), worker_identity=identity):
        return 0
    job = store.get(job_id)
    if job is None:
        store.fail(job_id, error="claimed job could not be reloaded")
        return 4
    try:
        argv = argv_with_remaining_runtime(job.argv, deadline_at=job.deadline_at)
    except JobDeadlineExpiredError as exc:
        store.expire(job_id, error=str(exc))
        return 1
    except ValueError as exc:
        store.fail(job_id, error=str(exc))
        return 4

    command = [sys.executable, "-m", "chainwake", "--json", *argv]
    try:
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(
                command,
                stdout=stdout,
                stderr=stderr,
                env=_clean_child_environment(),
            )
            while process.poll() is None:
                current = store.get(job_id)
                if current is None or current.state != JobState.RUNNING:
                    _terminate(process)
                    return 1
                time.sleep(_CANCEL_CHECK_SECONDS)

            raw_stdout = _read_output(stdout)
            raw_stderr = _read_output(stderr)
    except OSError as exc:
        store.fail(job_id, error=f"watcher could not start: {exc}")
        return 4

    try:
        result = json.loads(raw_stdout)
    except json.JSONDecodeError:
        detail = raw_stderr or raw_stdout or "watcher produced no output"
        store.fail(job_id, error=f"watcher produced invalid JSON: {detail}")
        return 4
    if not isinstance(result, dict) or not isinstance(result.get("status"), str):
        store.fail(job_id, error="watcher result is not a status-bearing JSON object")
        return 4
    return_code = int(process.returncode or 0)
    store.complete(job_id, result=result, exit_code=return_code)
    return return_code


def main() -> None:
    if len(sys.argv) != _EXPECTED_ARGC:
        raise SystemExit("usage: python -m chainwake.jobs.worker <job-id>")
    database_raw = os.environ.get(DATABASE_ENV_VAR)
    database = Path(database_raw) if database_raw else None
    raise SystemExit(run_job(sys.argv[1], database=database))


if __name__ == "__main__":
    main()


__all__ = [
    "JobDeadlineExpiredError",
    "argv_with_remaining_runtime",
    "run_job",
    "state_for_result",
]
