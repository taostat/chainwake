"""Contracts for durable worker invocation and terminal-state mapping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from chainwake.jobs.config import DURABLE_ARGV_ENV_VAR, DURABLE_CONTEXT_ENV_VAR, DURABLE_ENV_VAR
from chainwake.jobs.models import JobState
from chainwake.jobs.worker import (
    _clean_child_environment,
    argv_with_remaining_runtime,
    state_for_result,
)

pytestmark = pytest.mark.unit


def test_worker_replaces_runtime_with_persisted_remaining_deadline() -> None:
    argv = [
        "bt",
        "subnet",
        "19",
        "price",
        "--below",
        "0.05",
        "--max-runtime",
        "4h",
    ]
    now = datetime.now(UTC)

    adjusted = argv_with_remaining_runtime(
        argv,
        deadline_at=now + timedelta(seconds=90),
        now=now,
    )

    index = adjusted.index("--max-runtime")
    assert float(adjusted[index + 1]) == pytest.approx(90)
    assert adjusted.count("--max-runtime") == 1


def test_worker_adds_runtime_when_deadline_exists_without_original_flag() -> None:
    now = datetime.now(UTC)

    adjusted = argv_with_remaining_runtime(
        ["bt", "subnet", "19", "price", "--below", "0.05"],
        deadline_at=now + timedelta(seconds=30),
        now=now,
    )

    assert adjusted[-2] == "--max-runtime"
    assert float(adjusted[-1]) == pytest.approx(30)


def test_worker_child_environment_is_minimal_but_preserves_launch_and_chainwake_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "chainwake.jobs.worker.os.environ",
        {
            "PATH": "/usr/bin:/bin",
            "SYSTEMROOT": r"C:\\Windows",
            "CHAINWAKE_HOME": "state/chainwake",
            "CHAINWAKE_RPC_URL": "https://rpc.example",
            DURABLE_ENV_VAR: "1",
            DURABLE_CONTEXT_ENV_VAR: "sensitive context",
            DURABLE_ARGV_ENV_VAR: '["bt", "subnet"]',
            "OPENAI_API_KEY": "should-not-reach-the-watcher",
            "AWS_SECRET_ACCESS_KEY": "should-not-reach-the-watcher",
            "UNRELATED_SETTING": "should-not-reach-the-watcher",
        },
    )

    environment = _clean_child_environment()

    assert environment["PATH"] == "/usr/bin:/bin"
    assert environment["SYSTEMROOT"] == r"C:\\Windows"
    assert environment["CHAINWAKE_HOME"] == "state/chainwake"
    assert environment["CHAINWAKE_RPC_URL"] == "https://rpc.example"
    assert DURABLE_ENV_VAR not in environment
    assert DURABLE_CONTEXT_ENV_VAR not in environment
    assert DURABLE_ARGV_ENV_VAR not in environment
    assert "OPENAI_API_KEY" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "UNRELATED_SETTING" not in environment


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("matched", JobState.MATCHED),
        ("timeout", JobState.EXPIRED),
        ("budget_exhausted", JobState.BUDGET_EXHAUSTED),
        ("stopped", JobState.STOPPED),
        ("provider_error", JobState.FAILED),
        ("auth_error", JobState.FAILED),
        ("user_error", JobState.FAILED),
        ("internal_error", JobState.FAILED),
    ],
)
def test_result_status_maps_to_job_state(status: str, state: JobState) -> None:
    assert state_for_result({"status": status}) == state
