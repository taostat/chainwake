"""CLI management surface for durable jobs and their supervisor."""

from __future__ import annotations

import time
from typing import Annotated, Never

import cyclopts

from chainwake.jobs.config import job_database_path
from chainwake.jobs.daemon import (
    daemon_pid,
    daemon_running,
    ensure_daemon,
    run_daemon,
    stop_daemon,
)
from chainwake.jobs.models import TERMINAL_JOB_STATES, JobEvent, JobList, JobRecord
from chainwake.jobs.render import emit_job_value
from chainwake.jobs.store import JobStore
from chainwake.output.render import emit_user_error

_WAIT_INTERVAL_SECONDS = 0.25


def _user_error(message: str) -> Never:
    emit_user_error("invalid_input", message)
    raise SystemExit(2)


def _store() -> JobStore:
    return JobStore(job_database_path())


def _require_job(job_id: str) -> JobRecord:
    job = _store().get(job_id)
    if job is None:
        _user_error(f"unknown durable job {job_id!r}")
    return job


def _emit_record(job: JobRecord) -> None:
    emit_job_value(job, human=f"{job.id} · {job.state.value}")


def build_jobs_app() -> cyclopts.App:
    app = cyclopts.App(name="jobs", help="Manage durable Chainwake jobs.", print_error=False)

    @app.command(name="list")
    def list_jobs() -> None:
        """List durable jobs."""
        jobs = JobList(jobs=_store().list_jobs())
        emit_job_value(jobs, human="\n".join(f"{job.id} · {job.state.value}" for job in jobs.jobs))

    @app.command
    def show(job_id: Annotated[str, cyclopts.Parameter(help="Durable job id.")]) -> None:
        """Show one durable job."""
        _emit_record(_require_job(job_id))

    @app.command
    def wait(job_id: Annotated[str, cyclopts.Parameter(help="Durable job id.")]) -> None:
        """Wait without chain polling until a durable job reaches a terminal state."""
        job = _require_job(job_id)
        if job.state not in TERMINAL_JOB_STATES and not ensure_daemon():
            _user_error("durable-job supervisor is not running; run `chainwake daemon start` first")
        while True:
            job = _require_job(job_id)
            if job.state in TERMINAL_JOB_STATES:
                event = JobEvent(
                    job_id=job.id,
                    state=job.state,
                    context=job.context,
                    result=job.result,
                    error=job.error,
                )
                emit_job_value(
                    event,
                    human=f"{job.id} completed: {job.state.value}",
                )
                raise SystemExit(job.exit_code if job.exit_code is not None else 1)
            time.sleep(_WAIT_INTERVAL_SECONDS)

    @app.command
    def cancel(job_id: Annotated[str, cyclopts.Parameter(help="Durable job id.")]) -> None:
        """Cancel a pending or running durable job."""
        store = _store()
        if store.get(job_id) is None:
            _user_error(f"unknown durable job {job_id!r}")
        if not store.cancel(job_id):
            _user_error(f"durable job {job_id!r} is already terminal")
        _emit_record(_require_job(job_id))

    return app


def build_daemon_app() -> cyclopts.App:
    app = cyclopts.App(name="daemon", help="Control the durable-job supervisor.", print_error=False)

    @app.command
    def status() -> None:
        """Show whether the durable-job supervisor is running."""
        running = daemon_running()
        payload = {"running": running, "pid": daemon_pid()}
        emit_job_value(payload, human=f"daemon {'running' if running else 'stopped'}")
        if not running:
            raise SystemExit(1)

    @app.command
    def start() -> None:
        """Start the durable-job supervisor in the background."""
        running = ensure_daemon()
        payload = {"running": running, "pid": daemon_pid()}
        emit_job_value(payload, human=f"daemon {'running' if running else 'failed to start'}")
        if not running:
            raise SystemExit(1)

    @app.command
    def stop() -> None:
        """Stop the supervisor without cancelling watcher workers."""
        stopped = stop_daemon()
        emit_job_value(
            {"stopping": stopped, "pid": daemon_pid()},
            human="daemon stopping" if stopped else "daemon already stopped",
        )

    @app.command
    def run() -> None:
        """Run the durable-job supervisor in the foreground."""
        raise SystemExit(run_daemon())

    return app


__all__ = ["build_daemon_app", "build_jobs_app"]
