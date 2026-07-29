"""Golden roundtrip + JSON Schema validation tests for the output contract."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import jsonschema
import pytest
from pydantic import TypeAdapter, ValidationError

from chainwake.output.schema import (
    AuthErrorPayload,
    Budget,
    BudgetExhaustedPayload,
    DeltaCondition,
    EventCondition,
    InternalErrorPayload,
    LivenessCondition,
    MatchedPayload,
    ObservedDelta,
    ObservedEvent,
    ObservedLiveness,
    ObservedState,
    ObservedThreshold,
    ObservedTx,
    Payload,
    Process,
    ProviderErrorPayload,
    StateCondition,
    ThresholdCondition,
    TimeoutPayload,
    TxCondition,
    UserErrorPayload,
    Watcher,
    Window,
)
from scripts.generate_json_schema import build_schema

pytestmark = pytest.mark.unit


SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "output.json"
PAYLOAD_ADAPTER: TypeAdapter[Payload] = TypeAdapter(Payload)


def _ts() -> datetime:
    return datetime(2026, 5, 5, 19, 42, 10, tzinfo=UTC)


def _budget() -> Budget:
    return Budget(runtime_ms=384_201, rpc_calls=42, estimated_ru_consumed=42)


def _process() -> Process:
    return Process(pid=12_453, started_at=_ts())


def _watcher_subnet_price() -> Watcher:
    return Watcher(
        chain="bt",
        resource="subnet",
        resource_id="19",
        sub_resource="pool.price",
        name=None,
        primitive="delta",
        invocation=[
            "chainwake",
            "bt",
            "subnet",
            "19",
            "price",
            "--drop-pct",
            "5",
            "--window-time",
            "1h",
        ],
    )


def _matched_delta() -> MatchedPayload:
    return MatchedPayload(
        watcher=_watcher_subnet_price(),
        condition=DeltaCondition(
            operator="drop-pct",
            target=5.0,
            window=Window(unit="time", value="1h"),
        ),
        observed=ObservedDelta(
            path="subnet.19.pool.price",
            value=0.0432,
            previous_value=0.0455,
            delta=-0.0023,
            delta_pct=-5.05,
            block=8_119_123,
            block_hash="0xabc",
            timestamp=_ts(),
        ),
        budget=_budget(),
        process=_process(),
    )


def test_delta_condition_represents_unbounded_watcher_start_window() -> None:
    condition = DeltaCondition(
        operator="move-pct",
        target=1.0,
        window=Window(unit="ever", value="watcher-start"),
    )

    assert condition.model_dump(mode="json") == {
        "operator": "move-pct",
        "target": 1.0,
        "window": {"unit": "ever", "value": "watcher-start"},
    }


def _watcher_subnet_event() -> Watcher:
    return Watcher(
        chain="bt",
        resource="subnet",
        resource_id=None,
        sub_resource=None,
        name="subnet-registration-watcher",
        primitive="event",
        invocation=["chainwake", "bt", "event", "--type", "subnet-registered"],
    )


def _matched_event() -> MatchedPayload:
    return MatchedPayload(
        watcher=_watcher_subnet_event(),
        condition=EventCondition(event_type="subnet-registered"),
        observed=ObservedEvent(
            event_type="subnet-registered",
            raw_event="SubtensorModule.NetworkAdded",
            args={"netuid": 130, "owner_coldkey": "5Fxxx"},
            block=8_119_156,
            block_hash="0xdef",
            timestamp=_ts(),
            extrinsic_hash="0x789",
        ),
        budget=_budget(),
        process=_process(),
    )


def _watcher_threshold() -> Watcher:
    return Watcher(
        chain="bt",
        resource="subnet",
        resource_id="19",
        sub_resource="pool.price",
        primitive="threshold",
        invocation=["chainwake", "bt", "subnet", "19", "price", "--below", "0.05"],
    )


def _matched_threshold() -> MatchedPayload:
    return MatchedPayload(
        watcher=_watcher_threshold(),
        condition=ThresholdCondition(operator="below", target=0.05),
        observed=ObservedThreshold(
            path="subnet.19.pool.price",
            value=0.0432,
            block=8_119_123,
            block_hash="0xabc",
            timestamp=_ts(),
        ),
        budget=_budget(),
        process=_process(),
    )


def _watcher_state() -> Watcher:
    return Watcher(
        chain="bt",
        resource="validator",
        resource_id="5Fxxx",
        sub_resource="commission",
        primitive="state",
        invocation=["chainwake", "bt", "validator", "5Fxxx", "commission", "--on-change"],
    )


def _matched_state() -> MatchedPayload:
    return MatchedPayload(
        watcher=_watcher_state(),
        condition=StateCondition(operator="on-change"),
        observed=ObservedState(
            path="validator.5Fxxx.commission",
            value=0.18,
            previous_value=0.15,
            block=8_119_001,
            block_hash="0xaaa",
            timestamp=_ts(),
        ),
        budget=_budget(),
        process=_process(),
    )


def _matched_list_state() -> MatchedPayload:
    return MatchedPayload(
        watcher=Watcher(
            chain="bt",
            resource="validator",
            resource_id="5Fxxx",
            sub_resource="child-keys",
            primitive="state",
            invocation=[
                "chainwake",
                "bt",
                "validator",
                "5Fxxx",
                "child-keys",
                "--on-change",
            ],
        ),
        condition=StateCondition(operator="on-change"),
        observed=ObservedState(
            path="validator.5Fxxx.child-keys",
            value=[
                {"netuid": 1, "child": "5Child"},
                {"netuid": 2, "child": "5Other"},
            ],
            previous_value=[{"netuid": 1, "child": "5Child"}],
            block=8_119_002,
            block_hash="0xaab",
            timestamp=_ts(),
        ),
        budget=_budget(),
        process=_process(),
    )


def _watcher_liveness() -> Watcher:
    return Watcher(
        chain="bt",
        resource="validator",
        resource_id="5Fxxx",
        sub_resource="weights",
        primitive="liveness",
        invocation=[
            "chainwake",
            "bt",
            "validator",
            "5Fxxx",
            "weights",
            "--silent-for",
            "3epochs",
        ],
    )


def _matched_liveness() -> MatchedPayload:
    return MatchedPayload(
        watcher=_watcher_liveness(),
        condition=LivenessCondition(operator="silent-for", duration="3epochs"),
        observed=ObservedLiveness(
            path="validator.5Fxxx.weights",
            last_seen_block=8_118_000,
            last_seen_timestamp=_ts(),
            elapsed="3epochs",
            block=8_119_080,
            block_hash="0xbbb",
            timestamp=_ts(),
        ),
        budget=_budget(),
        process=_process(),
    )


def _watcher_tx() -> Watcher:
    return Watcher(
        chain="bt",
        resource="tx",
        resource_id="0xabc",
        primitive="tx",
        invocation=["chainwake", "bt", "tx", "0xabc", "--finality", "finalized"],
    )


def _matched_tx() -> MatchedPayload:
    return MatchedPayload(
        watcher=_watcher_tx(),
        condition=TxCondition(finality="finalized"),
        observed=ObservedTx(
            tx_hash="0xabc",
            finality="finalized",
            block=8_119_123,
            block_hash="0xabc",
            timestamp=_ts(),
        ),
        budget=_budget(),
        process=_process(),
    )


def _matched_evm_tx() -> MatchedPayload:
    return MatchedPayload(
        watcher=Watcher(
            chain="eth",
            resource="tx",
            resource_id="0xdef",
            primitive="tx",
            invocation=["chainwake", "eth", "tx", "0xdef", "--confirmations", "2"],
        ),
        condition=TxCondition(finality="included", confirmations=2),
        observed=ObservedTx(
            tx_hash="0xdef",
            finality="included",
            block=8_119_124,
            block_hash="0xdef",
            timestamp=_ts(),
            confirmations=2,
            execution_status="success",
            gas_used=21_000,
            effective_gas_price_wei=1_500_000_000,
        ),
        budget=_budget(),
        process=_process(),
    )


GOLDEN_PAYLOADS: list[Payload] = [
    _matched_delta(),
    _matched_threshold(),
    _matched_state(),
    _matched_list_state(),
    _matched_liveness(),
    _matched_event(),
    _matched_tx(),
    _matched_evm_tx(),
    TimeoutPayload(
        watcher=_watcher_threshold(),
        condition=ThresholdCondition(operator="below", target=0.05),
        reason="max_runtime_reached",
        budget=_budget(),
        process=_process(),
    ),
    BudgetExhaustedPayload(
        watcher=_watcher_threshold(),
        condition=ThresholdCondition(operator="below", target=0.05),
        reason="max_ru_reached",
        budget=_budget(),
        process=_process(),
    ),
    UserErrorPayload(
        reason="invalid_flag",
        message="--below requires a numeric value",
        budget=Budget(runtime_ms=12, rpc_calls=0, estimated_ru_consumed=0),
        process=_process(),
    ),
    ProviderErrorPayload(
        reason="auth_failed",
        message="RPC returned -32021: invalid API key",
        watcher=_watcher_threshold(),
        condition=ThresholdCondition(operator="below", target=0.05),
        budget=_budget(),
        process=_process(),
    ),
    AuthErrorPayload(
        reason="invalid_api_key",
        message="Authentication failed for chain 'bt'.",
        watcher=_watcher_threshold(),
        condition=ThresholdCondition(operator="below", target=0.05),
        budget=_budget(),
        process=_process(),
        api_key_env_vars=["CHAINWAKE_BT_API_KEY", "CHAINWAKE_API_KEY"],
        docs_url="https://blockmachine.io",
    ),
    InternalErrorPayload(
        reason="unhandled_exception",
        message="ValueError in registry lookup",
        watcher=_watcher_threshold(),
        condition=ThresholdCondition(operator="below", target=0.05),
        budget=_budget(),
        process=_process(),
    ),
]


@pytest.mark.parametrize("payload", GOLDEN_PAYLOADS, ids=lambda p: p.status)
def test_roundtrip(payload: Payload) -> None:
    raw = payload.model_dump(mode="json")
    parsed = PAYLOAD_ADAPTER.validate_python(raw)
    assert parsed == payload


def test_transaction_receipt_context_is_all_present_or_all_absent() -> None:
    generic = ObservedTx(
        tx_hash="0xdeadbeef",
        finality="included",
        block=1,
        block_hash="0xabc",
        timestamp=_ts(),
    )
    assert generic.confirmations is None
    generic_raw = generic.model_dump(mode="json")
    assert set(generic_raw) == {
        "tx_hash",
        "finality",
        "block",
        "block_hash",
        "timestamp",
    }

    rich = ObservedTx(
        tx_hash="0xdeadbeef",
        finality="included",
        block=1,
        block_hash="0xabc",
        timestamp=_ts(),
        confirmations=2,
        execution_status="success",
        gas_used=21_000,
        effective_gas_price_wei=1_500_000_000,
    )
    assert rich.confirmations == 2

    with pytest.raises(ValidationError, match="receipt context"):
        ObservedTx(
            tx_hash="0xdeadbeef",
            finality="included",
            block=1,
            block_hash="0xabc",
            timestamp=_ts(),
            confirmations=2,
        )


@pytest.mark.parametrize(
    "field",
    [
        "confirmations",
        "execution_status",
        "gas_used",
        "effective_gas_price_wei",
    ],
)
def test_transaction_receipt_context_rejects_each_missing_or_partial_field(field: str) -> None:
    schema_validator = jsonschema.Draft202012Validator(build_schema())

    rich_raw = cast(dict[str, Any], _matched_evm_tx().model_dump(mode="json"))
    del rich_raw["observed"][field]
    with pytest.raises(ValidationError, match="receipt context"):
        PAYLOAD_ADAPTER.validate_python(rich_raw)
    with pytest.raises(jsonschema.ValidationError):
        schema_validator.validate(rich_raw)

    generic_raw = cast(dict[str, Any], _matched_tx().model_dump(mode="json"))
    generic_raw["observed"][field] = None
    with pytest.raises(ValidationError, match="receipt context"):
        PAYLOAD_ADAPTER.validate_python(generic_raw)
    with pytest.raises(jsonschema.ValidationError):
        schema_validator.validate(generic_raw)


def test_transaction_receipt_context_rejects_four_explicit_nulls() -> None:
    schema_validator = jsonschema.Draft202012Validator(build_schema())
    raw = cast(dict[str, Any], _matched_tx().model_dump(mode="json"))
    raw["observed"].update(
        {
            "confirmations": None,
            "execution_status": None,
            "gas_used": None,
            "effective_gas_price_wei": None,
        }
    )

    with pytest.raises(ValidationError, match="omit all receipt fields"):
        PAYLOAD_ADAPTER.validate_python(raw)
    with pytest.raises(jsonschema.ValidationError):
        schema_validator.validate(raw)


def test_unknown_field_rejected() -> None:
    raw = _matched_delta().model_dump(mode="json")
    raw["surprise"] = "x"
    with pytest.raises(ValidationError):
        PAYLOAD_ADAPTER.validate_python(raw)


def test_status_discriminator_required() -> None:
    raw = _matched_delta().model_dump(mode="json")
    del raw["status"]
    with pytest.raises(ValidationError):
        PAYLOAD_ADAPTER.validate_python(raw)


def test_matched_payload_requires_observed() -> None:
    raw = _matched_threshold().model_dump(mode="json")
    del raw["observed"]
    with pytest.raises(ValidationError):
        PAYLOAD_ADAPTER.validate_python(raw)


def test_timeout_observed_must_be_null() -> None:
    raw = TimeoutPayload(
        watcher=_watcher_threshold(),
        condition=ThresholdCondition(operator="below", target=0.05),
        reason="max_runtime_reached",
        budget=_budget(),
        process=_process(),
    ).model_dump(mode="json")
    raw["observed"] = {"foo": "bar"}
    with pytest.raises(ValidationError):
        PAYLOAD_ADAPTER.validate_python(raw)


def test_numeric_observation_accepts_json_provider_context() -> None:
    observed = ObservedThreshold(
        path="token.DAI.price",
        value=1.0001,
        block=42,
        block_hash="0xabc",
        timestamp=_ts(),
        meta={
            "source": "coingecko",
            "quote_currency": "usd",
            "token_address": "0x6b175474e89094c44da98b954eedeac495271d0f",
        },
    )

    assert observed.model_dump(mode="json")["meta"]["source"] == "coingecko"


def test_published_schema_in_sync() -> None:
    on_disk = json.loads(SCHEMA_PATH.read_text())
    assert on_disk == build_schema(), (
        "schemas/output.json is out of sync with chainwake.output.schema; "
        "regenerate via scripts/generate_json_schema.py."
    )


def test_payloads_validate_against_published_schema() -> None:
    schema = json.loads(SCHEMA_PATH.read_text())
    validator = jsonschema.Draft202012Validator(schema)
    for payload in GOLDEN_PAYLOADS:
        raw = cast(dict[str, Any], payload.model_dump(mode="json"))
        validator.validate(raw)


# ---------------------------------------------------------------------------
# AuthErrorPayload (spec §9.4 / Appendix D)
# ---------------------------------------------------------------------------


def _auth_error_payload() -> AuthErrorPayload:
    return AuthErrorPayload(
        reason="missing_api_key",
        message="Authentication failed for chain 'bt'.",
        watcher=_watcher_threshold(),
        condition=ThresholdCondition(operator="below", target=0.05),
        budget=_budget(),
        process=_process(),
        api_key_env_vars=["CHAINWAKE_BT_API_KEY", "CHAINWAKE_API_KEY"],
        docs_url="https://blockmachine.io",
    )


def test_auth_error_payload_status_is_auth_error() -> None:
    payload = _auth_error_payload()
    assert payload.status == "auth_error"


def test_auth_error_payload_default_reason_is_auth_failed() -> None:
    payload = AuthErrorPayload(
        message="Authentication failed for chain 'bt'.",
        watcher=_watcher_threshold(),
        condition=ThresholdCondition(operator="below", target=0.05),
        budget=_budget(),
        process=_process(),
    )
    assert payload.reason == "auth_failed"
    assert payload.api_key_env_vars == []
    assert payload.docs_url is None


def test_auth_error_payload_rejects_unknown_reason() -> None:
    with pytest.raises(ValidationError):
        AuthErrorPayload(
            reason="totally_made_up",  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
            message="x",
            watcher=_watcher_threshold(),
            condition=ThresholdCondition(operator="below", target=0.05),
            budget=_budget(),
            process=_process(),
        )


def test_auth_error_payload_requires_message() -> None:
    raw = _auth_error_payload().model_dump(mode="json")
    del raw["message"]
    with pytest.raises(ValidationError):
        PAYLOAD_ADAPTER.validate_python(raw)


def test_auth_error_payload_routes_via_status_discriminator() -> None:
    raw = _auth_error_payload().model_dump(mode="json")
    parsed = PAYLOAD_ADAPTER.validate_python(raw)
    assert isinstance(parsed, AuthErrorPayload)
    assert parsed.api_key_env_vars == ["CHAINWAKE_BT_API_KEY", "CHAINWAKE_API_KEY"]
    assert parsed.docs_url == "https://blockmachine.io"


def test_auth_error_payload_observed_must_be_null() -> None:
    raw = _auth_error_payload().model_dump(mode="json")
    raw["observed"] = {"foo": "bar"}
    with pytest.raises(ValidationError):
        PAYLOAD_ADAPTER.validate_python(raw)
