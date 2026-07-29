"""Durable submission seam shared by every watcher dispatch function."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

from chainwake.core.runtime import WatcherSpec
from chainwake.jobs.config import (
    DURABLE_ARGV_ENV_VAR,
    DURABLE_CONTEXT_ENV_VAR,
    DURABLE_ENV_VAR,
    NO_AUTOSTART_ENV_VAR,
    job_database_path,
)
from chainwake.jobs.daemon import ensure_daemon
from chainwake.jobs.models import JobCreate, JobRecord
from chainwake.jobs.render import emit_job_value
from chainwake.jobs.store import JobStore

_API_KEY_FLAG = "--api-key"
_RPC_URL_FLAG = "--rpc-url"


def durable_requested() -> bool:
    return os.environ.get(DURABLE_ENV_VAR) == "1"


def _stored_argv() -> list[str]:
    raw = os.environ.get(DURABLE_ARGV_ENV_VAR)
    if not raw:
        raise ValueError("durable invocation was not captured by the root CLI")
    value = json.loads(raw)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ValueError("durable invocation must be a non-empty string list")
    return value


def _contains_flag(argv: list[str], flag: str) -> bool:
    return any(argument == flag or argument.startswith(f"{flag}=") for argument in argv)


def submit_durable_job(
    spec: WatcherSpec,
    *,
    out_uris: list[str],
) -> JobRecord:
    """Persist a validated watcher before any provider connection is opened."""
    if out_uris:
        raise ValueError("--durable cannot be combined with --out; use jobs wait for delivery")
    argv = _stored_argv()
    chain_env = spec.chain.upper()
    if _contains_flag(argv, _API_KEY_FLAG):
        raise ValueError(
            f"--api-key is not persisted in durable jobs; configure "
            f"CHAINWAKE_{chain_env}_API_KEY instead"
        )
    if _contains_flag(argv, _RPC_URL_FLAG):
        raise ValueError(
            f"--rpc-url is not persisted in durable jobs; configure "
            f"CHAINWAKE_{chain_env}_RPC_URL instead"
        )
    deadline = (
        datetime.now(UTC) + timedelta(seconds=spec.max_runtime_seconds)
        if spec.max_runtime_seconds is not None
        else None
    )
    request = JobCreate(
        argv=argv,
        context=os.environ.get(DURABLE_CONTEXT_ENV_VAR) or None,
        deadline_at=deadline,
    )
    store = JobStore(job_database_path())
    record = store.create(request)
    if not ensure_daemon() and os.environ.get(NO_AUTOSTART_ENV_VAR) != "1":
        store.fail(record.id, error="durable-job supervisor failed to start")
        failed = store.get(record.id)
        if failed is None:
            raise RuntimeError("failed durable job could not be reloaded")
        record = failed
    emit_job_value(
        record,
        human=f"durable job {record.id} created ({record.state.value})",
    )
    return record


__all__ = ["durable_requested", "submit_durable_job"]
