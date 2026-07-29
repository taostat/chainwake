"""CLI contracts for profile-driven EVM command trees."""

from __future__ import annotations

import contextlib
import json
from io import StringIO
from typing import Any
from unittest.mock import patch

import cyclopts
import pytest

from chainwake.cli.app import build_app
from chainwake.cli.chains import dispatch
from chainwake.core.runtime import WatcherSpec
from chainwake.output.schema import ThresholdCondition, TxCondition

TX_HASH = f"0x{'ab' * 32}"

pytestmark = pytest.mark.unit


class _RunCapture:
    def __init__(self) -> None:
        self.spec: WatcherSpec | None = None
        self.rpc_url: str | None = None

    async def stub(self, spec: WatcherSpec, /, **kwargs: object) -> int:
        self.spec = spec
        self.rpc_url = str(kwargs["rpc_url"])
        return 0


@pytest.fixture
def capture_run(monkeypatch: pytest.MonkeyPatch) -> _RunCapture:
    capture = _RunCapture()
    monkeypatch.setattr(dispatch, "_run_with_error_handling", capture.stub)
    monkeypatch.setenv("CHAINWAKE_RENDER_MODE", "json")
    return capture


def _invoke(*args: str) -> tuple[int, dict[str, Any] | None]:
    stdout = StringIO()
    app = build_app()
    try:
        with patch("sys.stdout", stdout):
            app(list(args), exit_on_error=False)
        code = 0
    except cyclopts.CycloptsError:
        code = 2
    except SystemExit as exc:
        code = int(exc.code) if exc.code is not None else 0
    payload: dict[str, Any] | None = None
    with contextlib.suppress(json.JSONDecodeError):
        payload = json.loads(stdout.getvalue())
    return code, payload


def test_root_registers_base_and_bsc_commands() -> None:
    app = build_app()

    assert "base" in app._commands
    assert "bsc" in app._commands


@pytest.mark.parametrize(
    ("chain", "command", "default_rpc"),
    [
        ("base", "base-fee", "wss://base-rpc.publicnode.com"),
        ("base", "l1-base-fee", "wss://base-rpc.publicnode.com"),
        ("base", "l1-blob-base-fee", "wss://base-rpc.publicnode.com"),
        ("bsc", "gas-price", "wss://bsc-rpc.publicnode.com"),
    ],
)
def test_chain_appropriate_fee_commands_build_numeric_watchers(
    capture_run: _RunCapture,
    chain: str,
    command: str,
    default_rpc: str,
) -> None:
    code, payload = _invoke(chain, "network", command, "--above", "1")

    assert code == 0, payload
    assert capture_run.spec is not None
    assert capture_run.spec.chain == chain
    assert capture_run.spec.sub_resource == command
    assert capture_run.spec.primitive_name == "threshold"
    assert isinstance(capture_run.spec.condition, ThresholdCondition)
    assert capture_run.rpc_url == default_rpc


@pytest.mark.parametrize("chain", ["eth", "base", "bsc"])
def test_token_price_command_builds_chain_scoped_usd_watcher(
    capture_run: _RunCapture,
    chain: str,
) -> None:
    code, payload = _invoke(chain, "token", "DAI", "price", "--below", "0.995")

    assert code == 0, payload
    assert capture_run.spec is not None
    assert capture_run.spec.chain == chain
    assert capture_run.spec.resource == "token"
    assert capture_run.spec.path_params == {"token": "DAI"}
    assert capture_run.spec.sub_resource == "price"
    assert capture_run.spec.primitive_name == "threshold"
    assert capture_run.spec.poll_seconds is None


def test_token_price_does_not_advertise_chain_block_windows() -> None:
    code, payload = _invoke(
        "bsc",
        "token",
        "GRAM",
        "price",
        "--move-pct",
        "5",
        "--window-blocks",
        "100",
    )

    assert code == 2
    assert payload is None


def test_base_transaction_accepts_safe_finality(capture_run: _RunCapture) -> None:
    code, payload = _invoke("base", "tx", TX_HASH, "--finality", "safe")

    assert code == 0, payload
    assert capture_run.spec is not None
    assert capture_run.spec.chain == "base"
    condition = capture_run.spec.condition
    assert isinstance(condition, TxCondition)
    assert condition.finality == "safe"
    assert condition.confirmations is None


def test_bsc_transaction_accepts_confirmation_depth(capture_run: _RunCapture) -> None:
    code, payload = _invoke("bsc", "tx", TX_HASH, "--confirmations", "12")

    assert code == 0, payload
    assert capture_run.spec is not None
    assert capture_run.spec.chain == "bsc"
    condition = capture_run.spec.condition
    assert isinstance(condition, TxCondition)
    assert condition.finality == "included"
    assert condition.confirmations == 12


def test_bsc_transaction_accepts_finalized(capture_run: _RunCapture) -> None:
    code, payload = _invoke("bsc", "tx", TX_HASH, "--finality", "finalized")

    assert code == 0, payload
    assert capture_run.spec is not None
    condition = capture_run.spec.condition
    assert isinstance(condition, TxCondition)
    assert condition.finality == "finalized"


def test_bsc_rejects_unsupported_safe_finality() -> None:
    code, payload = _invoke("bsc", "tx", TX_HASH, "--finality", "safe")

    assert code == 2
    assert payload is not None
    assert payload["status"] == "user_error"
    assert "BSC supports finality levels: included, finalized" in payload["message"]


def test_base_rejects_confirmations_with_safe_finality() -> None:
    code, payload = _invoke(
        "base",
        "tx",
        TX_HASH,
        "--finality",
        "safe",
        "--confirmations",
        "2",
    )

    assert code == 2
    assert payload is not None
    assert "--confirmations cannot be combined with --finality safe" in payload["message"]
