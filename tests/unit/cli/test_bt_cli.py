"""Unit tests for ``chainwake bt`` CLI flag validation.

These tests exercise the parse-time validation layer only: flag mutual
exclusion, required flags, missing arguments, and user-error exit codes.
No provider connection is made — test cases hit SystemExit(2) before any
async dispatch runs.

Strategy: invoke the cyclopts app directly with ``exit_on_error=False``,
catching ``CycloptsError`` for parse-level rejections and ``SystemExit``
for application-level exit codes. Assertions are on exit code and JSON
output (where applicable), not on Typer-internal formatting.
"""

from __future__ import annotations

import contextlib
import json
from io import StringIO
from typing import Any
from unittest.mock import AsyncMock, patch

import cyclopts
import pytest

from chainwake.cli.app import build_app
from tests.ss58 import ALICE_SS58, BOB_SS58

pytestmark = pytest.mark.unit

app = build_app()


def invoke(
    *args: str,
    dispatch_delta_override: AsyncMock | None = None,
    dispatch_event_override: AsyncMock | None = None,
    dispatch_state_override: AsyncMock | None = None,
    dispatch_threshold_override: AsyncMock | None = None,
) -> tuple[int, dict[str, Any] | None]:
    """Invoke the app with the given args.

    Returns (exit_code, parsed_stdout_json_or_None).
    Cyclopts parse errors → exit 2 with JSON payload (via __main__ contract).
    Application-level SystemExit → that code.
    """

    async def stop_before_provider(*_args: object, **_kwargs: object) -> int:
        return 3

    stdout_capture = StringIO()
    try:
        with (
            patch("sys.stdout", stdout_capture),
            patch(
                "chainwake.cli.chains.bittensor.dispatch_delta",
                dispatch_delta_override or stop_before_provider,
            ),
            patch(
                "chainwake.cli.chains.common.dispatch_delta",
                dispatch_delta_override or stop_before_provider,
            ),
            patch(
                "chainwake.cli.chains.bittensor.dispatch_event",
                dispatch_event_override or stop_before_provider,
            ),
            patch("chainwake.cli.chains.bittensor.dispatch_liveness", stop_before_provider),
            patch(
                "chainwake.cli.chains.bittensor.dispatch_state",
                dispatch_state_override or stop_before_provider,
            ),
            patch(
                "chainwake.cli.chains.bittensor.dispatch_threshold",
                dispatch_threshold_override or stop_before_provider,
            ),
            patch(
                "chainwake.cli.chains.common.dispatch_threshold",
                dispatch_threshold_override or stop_before_provider,
            ),
            patch("chainwake.cli.chains.bittensor.dispatch_tx", stop_before_provider),
        ):
            app(list(args), exit_on_error=False)
        return 0, None
    except cyclopts.CycloptsError:
        # Parse error — mimics what __main__ does: exit 2.
        return 2, None
    except SystemExit as exc:
        raw = stdout_capture.getvalue().strip()
        payload: dict[str, Any] | None = None
        if raw:
            with contextlib.suppress(json.JSONDecodeError):
                payload = json.loads(raw)
        return int(exc.code) if exc.code is not None else 0, payload


# ---------------------------------------------------------------------------
# spec Appendix C — canonical id-first shape
# ---------------------------------------------------------------------------


class TestSpecAppendixCParses:
    """Spec §5.1 / Appendix C invocations all parse with the new shape.

    These cover the resource-id-first form for every resource that takes
    an id positional. Each test reaches dispatch (no provider, so exit
    is 3 or 4) — what we assert is that parse-time validation does not
    reject the invocation.
    """

    def test_subnet_19_price_move_pct_window_time(self) -> None:
        code, payload = invoke(
            "bt", "subnet", "19", "price", "--move-pct", "10", "--window-time", "1h"
        )
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")

    def test_subnet_64_registration_cost_below(self) -> None:
        code, payload = invoke("bt", "subnet", "64", "registration-cost", "--below", "0.5")
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")

    def test_validator_weights_silent_for(self) -> None:
        code, payload = invoke("bt", "validator", ALICE_SS58, "weights", "--silent-for", "1epoch")
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")

    def test_validator_commission_on_change(self) -> None:
        code, payload = invoke("bt", "validator", ALICE_SS58, "commission", "--on-change")
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")

    def test_account_balance_on_change(self) -> None:
        code, payload = invoke(
            "bt", "account", ALICE_SS58, "balance", "--on-change", "--max-runtime", "10m"
        )
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")

    def test_neuron_last_update_silent_for(self) -> None:
        code, payload = invoke(
            "bt", "neuron", "19", ALICE_SS58, "last-update", "--silent-for", "10blocks"
        )
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")


@pytest.mark.parametrize(
    "args",
    [
        ("bt", "subnet", "28", "registration-cost", "--above", "1"),
        ("bt", "subnet", "28", "price", "--above", "1"),
        ("bt", "subnet", "28", "burn-rate", "--above", "0.5"),
        ("bt", "validator", ALICE_SS58, "weights", "--silent-for", "1epoch"),
    ],
)
def test_native_monitoring_commands_do_not_accept_poll_seconds(args: tuple[str, ...]) -> None:
    code, _payload = invoke(*args, "--poll-seconds", "0.25")

    assert code == 2


@pytest.mark.parametrize(
    "args",
    [
        ("bt", "validator", "5Fxxx", "commission", "--on-change"),
        ("bt", "neuron", "1", "5Fxxx", "incentive", "--below", "1"),
        ("bt", "account", "5Fxxx", "balance", "--below", "1"),
        ("bt", "event", "--type", "transfer", "--from", "5Fxxx"),
        ("bt", "event", "--type", "transfer", "--to", "5Fxxx"),
        (
            "bt",
            "event",
            "--type",
            "transfer",
            "--direction",
            "in",
            "--address",
            "5Fxxx",
        ),
    ],
)
def test_cli_rejects_malformed_ss58_before_dispatch(args: tuple[str, ...]) -> None:
    code, payload = invoke(*args)

    assert code == 2
    assert payload is not None
    assert payload["status"] == "user_error"
    assert "Bittensor SS58" in payload["message"]


# ---------------------------------------------------------------------------
# subnet price — threshold
# ---------------------------------------------------------------------------


class TestSubnetPriceThreshold:
    def test_bare_leaf_shows_help(self) -> None:
        # Spec §13: bare leaf invocation prints the leaf's help (parameter
        # list) instead of erroring on missing condition flags.
        code, _ = invoke("bt", "subnet", "19", "price")
        assert code == 0

    def test_below_and_above_mutually_exclusive(self) -> None:
        code, payload = invoke("bt", "subnet", "19", "price", "--below", "0.05", "--above", "0.1")
        assert code == 2
        assert payload is not None
        assert payload["status"] == "user_error"
        assert "mutually exclusive" in payload["message"]

    def test_below_accepted(self) -> None:
        # Exits non-2 because below is valid; may exit 3 (provider unreachable).
        # We just confirm parse validation did not reject it.
        _code, _payload = invoke("bt", "subnet", "19", "price", "--below", "0.05")
        assert _payload is None or _payload.get("status") != "user_error"

    def test_delta_without_window_uses_watcher_start_baseline(self) -> None:
        dispatch = AsyncMock(return_value=0)
        code, payload = invoke(
            "bt",
            "subnet",
            "28",
            "burnrate",
            "--move-pct",
            "1",
            dispatch_delta_override=dispatch,
        )
        assert code == 0
        assert payload is None
        dispatch.assert_awaited_once()
        assert dispatch.await_args is not None
        assert dispatch.await_args.kwargs["window_unit"] == "ever"
        assert dispatch.await_args.kwargs["window_value"] == "watcher-start"

    def test_two_window_flags_rejected(self) -> None:
        code, _ = invoke(
            "bt",
            "subnet",
            "19",
            "price",
            "--drop-pct",
            "5",
            "--window-time",
            "1h",
            "--window-blocks",
            "50",
        )
        assert code == 2

    def test_two_delta_flags_rejected(self) -> None:
        code, _ = invoke(
            "bt",
            "subnet",
            "19",
            "price",
            "--drop-pct",
            "5",
            "--rise-pct",
            "5",
            "--window-time",
            "1h",
        )
        assert code == 2

    def test_threshold_with_window_flag_rejected(self) -> None:
        code, payload = invoke(
            "bt",
            "subnet",
            "19",
            "price",
            "--below",
            "0.05",
            "--window-time",
            "1h",
        )
        assert code == 2
        assert payload is not None
        assert payload["status"] == "user_error"

    def test_subcommand_before_id_is_a_standard_parse_error(self) -> None:
        code, payload = invoke("bt", "subnet", "price", "19", "--below", "0.5")
        assert code == 2
        assert payload is None


# ---------------------------------------------------------------------------
# subnet registration-cost
# ---------------------------------------------------------------------------


class TestSubnetRegistrationCost:
    def test_bare_leaf_shows_help(self) -> None:
        code, _ = invoke("bt", "subnet", "19", "registration-cost")
        assert code == 0

    def test_below_and_above_mutually_exclusive(self) -> None:
        code, _ = invoke(
            "bt",
            "subnet",
            "19",
            "registration-cost",
            "--below",
            "500",
            "--above",
            "600",
        )
        assert code == 2

    def test_below_dispatches(self) -> None:
        # Valid flags reach dispatch; may exit 3 (provider unreachable) or 4 (internal error).
        code, payload = invoke("bt", "subnet", "19", "registration-cost", "--below", "500")
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")


# ---------------------------------------------------------------------------
# validator weights (liveness)
# ---------------------------------------------------------------------------


class TestValidatorWeights:
    def test_bare_leaf_shows_help(self) -> None:
        # Bare ``bt validator <hk> weights`` (no flags) prints the leaf's
        # help — same UX the user gets from ``--help`` on the leaf.
        code, _ = invoke("bt", "validator", ALICE_SS58, "weights")
        assert code == 0

    def test_invalid_duration_rejected(self) -> None:
        code, _ = invoke("bt", "validator", ALICE_SS58, "weights", "--silent-for", "notaduration")
        assert code == 2

    def test_valid_duration_dispatches(self) -> None:
        # Valid flags reach dispatch; may exit 3 (provider unreachable) or 4 (internal error).
        code, payload = invoke("bt", "validator", ALICE_SS58, "weights", "--silent-for", "3epochs")
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")

    def test_subcommand_before_id_is_a_standard_parse_error(self) -> None:
        code, payload = invoke("bt", "validator", "weights", ALICE_SS58, "--silent-for", "3epochs")
        assert code == 2
        assert payload is None


# ---------------------------------------------------------------------------
# validator commission (state)
# ---------------------------------------------------------------------------


class TestValidatorCommission:
    def test_bare_leaf_shows_help(self) -> None:
        code, _ = invoke("bt", "validator", ALICE_SS58, "commission")
        assert code == 0

    def test_on_change_dispatches(self) -> None:
        # Valid flags reach dispatch; may exit 3 (provider unreachable) or 4 (internal error).
        code, payload = invoke("bt", "validator", ALICE_SS58, "commission", "--on-change")
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")


# ---------------------------------------------------------------------------
# neuron last-update (liveness)
# ---------------------------------------------------------------------------


class TestNeuronLastUpdate:
    def test_bare_leaf_shows_help(self) -> None:
        code, _ = invoke("bt", "neuron", "19", ALICE_SS58, "last-update")
        assert code == 0

    def test_valid_duration_dispatches(self) -> None:
        # Valid flags reach dispatch; may exit 3 (provider unreachable) or 4 (internal error).
        code, payload = invoke(
            "bt", "neuron", "19", ALICE_SS58, "last-update", "--silent-for", "10blocks"
        )
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")

    def test_subcommand_before_ids_is_a_standard_parse_error(self) -> None:
        code, payload = invoke(
            "bt", "neuron", "last-update", "19", ALICE_SS58, "--silent-for", "10blocks"
        )
        assert code == 2
        assert payload is None


# ---------------------------------------------------------------------------
# tx (tx finality)
# ---------------------------------------------------------------------------


class TestTxFinality:
    def test_requires_tx_hash(self) -> None:
        code, _ = invoke("bt", "tx")
        # No arg → parse error or help
        assert code in (0, 1, 2)

    def test_invalid_finality_rejected(self) -> None:
        code, _ = invoke("bt", "tx", "0xabc", "--finality", "maybeconfirmed")
        assert code == 2

    @pytest.mark.parametrize(
        "tx_hash",
        [
            "0xabc",
            "ab" * 32,
            "0x" + "gg" * 32,
            "0x" + "ab" * 31,
            "0x" + "ab" * 33,
        ],
    )
    def test_invalid_transaction_hash_rejected_before_provider(self, tx_hash: str) -> None:
        code, payload = invoke("bt", "tx", tx_hash, "--finality", "finalized")

        assert code == 2
        assert payload is not None
        assert payload["status"] == "user_error"
        assert "32-byte" in payload["message"]


# ---------------------------------------------------------------------------
# max-runtime duration parsing
# ---------------------------------------------------------------------------


class TestMaxRuntime:
    def test_valid_duration_seconds(self) -> None:
        _code, payload = invoke(
            "bt",
            "subnet",
            "19",
            "price",
            "--below",
            "999999",
            "--max-runtime",
            "1s",
        )
        # Should not be exit 2 for flag reasons; may be 3 (no provider)
        assert payload is None or "invalid_duration" not in str(payload)

    def test_invalid_duration_rejected(self) -> None:
        code, _ = invoke(
            "bt",
            "subnet",
            "19",
            "price",
            "--below",
            "999999",
            "--max-runtime",
            "badvalue",
        )
        assert code == 2


# ---------------------------------------------------------------------------
# resolve_api_key — precedence ladder
# ---------------------------------------------------------------------------


class TestResolveApiKey:
    """Spec §10.2: --api-key > CHAINWAKE_<CHAIN>_API_KEY > CHAINWAKE_API_KEY."""

    def test_explicit_value_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from chainwake.cli.chains.bittensor import resolve_api_key  # noqa: PLC0415

        monkeypatch.setenv("CHAINWAKE_BT_API_KEY", "from-bt-env")
        monkeypatch.setenv("CHAINWAKE_API_KEY", "from-generic-env")
        assert resolve_api_key("explicit", "bt") == "explicit"

    def test_chain_specific_env_var_wins_over_generic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from chainwake.cli.chains.bittensor import resolve_api_key  # noqa: PLC0415

        monkeypatch.setenv("CHAINWAKE_BT_API_KEY", "from-bt-env")
        monkeypatch.setenv("CHAINWAKE_API_KEY", "from-generic-env")
        assert resolve_api_key(None, "bt") == "from-bt-env"

    def test_generic_env_var_used_when_no_chain_specific(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from chainwake.cli.chains.bittensor import resolve_api_key  # noqa: PLC0415

        monkeypatch.delenv("CHAINWAKE_BT_API_KEY", raising=False)
        monkeypatch.setenv("CHAINWAKE_API_KEY", "from-generic-env")
        assert resolve_api_key(None, "bt") == "from-generic-env"

    def test_returns_none_when_nothing_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from chainwake.cli.chains.bittensor import resolve_api_key  # noqa: PLC0415

        monkeypatch.delenv("CHAINWAKE_BT_API_KEY", raising=False)
        monkeypatch.delenv("CHAINWAKE_API_KEY", raising=False)
        assert resolve_api_key(None, "bt") is None

    def test_chain_uppercases_for_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from chainwake.cli.chains.bittensor import resolve_api_key  # noqa: PLC0415

        monkeypatch.delenv("CHAINWAKE_BT_API_KEY", raising=False)
        monkeypatch.delenv("CHAINWAKE_API_KEY", raising=False)
        monkeypatch.setenv("CHAINWAKE_ETH_API_KEY", "from-eth-env")
        assert resolve_api_key(None, "eth") == "from-eth-env"

    def test_empty_string_chain_specific_falls_through_to_generic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spec §10.2: an empty value should not satisfy the env var — fall through."""
        from chainwake.cli.chains.bittensor import resolve_api_key  # noqa: PLC0415

        monkeypatch.setenv("CHAINWAKE_BT_API_KEY", "")
        monkeypatch.setenv("CHAINWAKE_API_KEY", "from-generic-env")
        assert resolve_api_key(None, "bt") == "from-generic-env"


# ---------------------------------------------------------------------------
# subnet pool depth observables (numeric, threshold-or-delta)
# ---------------------------------------------------------------------------


class TestSubnetPoolDepth:
    @pytest.mark.parametrize("cmd", ["tao-depth", "alpha-depth"])
    def test_bare_leaf_shows_help(self, cmd: str) -> None:
        code, _ = invoke("bt", "subnet", "19", cmd)
        assert code == 0

    @pytest.mark.parametrize("cmd", ["tao-depth", "alpha-depth"])
    def test_below_dispatches(self, cmd: str) -> None:
        code, payload = invoke("bt", "subnet", "19", cmd, "--below", "1.0")
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")

    @pytest.mark.parametrize("cmd", ["tao-depth", "alpha-depth"])
    def test_delta_without_window_is_accepted(self, cmd: str) -> None:
        code, payload = invoke("bt", "subnet", "19", cmd, "--drop-pct", "5")
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")


class TestSubnetEmissionShareAndBurnRate:
    @pytest.mark.parametrize("cmd", ["emission-share", "burn-rate"])
    def test_bare_leaf_shows_help(self, cmd: str) -> None:
        code, _ = invoke("bt", "subnet", "19", cmd)
        assert code == 0

    @pytest.mark.parametrize("cmd", ["emission-share", "burn-rate"])
    def test_above_dispatches(self, cmd: str) -> None:
        code, payload = invoke("bt", "subnet", "19", cmd, "--above", "0.1")
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")


class TestSubnetIdentityTargetOperators:
    def test_bare_leaf_shows_help(self) -> None:
        code, _ = invoke("bt", "subnet", "19", "identity")
        assert code == 0

    def test_on_change_dispatches(self) -> None:
        code, payload = invoke("bt", "subnet", "19", "identity", "--on-change")
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")


class TestSubnetHyperparams:
    def test_bare_leaf_shows_help(self) -> None:
        code, _ = invoke("bt", "subnet", "19", "hyperparams")
        assert code == 0

    def test_on_change_dispatches(self) -> None:
        code, payload = invoke("bt", "subnet", "19", "hyperparams", "--on-change")
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")


class TestSubnetDepthForTrade:
    def test_requires_size(self) -> None:
        code, _ = invoke("bt", "subnet", "19", "depth-for-trade", "--max-bps", "50", "--above", "0")
        assert code == 2

    def test_requires_max_bps(self) -> None:
        code, _ = invoke("bt", "subnet", "19", "depth-for-trade", "--size", "100", "--above", "0")
        assert code == 2

    def test_requires_threshold(self) -> None:
        code, _ = invoke(
            "bt", "subnet", "19", "depth-for-trade", "--size", "100", "--max-bps", "50"
        )
        assert code == 2

    @pytest.mark.parametrize("flag", ["--size", "--max-bps"])
    @pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
    def test_computed_inputs_must_be_finite_and_positive(
        self,
        flag: str,
        value: str,
    ) -> None:
        args = [
            "bt",
            "subnet",
            "19",
            "depth-for-trade",
            "--size",
            "100",
            "--max-bps",
            "50",
            "--above",
            "0",
        ]
        args[args.index(flag) + 1] = value

        code, payload = invoke(*args)

        assert code == 2
        assert payload is not None
        assert payload["status"] == "user_error"

    def test_size_max_bps_above_dispatches(self) -> None:
        code, payload = invoke(
            "bt",
            "subnet",
            "19",
            "depth-for-trade",
            "--size",
            "100",
            "--max-bps",
            "50",
            "--above",
            "0",
        )
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")


class TestValidatorIdentity:
    def test_bare_leaf_shows_help(self) -> None:
        code, _ = invoke("bt", "validator", ALICE_SS58, "identity")
        assert code == 0

    def test_on_change_dispatches(self) -> None:
        code, payload = invoke("bt", "validator", ALICE_SS58, "identity", "--on-change")
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")

    @pytest.mark.parametrize("flag", ["--changes-to", "--changes-from"])
    def test_structured_identity_rejects_scalar_target_operators(self, flag: str) -> None:
        code, _ = invoke("bt", "validator", ALICE_SS58, "identity", flag, "alice")
        assert code == 2


class TestSubnetIdentity:
    @pytest.mark.parametrize("flag", ["--changes-to", "--changes-from"])
    def test_structured_identity_rejects_scalar_target_operators(self, flag: str) -> None:
        code, _ = invoke("bt", "subnet", "19", "identity", flag, "alice")
        assert code == 2


class TestValidatorCommissionTargetOperators:
    @pytest.mark.parametrize(
        ("flag", "target"),
        [("--changes-to", "0.18"), ("--changes-from", "0.05")],
    )
    def test_numeric_target_dispatches_as_float(self, flag: str, target: str) -> None:
        dispatch = AsyncMock(return_value=0)
        code, payload = invoke(
            "bt",
            "validator",
            ALICE_SS58,
            "commission",
            flag,
            target,
            dispatch_state_override=dispatch,
        )
        assert code == 0
        assert payload is None
        assert dispatch.await_args is not None
        assert dispatch.await_args.kwargs["target"] == float(target)

    @pytest.mark.parametrize("target", ["alice", "nan", "inf", "-0.01", "1.01"])
    def test_invalid_numeric_target_is_rejected(self, target: str) -> None:
        code, _ = invoke("bt", "validator", ALICE_SS58, "commission", "--changes-to", target)
        assert code == 2


class TestNeuronNumeric:
    @pytest.mark.parametrize("cmd", ["dividends", "stake-alpha"])
    def test_bare_leaf_shows_help(self, cmd: str) -> None:
        code, _ = invoke("bt", "neuron", "19", ALICE_SS58, cmd)
        assert code == 0

    @pytest.mark.parametrize("cmd", ["dividends", "stake-alpha"])
    def test_below_dispatches(self, cmd: str) -> None:
        code, payload = invoke("bt", "neuron", "19", ALICE_SS58, cmd, "--below", "1.0")
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")

    @pytest.mark.parametrize("cmd", ["dividends", "stake-alpha"])
    def test_delta_without_window_is_accepted(self, cmd: str) -> None:
        code, payload = invoke("bt", "neuron", "19", ALICE_SS58, cmd, "--drop-pct", "5")
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")


@pytest.mark.parametrize(
    "cmd",
    [
        "pruning-score",
        "blocks-until-deregistration",
        "epochs-until-immunity-expires",
    ],
)
def test_obsolete_neuron_commands_are_not_registered(cmd: str) -> None:
    code, payload = invoke("bt", "neuron", "19", ALICE_SS58, cmd, "--below", "100")
    assert code == 2
    assert payload is None


@pytest.mark.parametrize(
    ("resource", "args"),
    [
        ("validator", (ALICE_SS58, "stake", "--netuid", "19", "--below", "1")),
        ("validator", (ALICE_SS58, "dividends", "--netuid", "19", "--below", "1")),
        ("neuron", ("19", ALICE_SS58, "stake", "--below", "1")),
    ],
)
def test_replaced_numeric_aliases_are_not_registered(
    resource: str,
    args: tuple[str, ...],
) -> None:
    code, payload = invoke("bt", resource, *args)
    assert code == 2
    assert payload is None


class TestEventArgFilters:
    def test_unobservable_friendly_event_rejected_with_raw_hint(self) -> None:
        code, payload = invoke("bt", "event", "--type", "hyperparam-changed")
        assert code == 2
        assert payload is not None
        assert "--type-raw" in payload["message"]

    def test_type_and_type_raw_are_mutually_exclusive(self) -> None:
        code, payload = invoke(
            "bt",
            "event",
            "--type",
            "transfer",
            "--type-raw",
            "Balances.Transfer",
        )
        assert code == 2
        assert payload is not None
        assert "mutually exclusive" in payload["message"]

    def test_from_dispatches(self) -> None:
        code, payload = invoke(
            "bt", "event", "--type", "transfer", "--from", ALICE_SS58, "--max-runtime", "1s"
        )
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")

    def test_to_dispatches(self) -> None:
        code, payload = invoke(
            "bt", "event", "--type", "transfer", "--to", ALICE_SS58, "--max-runtime", "1s"
        )
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")

    def test_from_populates_args_match_filter(self) -> None:
        from chainwake.cli.chains.bittensor import (  # noqa: PLC0415
            _resolve_event_args_match,
        )

        assert _resolve_event_args_match(from_addr="5Grwv", to_addr=None) == {"from": "5Grwv"}

    def test_to_populates_args_match_filter(self) -> None:
        from chainwake.cli.chains.bittensor import (  # noqa: PLC0415
            _resolve_event_args_match,
        )

        assert _resolve_event_args_match(from_addr=None, to_addr="5Grwv") == {"to": "5Grwv"}

    def test_from_and_to_combine(self) -> None:
        from chainwake.cli.chains.bittensor import (  # noqa: PLC0415
            _resolve_event_args_match,
        )

        assert _resolve_event_args_match(from_addr="A", to_addr="B") == {"from": "A", "to": "B"}

    def test_no_filter_returns_none(self) -> None:
        from chainwake.cli.chains.bittensor import (  # noqa: PLC0415
            _resolve_event_args_match,
        )

        assert _resolve_event_args_match(from_addr=None, to_addr=None) is None

    def test_amount_min_dispatches(self) -> None:
        code, payload = invoke(
            "bt",
            "event",
            "--type",
            "transfer",
            "--amount-min",
            "1000",
            "--max-runtime",
            "1s",
        )
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")

    def test_negative_amount_min_rejected(self) -> None:
        code, payload = invoke("bt", "event", "--type", "transfer", "--amount-min", "-1")
        assert code == 2
        assert payload is not None
        assert "amount-min" in payload["message"]

    def test_direction_without_address_rejected(self) -> None:
        code, payload = invoke("bt", "event", "--type", "transfer", "--direction", "in")
        assert code == 2
        assert payload is not None
        assert "--address" in payload["message"]

    def test_direction_with_address_dispatches(self) -> None:
        code, payload = invoke(
            "bt",
            "event",
            "--type",
            "transfer",
            "--direction",
            "in",
            "--address",
            "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
            "--max-runtime",
            "1s",
        )
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")

    @pytest.mark.asyncio
    async def test_dispatch_event_threads_predicates_to_event_filter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from chainwake.cli.chains import dispatch  # noqa: PLC0415
        from chainwake.core.runtime import WatcherSpec  # noqa: PLC0415
        from chainwake.providers.base import EventFilter  # noqa: PLC0415

        captured: dict[str, WatcherSpec] = {}

        async def _stub(spec: WatcherSpec, /, **_kwargs: object) -> int:
            captured["spec"] = spec
            return 0

        monkeypatch.setattr(dispatch, "_run_with_error_handling", _stub)

        await dispatch.dispatch_event(
            event_type="transfer",
            args_match={"from": ALICE_SS58},
            entry_path="event.transfer",
            rpc_url="ws://localhost:9944",
            max_runtime_seconds=1.0,
            invocation=["chainwake", "bt", "event"],
            out_uris=[],
            amount_min=10_000,
            direction="in",
            direction_address=BOB_SS58,
        )

        ef = captured["spec"].event_filter
        assert isinstance(ef, EventFilter)
        assert ef.event_types == ("transfer",)
        assert ef.args_match == {"from": ALICE_SS58}
        assert ef.amount_min == 10_000
        assert ef.direction == "in"
        assert ef.direction_address == BOB_SS58

    @pytest.mark.asyncio
    async def test_dispatch_event_no_predicates_yields_minimal_filter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from chainwake.cli.chains import dispatch  # noqa: PLC0415
        from chainwake.core.runtime import WatcherSpec  # noqa: PLC0415
        from chainwake.providers.base import EventFilter  # noqa: PLC0415

        captured: dict[str, WatcherSpec] = {}

        async def _stub(spec: WatcherSpec, /, **_kwargs: object) -> int:
            captured["spec"] = spec
            return 0

        monkeypatch.setattr(dispatch, "_run_with_error_handling", _stub)

        await dispatch.dispatch_event(
            event_type="transfer",
            args_match=None,
            entry_path="event.transfer",
            rpc_url="ws://localhost:9944",
            max_runtime_seconds=1.0,
            invocation=["chainwake", "bt", "event"],
            out_uris=[],
        )

        ef = captured["spec"].event_filter
        assert isinstance(ef, EventFilter)
        assert ef.args_match == {}
        assert ef.amount_min is None
        assert ef.direction is None
        assert ef.direction_address is None


class TestAccountBalance:
    def test_bare_leaf_shows_help(self) -> None:
        code, _ = invoke("bt", "account", ALICE_SS58, "balance")
        assert code == 0

    def test_on_change_dispatches(self) -> None:
        code, payload = invoke("bt", "account", ALICE_SS58, "balance", "--on-change")
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")

    def test_below_dispatches(self) -> None:
        code, payload = invoke("bt", "account", ALICE_SS58, "balance", "--below", "10.0")
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")

    def test_subcommand_before_id_is_a_standard_parse_error(self) -> None:
        code, payload = invoke("bt", "account", "balance", ALICE_SS58, "--on-change")
        assert code == 2
        assert payload is None


class TestAccountActivity:
    def test_bare_leaf_shows_help(self) -> None:
        code, _ = invoke("bt", "account", ALICE_SS58, "activity")
        assert code == 0

    def test_valid_duration_dispatches(self) -> None:
        code, payload = invoke("bt", "account", ALICE_SS58, "activity", "--silent-for", "10m")
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")


class TestNetworkSubnetCount:
    def test_requires_condition(self) -> None:
        code, _ = invoke("bt", "network", "subnet-count")
        assert code == 2

    def test_above_dispatches(self) -> None:
        code, payload = invoke("bt", "network", "subnet-count", "--above", "100")
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")

    def test_delta_without_window_is_accepted(self) -> None:
        code, payload = invoke("bt", "network", "subnet-count", "--rise-pct", "5")
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")


class TestNetworkTaoPrice:
    def test_requires_condition(self) -> None:
        code, _ = invoke("bt", "network", "tao-price")
        assert code == 2

    def test_time_delta_dispatches(self) -> None:
        code, payload = invoke(
            "bt",
            "network",
            "tao-price",
            "--move-pct",
            "5",
            "--window-time",
            "1h",
        )
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")

    def test_block_window_is_not_available_for_external_price(self) -> None:
        code, _ = invoke(
            "bt",
            "network",
            "tao-price",
            "--move-pct",
            "5",
            "--window-blocks",
            "100",
        )
        assert code == 2


class TestNetworkOnRuntimeUpgraded:
    def test_dispatches_without_flags(self) -> None:
        code, payload = invoke("bt", "network", "on-runtime-upgraded", "--max-runtime", "1s")
        assert code != 2 or (payload is not None and payload.get("status") != "user_error")

    def test_dispatch_preserves_network_watcher_identity(self) -> None:
        dispatch = AsyncMock(return_value=0)
        code, _payload = invoke(
            "bt",
            "network",
            "on-runtime-upgraded",
            "--max-runtime",
            "1s",
            dispatch_event_override=dispatch,
        )

        assert code == 0
        await_args = dispatch.await_args
        assert await_args is not None
        assert await_args.kwargs["resource"] == "network"
        assert await_args.kwargs["sub_resource"] == "on-runtime-upgraded"


class TestNeuronImmunityExpires:
    def test_bare_leaf_shows_help(self) -> None:
        code, _ = invoke("bt", "neuron", "19", ALICE_SS58, "blocks-until-immunity-expires")
        assert code == 0

    def test_below_dispatches(self) -> None:
        dispatch = AsyncMock(return_value=0)
        code, _payload = invoke(
            "bt",
            "neuron",
            "19",
            ALICE_SS58,
            "blocks-until-immunity-expires",
            "--below",
            "100",
            dispatch_threshold_override=dispatch,
        )

        assert code == 0
        await_args = dispatch.await_args
        assert await_args is not None
        assert (
            await_args.kwargs["entry_path"]
            == "neuron.{netuid}.{hotkey}.blocks-until-immunity-expires"
        )
        assert await_args.kwargs["sub_resource"] == "blocks-until-immunity-expires"
