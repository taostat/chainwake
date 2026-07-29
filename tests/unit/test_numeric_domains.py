"""Regression coverage for finite, positive watcher controls."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from chainwake.cli.chains.bittensor import (
    _parse_max_runtime,
    _resolve_delta,
    _resolve_threshold,
    _validate_duration_flag,
)
from chainwake.cli.chains.common import resolve_window as _resolve_window
from chainwake.cli.inputs.common import AboveCondition, BelowCondition, MovePctCondition
from chainwake.cli.inputs.event import EventInput
from chainwake.cli.inputs.subnet import SubnetDepthForTradeInput, SubnetPriceInput
from chainwake.cli.inputs.tx import TxInput
from chainwake.cli.inputs.validator import ValidatorWeightsInput
from chainwake.core.errors import UserError
from chainwake.core.primitives.delta import DeltaPrimitive, WindowUnit
from chainwake.core.primitives.liveness import LivenessPrimitive
from chainwake.core.primitives.threshold import ThresholdPrimitive
from chainwake.core.primitives.tx import TxPrimitive
from chainwake.core.runtime import WatcherSpec
from chainwake.output.schema import (
    DeltaCondition,
    LivenessCondition,
    ObservedThreshold,
    ThresholdCondition,
    Window,
)
from chainwake.providers.bittensor import BittensorProvider
from tests.ss58 import ALICE_SS58

pytestmark = pytest.mark.unit


def test_mcp_event_amount_min_is_non_negative_and_advertised() -> None:
    with pytest.raises(ValidationError):
        EventInput(event_type="transfer", amount_min=-1)

    schema = EventInput.model_json_schema()
    assert schema["properties"]["amount_min"]["anyOf"][0]["minimum"] == 0


@pytest.mark.parametrize(
    "tx_hash",
    ["0xabc", "ab" * 32, "0x" + "gg" * 32, "0x" + "ab" * 31],
)
def test_mcp_tx_hash_requires_32_byte_prefixed_hex(tx_hash: str) -> None:
    with pytest.raises(ValidationError):
        TxInput(tx_hash=tx_hash, finality="finalized")


def test_mcp_tx_hash_schema_advertises_exact_hex_domain() -> None:
    schema = TxInput.model_json_schema()

    assert schema["properties"]["tx_hash"]["pattern"] == r"^0x[0-9a-fA-F]{64}$"


@pytest.mark.parametrize("tx_hash", ["0xabc", "0x" + "gg" * 32])
def test_tx_primitive_defensively_rejects_invalid_hash(tx_hash: str) -> None:
    with pytest.raises(ValueError, match="32-byte"):
        TxPrimitive(tx_hash=tx_hash, finality="finalized")


@pytest.mark.asyncio
async def test_provider_defensively_rejects_invalid_tx_hash_before_rpc() -> None:
    with pytest.raises(UserError, match="32-byte"):
        await BittensorProvider().get_block_finality("0xabc")


@pytest.mark.parametrize("field", ["size", "max_bps"])
@pytest.mark.parametrize("value", [0.0, -1.0, math.inf, -math.inf, math.nan])
def test_mcp_depth_for_trade_inputs_are_finite_and_positive(
    field: str,
    value: float,
) -> None:
    kwargs: dict[str, object] = {
        "netuid": 1,
        "size": 100.0,
        "max_bps": 50.0,
        "condition": {"kind": "above", "value": 0.0},
    }
    kwargs[field] = value

    with pytest.raises(ValidationError):
        SubnetDepthForTradeInput.model_validate(kwargs)


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
def test_observed_threshold_rejects_non_finite_json_numbers(value: float) -> None:
    with pytest.raises(ValidationError):
        ObservedThreshold(
            path="subnet.1.pool.depth-for-trade",
            value=value,
            block=1,
            block_hash="0xabc",
            timestamp="2026-01-01T00:00:00Z",
        )


@pytest.mark.parametrize("field", ["size", "max_bps"])
@pytest.mark.parametrize("value", [None, 0, -1, math.inf, -math.inf, math.nan])
def test_provider_defensively_rejects_invalid_trade_args(
    field: str,
    value: object,
) -> None:
    args: dict[str, object] = {"size": 100.0, "max_bps": 50.0}
    args[field] = value

    with pytest.raises(UserError, match="finite and greater than zero"):
        BittensorProvider._read_positive_float_arg(args, field)


@pytest.mark.parametrize("target", [math.inf, -math.inf, math.nan])
def test_cli_threshold_resolver_rejects_non_finite_values(target: float) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _resolve_threshold(None, target)
    assert exc_info.value.code == 2


@pytest.mark.parametrize("model", [BelowCondition, AboveCondition])
@pytest.mark.parametrize("target", [math.inf, -math.inf, math.nan])
def test_mcp_threshold_condition_rejects_non_finite_values(
    model: type[BelowCondition] | type[AboveCondition],
    target: float,
) -> None:
    with pytest.raises(ValidationError):
        model(value=target)


@pytest.mark.parametrize("target", [math.inf, -math.inf, math.nan])
def test_threshold_primitive_defensively_rejects_non_finite_target(target: float) -> None:
    with pytest.raises(ValueError, match="threshold target must be finite"):
        ThresholdPrimitive(operator="above", target=target)


def test_watcher_spec_defensively_rejects_non_finite_threshold() -> None:
    condition = ThresholdCondition.model_construct(operator="above", target=math.nan)
    with pytest.raises(ValueError, match="threshold target must be finite"):
        WatcherSpec(
            chain="bt",
            resource="subnet",
            path_params={"netuid": "1"},
            sub_resource="pool.price",
            primitive_name="threshold",
            condition=condition,
            invocation=["chainwake"],
        )


@pytest.mark.parametrize("target", [0.0, -1.0, math.inf, -math.inf, math.nan])
def test_cli_delta_resolver_rejects_non_positive_or_non_finite_pct(target: float) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _resolve_delta(
            drop_pct=None,
            rise_pct=None,
            move_pct=target,
            window_time=None,
            window_blocks=None,
            window_epochs=None,
        )
    assert exc_info.value.code == 2


@pytest.mark.parametrize(
    ("window_time", "window_blocks", "window_epochs"),
    [
        ("0s", None, None),
        (None, 0, None),
        (None, None, -1),
    ],
)
def test_cli_window_resolver_rejects_non_positive_explicit_window(
    window_time: str | None,
    window_blocks: int | None,
    window_epochs: int | None,
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _resolve_window(window_time, window_blocks, window_epochs)
    assert exc_info.value.code == 2


@pytest.mark.parametrize("raw", ["0", "-1", "nan", "inf", "-inf", "0s"])
def test_cli_max_runtime_rejects_non_positive_or_non_finite_values(raw: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _parse_max_runtime(raw)
    assert exc_info.value.code == 2


@pytest.mark.parametrize("raw", ["0s", "0blocks", "0epochs"])
def test_cli_liveness_rejects_zero_duration(raw: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _validate_duration_flag(raw, "--silent-for")
    assert exc_info.value.code == 2


@pytest.mark.parametrize("target", [0.0, -1.0, math.inf, -math.inf, math.nan])
def test_mcp_delta_condition_rejects_non_positive_or_non_finite_pct(target: float) -> None:
    with pytest.raises(ValidationError):
        MovePctCondition(pct=target)


@pytest.mark.parametrize(
    "condition",
    [
        {"kind": "move-pct", "pct": 1.0, "window_time": "0s"},
        {"kind": "move-pct", "pct": 1.0, "window_blocks": 0},
        {"kind": "move-pct", "pct": 1.0, "window_epochs": -1},
    ],
)
def test_mcp_delta_condition_rejects_non_positive_explicit_window(
    condition: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        SubnetPriceInput(netuid=1, condition=condition)


@pytest.mark.parametrize("raw", ["0", "-1", "nan", "inf", "-inf", "0s"])
def test_mcp_input_rejects_invalid_explicit_max_runtime(raw: str) -> None:
    with pytest.raises(ValidationError):
        SubnetPriceInput(
            netuid=1,
            condition={"kind": "below", "value": 1.0},
            max_runtime=raw,
        )


def test_mcp_schema_advertises_strict_numeric_domains() -> None:
    schema = SubnetPriceInput.model_json_schema()

    condition_variants = schema["$defs"]["MovePctCondition"]["properties"]
    assert condition_variants["pct"]["exclusiveMinimum"] == 0
    assert condition_variants["window_blocks"]["anyOf"][0]["exclusiveMinimum"] == 0
    assert condition_variants["window_epochs"]["anyOf"][0]["exclusiveMinimum"] == 0


@pytest.mark.parametrize("silent_for", ["0s", "0blocks", "0epochs"])
def test_mcp_liveness_input_rejects_zero_duration(silent_for: str) -> None:
    with pytest.raises(ValidationError):
        ValidatorWeightsInput(hotkey=ALICE_SS58, silent_for=silent_for)


@pytest.mark.parametrize(
    ("poll_seconds", "max_runtime_seconds"),
    [
        (0.0, None),
        (-1.0, None),
        (math.inf, None),
        (math.nan, None),
        (None, 0.0),
        (None, -1.0),
        (None, math.inf),
        (None, math.nan),
    ],
)
def test_watcher_spec_rejects_invalid_runtime_controls(
    poll_seconds: float | None,
    max_runtime_seconds: float | None,
) -> None:
    with pytest.raises(ValueError, match="finite and greater than zero"):
        WatcherSpec(
            chain="bt",
            resource="subnet",
            path_params={"netuid": "1"},
            sub_resource="pool.price",
            primitive_name="threshold",
            condition=LivenessCondition(operator="silent-for", duration="1s"),
            invocation=["chainwake"],
            poll_seconds=poll_seconds,
            max_runtime_seconds=max_runtime_seconds,
        )


@pytest.mark.parametrize("target", [0.0, -1.0, math.inf, math.nan])
def test_delta_primitive_defensively_rejects_invalid_target(target: float) -> None:
    with pytest.raises(ValueError, match="delta target must be finite"):
        DeltaPrimitive(
            operator="move-pct",
            target=target,
            window_unit="ever",
            window_value="watcher-start",
        )


@pytest.mark.parametrize(
    ("unit", "value"),
    [("time", "0s"), ("blocks", "0"), ("epochs", "-1")],
)
def test_delta_primitive_defensively_rejects_invalid_window(
    unit: WindowUnit,
    value: str,
) -> None:
    with pytest.raises(ValueError, match="explicit window must be finite"):
        DeltaPrimitive(
            operator="move-pct",
            target=1.0,
            window_unit=unit,
            window_value=value,
        )


@pytest.mark.parametrize("duration", ["0s", "0blocks", "0epochs"])
def test_liveness_primitive_defensively_rejects_zero_duration(duration: str) -> None:
    with pytest.raises(ValueError, match="silent_for must be finite"):
        LivenessPrimitive(silent_for=duration)


def test_watcher_spec_defensively_rejects_invalid_delta_condition() -> None:
    condition = DeltaCondition.model_construct(
        operator="move-pct",
        target=0.0,
        window=Window(unit="ever", value="watcher-start"),
    )
    with pytest.raises(ValueError, match="delta target must be finite"):
        WatcherSpec(
            chain="bt",
            resource="subnet",
            path_params={"netuid": "1"},
            sub_resource="pool.price",
            primitive_name="delta",
            condition=condition,
            invocation=["chainwake"],
        )
