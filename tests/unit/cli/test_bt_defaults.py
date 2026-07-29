"""Defaults asserted on the cyclopts ``chainwake bt …`` CLI surface.

Task #39 retired two foot-guns: the implicit ``--max-runtime 30s`` ceiling
and the hard-coded 1-second poll interval. The new defaults flow through
to ``WatcherSpec`` so the runtime can resolve them.
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest

from chainwake.cli.app import build_app
from chainwake.cli.chains import dispatch
from chainwake.cli.chains.bittensor import _resolve_rpc
from chainwake.core.runtime import WatcherSpec
from tests.ss58 import ALICE_SS58

pytestmark = pytest.mark.unit

app = build_app()


class _SpecCapture:
    """Captures the WatcherSpec built by a dispatch helper, no RPC."""

    def __init__(self) -> None:
        self.spec: WatcherSpec | None = None

    async def stub(self, spec: WatcherSpec, /, **_kwargs: object) -> int:
        self.spec = spec
        return 0


@pytest.fixture
def capture_spec(monkeypatch: pytest.MonkeyPatch) -> _SpecCapture:
    capture = _SpecCapture()
    monkeypatch.setattr(dispatch, "_run_with_error_handling", capture.stub)
    return capture


def _invoke_cli(*args: str) -> int:
    stdout = StringIO()
    try:
        with patch("sys.stdout", stdout):
            app(list(args), exit_on_error=False)
        return 0
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 0


# ---------------------------------------------------------------------------
# Bug 1 — --max-runtime defaults to None (unbounded)
# ---------------------------------------------------------------------------


def test_cli_subnet_price_omitting_max_runtime_is_unbounded(
    capture_spec: _SpecCapture,
) -> None:
    """No ``--max-runtime`` flag → ``WatcherSpec.max_runtime_seconds`` is None."""
    code = _invoke_cli("bt", "subnet", "1", "price", "--below", "0.5")
    assert code == 0
    assert capture_spec.spec is not None
    assert capture_spec.spec.max_runtime_seconds is None


def test_cli_event_omitting_max_runtime_is_unbounded(
    capture_spec: _SpecCapture,
) -> None:
    code = _invoke_cli("bt", "event", "--type", "transfer")
    assert code == 0
    assert capture_spec.spec is not None
    assert capture_spec.spec.max_runtime_seconds is None


def test_cli_tao_price_builds_external_usd_watcher(
    capture_spec: _SpecCapture,
) -> None:
    code = _invoke_cli("bt", "network", "tao-price", "--below", "180")

    assert code == 0
    assert capture_spec.spec is not None
    assert capture_spec.spec.chain == "bt"
    assert capture_spec.spec.resource == "network"
    assert capture_spec.spec.sub_resource == "tao-price"
    assert capture_spec.spec.primitive_name == "threshold"
    assert capture_spec.spec.poll_seconds is None


def test_cli_tx_omitting_max_runtime_is_unbounded(
    capture_spec: _SpecCapture,
) -> None:
    code = _invoke_cli("bt", "tx", "0x" + "ab" * 32, "--finality", "finalized")
    assert code == 0
    assert capture_spec.spec is not None
    assert capture_spec.spec.max_runtime_seconds is None


def test_cli_validator_weights_omitting_max_runtime_is_unbounded(
    capture_spec: _SpecCapture,
) -> None:
    code = _invoke_cli("bt", "validator", ALICE_SS58, "weights", "--silent-for", "3epochs")
    assert code == 0
    assert capture_spec.spec is not None
    assert capture_spec.spec.max_runtime_seconds is None


def test_cli_explicit_max_runtime_still_threads_through(
    capture_spec: _SpecCapture,
) -> None:
    """An explicit ``--max-runtime`` value takes precedence over the new default."""
    code = _invoke_cli("bt", "subnet", "1", "price", "--below", "0.5", "--max-runtime", "10m")
    assert code == 0
    assert capture_spec.spec is not None
    assert capture_spec.spec.max_runtime_seconds == 600.0


# ---------------------------------------------------------------------------
# Registry-owned observation timing
# ---------------------------------------------------------------------------


def test_cli_subnet_price_omitting_poll_seconds_lets_runtime_resolve(
    capture_spec: _SpecCapture,
) -> None:
    """The CLI leaves transport selection to the registry policy."""
    code = _invoke_cli("bt", "subnet", "1", "price", "--below", "0.5")
    assert code == 0
    assert capture_spec.spec is not None
    assert capture_spec.spec.poll_seconds is None


def test_cli_validator_dividends_omitting_poll_seconds_is_none(
    capture_spec: _SpecCapture,
) -> None:
    code = _invoke_cli(
        "bt", "validator", ALICE_SS58, "dividends-alpha", "--netuid", "19", "--below", "0.5"
    )
    assert code == 0
    assert capture_spec.spec is not None
    assert capture_spec.spec.poll_seconds is None
    assert capture_spec.spec.path_params == {"netuid": "19", "hotkey": ALICE_SS58}


def test_cli_validator_stake_requires_and_threads_netuid(
    capture_spec: _SpecCapture,
) -> None:
    code = _invoke_cli(
        "bt",
        "validator",
        ALICE_SS58,
        "stake-alpha",
        "--netuid",
        "19",
        "--below",
        "100",
    )
    assert code == 0
    assert capture_spec.spec is not None
    assert capture_spec.spec.path_params == {"netuid": "19", "hotkey": ALICE_SS58}
    assert capture_spec.spec.sub_resource == "stake-alpha"


def test_cli_leaves_observation_timing_to_registry_policy(
    capture_spec: _SpecCapture,
) -> None:
    code = _invoke_cli("bt", "subnet", "1", "price", "--below", "0.5")
    assert code == 0
    assert capture_spec.spec is not None
    assert capture_spec.spec.poll_seconds is None


def test_rpc_url_environment_is_used_for_every_bittensor_watcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHAINWAKE_BT_RPC_URL", "wss://private.example")

    assert _resolve_rpc(None) == "wss://private.example"
