"""Unit tests for the cross-cutting ``--max-ru`` estimate guard.

Spec §16.1 requires every primitive to accept a registry-estimated observation
budget. The runtime applies it through ``Budget``; the CLI surface plumbs
the user-supplied value into ``WatcherSpec.max_ru``. These tests verify:

- Each ``dispatch_*`` helper accepts ``max_ru=`` and threads it onto the
  ``WatcherSpec`` (the runtime contract surface).
- The cyclopts ``--max-ru`` flag and the ``CHAINWAKE_BT_MAX_RU`` env var
  both flow end-to-end into ``WatcherSpec.max_ru`` for a representative
  command from each primitive shape.
"""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest

from chainwake.cli.app import build_app
from chainwake.cli.chains import dispatch
from chainwake.cli.chains.bittensor import _MAX_RU_PARAM
from chainwake.core.runtime import WatcherSpec
from tests.ss58 import ALICE_SS58

pytestmark = pytest.mark.unit

app = build_app()
_TX_HASH = "0x" + "ab" * 32


def test_max_ru_help_describes_actual_status_and_declared_read_costs() -> None:
    help_text = str(_MAX_RU_PARAM.help)

    assert "budget_exhausted" in help_text
    assert "declared observation costs" in help_text
    assert "not a provider billing cap" in help_text
    assert "budget_reached" not in help_text
    assert "One read = 1 RU" not in help_text


# ---------------------------------------------------------------------------
# Spec-capturing fixture — intercepts the watcher before any RPC connect
# ---------------------------------------------------------------------------


class _SpecCapture:
    """Captures the WatcherSpec built by a dispatch helper."""

    def __init__(self) -> None:
        self.spec: WatcherSpec | None = None

    async def stub(self, spec: WatcherSpec, /, **_kwargs: object) -> int:
        self.spec = spec
        return 0


@pytest.fixture
def capture_spec(monkeypatch: pytest.MonkeyPatch) -> _SpecCapture:
    """Replace ``_run_with_error_handling`` with a spec-capturing stub.

    Avoids opening any RPC connection; we only need to confirm what the
    dispatch helper builds before handing off to the runtime.
    """
    capture = _SpecCapture()
    monkeypatch.setattr(dispatch, "_run_with_error_handling", capture.stub)
    return capture


def _invoke_cli(*args: str) -> int:
    """Invoke the cyclopts app, return the exit code."""
    stdout_capture = StringIO()
    try:
        with patch("sys.stdout", stdout_capture):
            app(list(args), exit_on_error=False)
        return 0
    except SystemExit as exc:
        return int(exc.code) if exc.code is not None else 0


# ---------------------------------------------------------------------------
# dispatch_* helpers — direct kwarg threading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_threshold_threads_max_ru(capture_spec: _SpecCapture) -> None:
    await dispatch.dispatch_threshold(
        resource="subnet",
        path_params={"netuid": "1"},
        sub_resource="pool.price",
        entry_path="subnet.{netuid}.pool.price",
        operator="below",
        target=0.5,
        rpc_url="ws://x",
        max_runtime_seconds=1.0,
        poll_seconds=1.0,
        invocation=["chainwake"],
        out_uris=[],
        max_ru=1234,
    )
    assert capture_spec.spec is not None
    assert capture_spec.spec.max_ru == 1234


@pytest.mark.asyncio
async def test_dispatch_delta_threads_max_ru(capture_spec: _SpecCapture) -> None:
    await dispatch.dispatch_delta(
        resource="subnet",
        path_params={"netuid": "1"},
        sub_resource="pool.price",
        entry_path="subnet.{netuid}.pool.price",
        operator="drop-pct",
        target=5.0,
        window_unit="time",
        window_value="1h",
        rpc_url="ws://x",
        max_runtime_seconds=1.0,
        poll_seconds=1.0,
        invocation=["chainwake"],
        out_uris=[],
        max_ru=2222,
    )
    assert capture_spec.spec is not None
    assert capture_spec.spec.max_ru == 2222


@pytest.mark.asyncio
async def test_dispatch_state_threads_max_ru(capture_spec: _SpecCapture) -> None:
    await dispatch.dispatch_state(
        resource="validator",
        path_params={"hotkey": ALICE_SS58},
        sub_resource="commission",
        entry_path="validator.{hotkey}.commission",
        operator="on-change",
        target=None,
        rpc_url="ws://x",
        max_runtime_seconds=1.0,
        poll_seconds=1.0,
        invocation=["chainwake"],
        out_uris=[],
        max_ru=333,
    )
    assert capture_spec.spec is not None
    assert capture_spec.spec.max_ru == 333


@pytest.mark.asyncio
async def test_dispatch_liveness_threads_max_ru(capture_spec: _SpecCapture) -> None:
    await dispatch.dispatch_liveness(
        resource="validator",
        path_params={"hotkey": ALICE_SS58},
        sub_resource="weights",
        entry_path="validator.{hotkey}.weights",
        silent_for="3epochs",
        rpc_url="ws://x",
        max_runtime_seconds=1.0,
        poll_seconds=1.0,
        invocation=["chainwake"],
        out_uris=[],
        max_ru=444,
    )
    assert capture_spec.spec is not None
    assert capture_spec.spec.max_ru == 444


@pytest.mark.asyncio
async def test_dispatch_event_threads_max_ru(capture_spec: _SpecCapture) -> None:
    await dispatch.dispatch_event(
        event_type="transfer",
        args_match=None,
        entry_path="event.transfer",
        rpc_url="ws://x",
        max_runtime_seconds=1.0,
        invocation=["chainwake"],
        out_uris=[],
        max_ru=555,
    )
    assert capture_spec.spec is not None
    assert capture_spec.spec.max_ru == 555


@pytest.mark.asyncio
async def test_dispatch_tx_threads_max_ru(capture_spec: _SpecCapture) -> None:
    await dispatch.dispatch_tx(
        tx_hash=_TX_HASH,
        finality="finalized",
        entry_path="tx.{tx_hash}",
        rpc_url="ws://x",
        max_runtime_seconds=1.0,
        invocation=["chainwake"],
        out_uris=[],
        max_ru=666,
    )
    assert capture_spec.spec is not None
    assert capture_spec.spec.max_ru == 666


@pytest.mark.asyncio
async def test_dispatch_threshold_max_ru_defaults_to_none(
    capture_spec: _SpecCapture,
) -> None:
    """Omitting ``--max-ru`` must leave ``WatcherSpec.max_ru`` unset (None)."""
    await dispatch.dispatch_threshold(
        resource="subnet",
        path_params={"netuid": "1"},
        sub_resource="pool.price",
        entry_path="subnet.{netuid}.pool.price",
        operator="below",
        target=0.5,
        rpc_url="ws://x",
        max_runtime_seconds=1.0,
        poll_seconds=1.0,
        invocation=["chainwake"],
        out_uris=[],
    )
    assert capture_spec.spec is not None
    assert capture_spec.spec.max_ru is None


# ---------------------------------------------------------------------------
# End-to-end CLI: --max-ru flag flows into the watcher spec
# ---------------------------------------------------------------------------


def test_cli_subnet_price_below_max_ru_flows_to_spec(
    capture_spec: _SpecCapture,
) -> None:
    code = _invoke_cli("bt", "subnet", "1", "price", "--below", "0.5", "--max-ru", "1000")
    assert code == 0
    assert capture_spec.spec is not None
    assert capture_spec.spec.max_ru == 1000


def test_cli_validator_commission_max_ru_flows_to_spec(
    capture_spec: _SpecCapture,
) -> None:
    code = _invoke_cli(
        "bt", "validator", ALICE_SS58, "commission", "--on-change", "--max-ru", "777"
    )
    assert code == 0
    assert capture_spec.spec is not None
    assert capture_spec.spec.max_ru == 777


def test_cli_event_transfer_max_ru_flows_to_spec(
    capture_spec: _SpecCapture,
) -> None:
    code = _invoke_cli(
        "bt", "event", "--type", "transfer", "--max-ru", "888", "--max-runtime", "1s"
    )
    assert code == 0
    assert capture_spec.spec is not None
    assert capture_spec.spec.max_ru == 888


def test_cli_tx_max_ru_flows_to_spec(capture_spec: _SpecCapture) -> None:
    code = _invoke_cli("bt", "tx", _TX_HASH, "--finality", "finalized", "--max-ru", "999")
    assert code == 0
    assert capture_spec.spec is not None
    assert capture_spec.spec.max_ru == 999


def test_cli_validator_weights_max_ru_flows_to_spec(
    capture_spec: _SpecCapture,
) -> None:
    code = _invoke_cli(
        "bt",
        "validator",
        ALICE_SS58,
        "weights",
        "--silent-for",
        "3epochs",
        "--max-ru",
        "111",
    )
    assert code == 0
    assert capture_spec.spec is not None
    assert capture_spec.spec.max_ru == 111


def test_cli_subnet_price_omitting_max_ru_leaves_spec_unset(
    capture_spec: _SpecCapture,
) -> None:
    """No ``--max-ru`` and no env var → ``WatcherSpec.max_ru`` is None."""
    code = _invoke_cli("bt", "subnet", "1", "price", "--below", "0.5")
    assert code == 0
    assert capture_spec.spec is not None
    assert capture_spec.spec.max_ru is None


# ---------------------------------------------------------------------------
# Env var: CHAINWAKE_BT_MAX_RU resolves through cyclopts
# ---------------------------------------------------------------------------


def test_cli_max_ru_env_var_resolved_via_cyclopts(
    capture_spec: _SpecCapture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHAINWAKE_BT_MAX_RU", "5000")
    code = _invoke_cli("bt", "subnet", "1", "price", "--below", "0.5")
    assert code == 0
    assert capture_spec.spec is not None
    assert capture_spec.spec.max_ru == 5000


def test_cli_explicit_max_ru_overrides_env_var(
    capture_spec: _SpecCapture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit --max-ru wins over CHAINWAKE_BT_MAX_RU (cyclopts precedence)."""
    monkeypatch.setenv("CHAINWAKE_BT_MAX_RU", "5000")
    code = _invoke_cli("bt", "subnet", "1", "price", "--below", "0.5", "--max-ru", "42")
    assert code == 0
    assert capture_spec.spec is not None
    assert capture_spec.spec.max_ru == 42


async def test_generic_dispatch_threads_explicit_chain(capture_spec: _SpecCapture) -> None:
    await dispatch.dispatch_threshold(
        chain="eth",
        resource="network",
        path_params={},
        sub_resource="base-fee",
        entry_path="network.base-fee",
        operator="above",
        target=1.0,
        rpc_url="ws://example",
        max_runtime_seconds=1.0,
        poll_seconds=None,
        invocation=["chainwake", "eth", "network", "base-fee"],
        out_uris=[],
    )

    assert capture_spec.spec is not None
    assert capture_spec.spec.chain == "eth"
