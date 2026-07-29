"""Pydantic contracts for durable Chainwake jobs."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MAX_CONTEXT_LENGTH = 8_192


class JobState(StrEnum):
    """Persistent lifecycle state for one durable watcher."""

    PENDING = "pending"
    RUNNING = "running"
    MATCHED = "matched"
    EXPIRED = "expired"
    BUDGET_EXHAUSTED = "budget_exhausted"
    STOPPED = "stopped"
    CANCELLED = "cancelled"
    FAILED = "failed"


TERMINAL_JOB_STATES: frozenset[JobState] = frozenset(
    {
        JobState.MATCHED,
        JobState.EXPIRED,
        JobState.BUDGET_EXHAUSTED,
        JobState.STOPPED,
        JobState.CANCELLED,
        JobState.FAILED,
    }
)


class _JobModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class JobCreate(_JobModel):
    """Validated input for a persisted job."""

    argv: list[str] = Field(min_length=1)
    context: str | None = Field(default=None, max_length=MAX_CONTEXT_LENGTH)
    deadline_at: datetime | None = None


class JobRecord(_JobModel):
    """Complete durable job record returned by the CLI."""

    id: str
    state: JobState
    argv: list[str]
    context: str | None
    created_at: datetime
    updated_at: datetime
    deadline_at: datetime | None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    worker_pid: int | None = None
    worker_identity: str | None = None
    exit_code: int | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


class JobList(_JobModel):
    """List response kept separate from the watcher output schema."""

    jobs: list[JobRecord]


class JobEvent(_JobModel):
    """Context delivered when ``jobs wait`` observes a terminal job."""

    event: Literal["chainwake.job.completed"] = "chainwake.job.completed"
    job_id: str
    state: JobState
    context: str | None
    result: dict[str, Any] | None
    error: str | None


__all__ = [
    "MAX_CONTEXT_LENGTH",
    "TERMINAL_JOB_STATES",
    "JobCreate",
    "JobEvent",
    "JobList",
    "JobRecord",
    "JobState",
]
