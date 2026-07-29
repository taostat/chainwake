"""CLI contracts for durable submission and job management."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from chainwake.cli import jobs
from chainwake.cli.app import (
    DURABLE_ARGV_ENV_VAR,
    DURABLE_CONTEXT_ENV_VAR,
    DURABLE_ENV_VAR,
    _apply_execution_flags,
)
from chainwake.cli.jobs import build_jobs_app
from chainwake.jobs.models import JobState

pytestmark = pytest.mark.unit


def test_durable_root_flags_capture_inner_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (DURABLE_ENV_VAR, DURABLE_CONTEXT_ENV_VAR, DURABLE_ARGV_ENV_VAR):
        monkeypatch.delenv(name, raising=False)

    _apply_execution_flags(
        durable=True,
        context="Wake me with the observed price.",
        tokens=("bt", "subnet", "19", "price", "--below", "0.05"),
    )

    assert os.environ[DURABLE_ENV_VAR] == "1"
    assert os.environ[DURABLE_CONTEXT_ENV_VAR] == "Wake me with the observed price."
    assert json.loads(os.environ[DURABLE_ARGV_ENV_VAR]) == [
        "bt",
        "subnet",
        "19",
        "price",
        "--below",
        "0.05",
    ]


def test_context_without_durable_is_rejected() -> None:
    with pytest.raises(ValueError, match="requires --durable"):
        _apply_execution_flags(
            durable=False,
            context="orphan context",
            tokens=("bt", "subnet", "19", "price", "--below", "0.05"),
        )


def test_durable_price_submission_does_not_connect_to_rpc(tmp_path) -> None:
    env = os.environ.copy()
    env["CHAINWAKE_HOME"] = str(tmp_path)
    env["CHAINWAKE_NO_AUTOSTART"] = "1"
    env["CHAINWAKE_BT_RPC_URL"] = "ws://127.0.0.1:1"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "chainwake",
            "--json",
            "--durable",
            "--context",
            "Tell me the price and block.",
            "bt",
            "subnet",
            "19",
            "price",
            "--below",
            "0.05",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["state"] == "pending"
    assert payload["context"] == "Tell me the price and block."


def test_durable_ethereum_base_fee_submission_does_not_connect_to_rpc(tmp_path) -> None:
    env = os.environ.copy()
    env["CHAINWAKE_HOME"] = str(tmp_path)
    env["CHAINWAKE_NO_AUTOSTART"] = "1"
    env["CHAINWAKE_ETH_RPC_URL"] = "ws://127.0.0.1:1"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "chainwake",
            "--json",
            "--durable",
            "--context",
            "Tell me the base fee and block.",
            "eth",
            "network",
            "base-fee",
            "--below",
            "10",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["state"] == "pending"
    assert payload["argv"][:3] == ["eth", "network", "base-fee"]
    assert payload["context"] == "Tell me the base fee and block."


def test_durable_ethereum_transaction_submission_does_not_connect_to_rpc(tmp_path) -> None:
    tx_hash = f"0x{'ab' * 32}"
    env = os.environ.copy()
    env["CHAINWAKE_HOME"] = str(tmp_path)
    env["CHAINWAKE_NO_AUTOSTART"] = "1"
    env["CHAINWAKE_ETH_RPC_URL"] = "ws://127.0.0.1:1"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "chainwake",
            "--json",
            "--durable",
            "--context",
            "Tell me whether the transaction succeeded.",
            "eth",
            "tx",
            tx_hash,
            "--confirmations",
            "2",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["state"] == "pending"
    assert payload["argv"] == [
        "eth",
        "tx",
        tx_hash,
        "--confirmations",
        "2",
    ]
    assert payload["context"] == "Tell me whether the transaction succeeded."


def test_jobs_show_returns_persisted_submission(tmp_path) -> None:
    env = os.environ.copy()
    env["CHAINWAKE_HOME"] = str(tmp_path)
    env["CHAINWAKE_NO_AUTOSTART"] = "1"
    created = subprocess.run(
        [
            sys.executable,
            "-m",
            "chainwake",
            "--json",
            "--durable",
            "bt",
            "subnet",
            "19",
            "price",
            "--below",
            "0.05",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    job_id = json.loads(created.stdout)["id"]

    shown = subprocess.run(
        [sys.executable, "-m", "chainwake", "--json", "jobs", "show", job_id],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert shown.returncode == 0, shown.stderr
    payload = json.loads(shown.stdout)
    assert payload["id"] == job_id
    assert payload["argv"][:4] == ["bt", "subnet", "19", "price"]


@pytest.mark.parametrize("state", [JobState.PENDING, JobState.RUNNING])
def test_jobs_wait_fails_when_nonterminal_job_has_no_supervisor(
    monkeypatch: pytest.MonkeyPatch,
    state: JobState,
) -> None:
    monkeypatch.setattr(jobs, "ensure_daemon", lambda: False)
    monkeypatch.setattr(
        jobs,
        "_require_job",
        lambda _job_id: SimpleNamespace(state=state),
    )

    with pytest.raises(SystemExit) as exc_info:
        build_jobs_app()(["wait", "cw_test"], exit_on_error=False)

    assert exc_info.value.code == 2
