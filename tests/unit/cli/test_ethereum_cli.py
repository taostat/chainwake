"""Unit contract for the ``chainwake eth network base-fee`` surface."""

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
from chainwake.output.schema import DeltaCondition, ThresholdCondition, TxCondition

TX_HASH = f"0x{'ab' * 32}"

pytestmark = pytest.mark.unit


class _RunCapture:
    """Capture the fully resolved watcher immediately before provider setup."""

    def __init__(self) -> None:
        self.spec: WatcherSpec | None = None
        self.rpc_url: str | None = None
        self.api_key: str | None = None

    async def stub(self, spec: WatcherSpec, /, **kwargs: object) -> int:
        self.spec = spec
        self.rpc_url = str(kwargs["rpc_url"])
        raw_api_key = kwargs.get("api_key")
        self.api_key = str(raw_api_key) if raw_api_key is not None else None
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


def test_root_registers_ethereum_short_and_long_aliases() -> None:
    app = build_app()

    assert "eth" in app._commands
    assert "ethereum" in app._commands
    assert app._commands["ethereum"] is app._commands["eth"]


def test_base_fee_threshold_builds_ethereum_watcher(capture_run: _RunCapture) -> None:
    code, payload = _invoke("eth", "network", "base-fee", "--below", "10")

    assert code == 0, payload
    assert capture_run.spec is not None
    assert capture_run.spec.chain == "eth"
    assert capture_run.spec.resource == "network"
    assert capture_run.spec.path_params == {}
    assert capture_run.spec.sub_resource == "base-fee"
    assert capture_run.spec.primitive_name == "threshold"
    condition = capture_run.spec.condition
    assert isinstance(condition, ThresholdCondition)
    assert condition.operator == "below"
    assert condition.target == 10.0
    assert capture_run.spec.poll_seconds is None


def test_base_fee_move_defaults_to_since_watcher_start(capture_run: _RunCapture) -> None:
    code, payload = _invoke("ethereum", "network", "base-fee", "--move-pct", "5")

    assert code == 0, payload
    assert capture_run.spec is not None
    assert capture_run.spec.chain == "eth"
    assert capture_run.spec.primitive_name == "delta"
    condition = capture_run.spec.condition
    assert isinstance(condition, DeltaCondition)
    assert condition.operator == "move-pct"
    assert condition.target == 5.0
    assert condition.window.unit == "ever"
    assert condition.window.value == "watcher-start"


def test_base_fee_resolves_ethereum_rpc_and_api_key_env(
    capture_run: _RunCapture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHAINWAKE_ETH_RPC_URL", "wss://ethereum.example/ws")
    monkeypatch.setenv("CHAINWAKE_ETH_API_KEY", "ethereum-secret")
    monkeypatch.setenv("CHAINWAKE_API_KEY", "generic-secret")

    code, payload = _invoke("eth", "network", "base-fee", "--above", "20")

    assert code == 0, payload
    assert capture_run.rpc_url == "wss://ethereum.example/ws"
    assert capture_run.api_key == "ethereum-secret"


def test_base_fee_explicit_rpc_and_api_key_override_environment(
    capture_run: _RunCapture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHAINWAKE_ETH_RPC_URL", "wss://environment.example/ws")
    monkeypatch.setenv("CHAINWAKE_ETH_API_KEY", "environment-secret")

    code, payload = _invoke(
        "eth",
        "network",
        "base-fee",
        "--below",
        "10",
        "--rpc-url",
        "ws://127.0.0.1:8545",
        "--api-key",
        "explicit-secret",
    )

    assert code == 0, payload
    assert capture_run.rpc_url == "ws://127.0.0.1:8545"
    assert capture_run.api_key == "explicit-secret"


def test_base_fee_falls_back_to_generic_api_key(
    capture_run: _RunCapture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CHAINWAKE_ETH_API_KEY", raising=False)
    monkeypatch.setenv("CHAINWAKE_API_KEY", "generic-secret")

    code, payload = _invoke("eth", "network", "base-fee", "--below", "10")

    assert code == 0, payload
    assert capture_run.api_key == "generic-secret"


def test_base_fee_requires_one_condition() -> None:
    code, payload = _invoke("eth", "network", "base-fee")

    assert code == 2
    assert payload is not None
    assert payload["status"] == "user_error"
    assert "one of --below, --above, --drop-pct, --rise-pct, --move-pct" in payload["message"]


def test_base_fee_rejects_mixed_threshold_and_delta_conditions() -> None:
    code, payload = _invoke(
        "eth",
        "network",
        "base-fee",
        "--below",
        "10",
        "--move-pct",
        "5",
    )

    assert code == 2
    assert payload is not None
    assert payload["status"] == "user_error"
    assert "mutually exclusive" in payload["message"]


def test_transaction_defaults_to_one_confirmation(capture_run: _RunCapture) -> None:
    code, payload = _invoke("eth", "tx", TX_HASH)

    assert code == 0, payload
    assert capture_run.spec is not None
    assert capture_run.spec.chain == "eth"
    assert capture_run.spec.resource == "tx"
    assert capture_run.spec.path_params == {"tx_hash": TX_HASH}
    assert capture_run.spec.primitive_name == "tx"
    assert capture_run.spec.read_args == {
        "finality": "included",
        "confirmations": 1,
    }
    condition = capture_run.spec.condition
    assert isinstance(condition, TxCondition)
    assert condition.finality == "included"
    assert condition.confirmations == 1


def test_transaction_accepts_finalized_target(capture_run: _RunCapture) -> None:
    code, payload = _invoke("ethereum", "tx", TX_HASH, "--finality", "finalized")

    assert code == 0, payload
    assert capture_run.spec is not None
    condition = capture_run.spec.condition
    assert isinstance(condition, TxCondition)
    assert condition.finality == "finalized"
    assert condition.confirmations is None


def test_transaction_rejects_confirmations_with_finalized() -> None:
    code, payload = _invoke(
        "eth",
        "tx",
        TX_HASH,
        "--finality",
        "finalized",
        "--confirmations",
        "2",
    )

    assert code == 2
    assert payload is not None
    assert payload["status"] == "user_error"
    assert "--confirmations" in payload["message"]


def test_transaction_rejects_zero_confirmations() -> None:
    code, payload = _invoke("eth", "tx", TX_HASH, "--confirmations", "0")

    assert code == 2
    assert payload is not None
    assert payload["status"] == "user_error"
    assert "greater than zero" in payload["message"]
