"""Unit tests for chainwake.output.render.render_human.

Covers every payload type and asserts deterministic, single-pass rendering
of the prose form chainwake emits when stdout is a TTY (or when --no-json
forces human mode).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

import pytest

from chainwake.output.render import render_human
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
    PrimitiveName,
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

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Common fixtures
# ---------------------------------------------------------------------------


def _ts() -> datetime:
    return datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)


def _budget(runtime_ms: int = 100, ru: int = 5) -> Budget:
    return Budget(runtime_ms=runtime_ms, rpc_calls=1, estimated_ru_consumed=ru)


def _process() -> Process:
    return Process(pid=999, started_at=_ts())


def _watcher(
    *,
    chain: Literal["bt", "eth"] = "bt",
    resource: str = "subnet",
    resource_id: str | None = "19",
    sub_resource: str | None = "pool.price",
    primitive: PrimitiveName = "threshold",
) -> Watcher:
    return Watcher(
        chain=chain,
        resource=resource,
        resource_id=resource_id,
        sub_resource=sub_resource,
        primitive=primitive,
        invocation=["chainwake", "bt", resource, "..."],
    )


# ---------------------------------------------------------------------------
# Matched — one snapshot test per primitive
# ---------------------------------------------------------------------------


class TestMatchedRendering:
    def test_threshold(self) -> None:
        payload = MatchedPayload(
            watcher=_watcher(),
            condition=ThresholdCondition(operator="below", target=0.05),
            observed=ObservedThreshold(
                path="subnet.19.pool.price",
                value=0.0123,
                block=4839172,
                block_hash="0xabc",
                timestamp=_ts(),
            ),
            budget=_budget(),
            process=_process(),
        )
        assert render_human(payload) == (
            "matched: subnet 19 pool.price = 0.0123 (below 0.05) at block 4839172"
        )

    def test_threshold_above(self) -> None:
        payload = MatchedPayload(
            watcher=_watcher(),
            condition=ThresholdCondition(operator="above", target=1.5),
            observed=ObservedThreshold(
                path="subnet.19.pool.price",
                value=2.0,
                block=100,
                block_hash="0xabc",
                timestamp=_ts(),
            ),
            budget=_budget(),
            process=_process(),
        )
        assert "above 1.5" in render_human(payload)
        assert "= 2.0 " in render_human(payload)

    def test_delta(self) -> None:
        payload = MatchedPayload(
            watcher=_watcher(primitive="delta"),
            condition=DeltaCondition(
                operator="drop-pct",
                target=10.0,
                window=Window(unit="time", value="1h"),
            ),
            observed=ObservedDelta(
                path="subnet.19.pool.price",
                value=0.9,
                previous_value=1.0,
                delta=-0.1,
                delta_pct=-10.0,
                block=200,
                block_hash="0xabc",
                timestamp=_ts(),
            ),
            budget=_budget(),
            process=_process(),
        )
        rendered = render_human(payload)
        assert rendered.startswith("matched: subnet 19 pool.price moved")
        assert "-10%" in rendered
        assert "1h" in rendered
        assert "1.0 -> 0.9" in rendered
        assert "block 200" in rendered

    def test_delta_blocks_window(self) -> None:
        payload = MatchedPayload(
            watcher=_watcher(primitive="delta"),
            condition=DeltaCondition(
                operator="rise-pct",
                target=5.0,
                window=Window(unit="blocks", value="50"),
            ),
            observed=ObservedDelta(
                path="x",
                value=2.0,
                previous_value=1.0,
                delta=1.0,
                delta_pct=100.0,
                block=300,
                block_hash="0xabc",
                timestamp=_ts(),
            ),
            budget=_budget(),
            process=_process(),
        )
        assert "50 blocks" in render_human(payload)

    def test_delta_epochs_window(self) -> None:
        payload = MatchedPayload(
            watcher=_watcher(primitive="delta"),
            condition=DeltaCondition(
                operator="move-pct",
                target=5.0,
                window=Window(unit="epochs", value="3"),
            ),
            observed=ObservedDelta(
                path="x",
                value=2.0,
                previous_value=1.0,
                delta=1.0,
                delta_pct=100.0,
                block=300,
                block_hash="0xabc",
                timestamp=_ts(),
            ),
            budget=_budget(),
            process=_process(),
        )
        assert "3 epochs" in render_human(payload)

    def test_delta_ever_window(self) -> None:
        payload = MatchedPayload(
            watcher=_watcher(primitive="delta"),
            condition=DeltaCondition(
                operator="move-pct",
                target=1.0,
                window=Window(unit="ever", value="watcher-start"),
            ),
            observed=ObservedDelta(
                path="subnet.28.burn-rate",
                value=0.2,
                previous_value=0.1,
                delta=0.1,
                delta_pct=100.0,
                block=123,
                block_hash="0xabc",
                timestamp=_ts(),
            ),
            budget=_budget(),
            process=_process(),
        )
        assert "since watcher start" in render_human(payload)

    def test_state(self) -> None:
        payload = MatchedPayload(
            watcher=_watcher(
                resource="validator",
                resource_id="5GBxyz",
                sub_resource="status",
                primitive="state",
            ),
            condition=StateCondition(operator="on-change"),
            observed=ObservedState(
                path="validator.5GBxyz.status",
                value="active",
                previous_value="inactive",
                block=42,
                block_hash="0xabc",
                timestamp=_ts(),
            ),
            budget=_budget(),
            process=_process(),
        )
        assert render_human(payload) == (
            "matched: validator 5GBxyz status changed (inactive -> active) at block 42"
        )

    def test_liveness(self) -> None:
        payload = MatchedPayload(
            watcher=_watcher(
                resource="neuron",
                resource_id="1",
                sub_resource="last-update",
                primitive="liveness",
            ),
            condition=LivenessCondition(operator="silent-for", duration="3epochs"),
            observed=ObservedLiveness(
                path="neuron.1.last-update",
                last_seen_block=100,
                last_seen_timestamp=_ts(),
                elapsed="3epochs",
                block=400,
                block_hash="0xabc",
                timestamp=_ts(),
            ),
            budget=_budget(),
            process=_process(),
        )
        assert render_human(payload) == (
            "matched: neuron 1 last-update silent for 3epochs at block 400"
        )

    def test_event(self) -> None:
        payload = MatchedPayload(
            watcher=_watcher(
                resource="event",
                resource_id=None,
                sub_resource="NetworkAdded",
                primitive="event",
            ),
            condition=EventCondition(event_type="NetworkAdded"),
            observed=ObservedEvent(
                event_type="NetworkAdded",
                raw_event="{...}",
                args={"netuid": 19, "owner": "5GAlice"},
                block=500,
                block_hash="0xabc",
                timestamp=_ts(),
            ),
            budget=_budget(),
            process=_process(),
        )
        rendered = render_human(payload)
        assert rendered.startswith("matched: NetworkAdded at block 500")
        assert "netuid=19" in rendered
        assert "owner=5GAlice" in rendered

    def test_event_no_args(self) -> None:
        payload = MatchedPayload(
            watcher=_watcher(
                resource="event",
                resource_id=None,
                sub_resource="SubnetRegistered",
                primitive="event",
            ),
            condition=EventCondition(event_type="SubnetRegistered"),
            observed=ObservedEvent(
                event_type="SubnetRegistered",
                raw_event="{}",
                args={},
                block=600,
                block_hash="0xabc",
                timestamp=_ts(),
            ),
            budget=_budget(),
            process=_process(),
        )
        assert render_human(payload) == "matched: SubnetRegistered at block 600"

    def test_tx(self) -> None:
        payload = MatchedPayload(
            watcher=_watcher(
                resource="tx",
                resource_id=None,
                sub_resource="finality",
                primitive="tx",
            ),
            condition=TxCondition(finality="finalized"),
            observed=ObservedTx(
                tx_hash="0xdeadbeef",
                finality="finalized",
                block=700,
                block_hash="0xabc",
                timestamp=_ts(),
            ),
            budget=_budget(),
            process=_process(),
        )
        assert render_human(payload) == ("matched: tx 0xdeadbeef reached finalized at block 700")

    def test_ethereum_tx_includes_execution_and_gas_context(self) -> None:
        payload = MatchedPayload(
            watcher=_watcher(
                chain="eth",
                resource="tx",
                resource_id=None,
                sub_resource="finality",
                primitive="tx",
            ),
            condition=TxCondition(finality="included", confirmations=2),
            observed=ObservedTx(
                tx_hash="0xdeadbeef",
                finality="included",
                confirmations=2,
                execution_status="reverted",
                gas_used=50_000,
                effective_gas_price_wei=1_500_000_000,
                block=700,
                block_hash="0xabc",
                timestamp=_ts(),
            ),
            budget=_budget(),
            process=_process(),
        )
        assert render_human(payload) == (
            "matched: tx 0xdeadbeef reached included at block 700 "
            "(2 confirmations, reverted, gas 50000 at 1500000000 wei)"
        )


# ---------------------------------------------------------------------------
# Timeout / budget exhausted
# ---------------------------------------------------------------------------


class TestTimeoutRendering:
    def test_runtime_seconds(self) -> None:
        payload = TimeoutPayload(
            watcher=_watcher(),
            condition=ThresholdCondition(operator="below", target=0.05),
            reason="max_runtime_reached",
            budget=_budget(runtime_ms=30_000),
            process=_process(),
        )
        rendered = render_human(payload)
        assert "timeout after 30s" in rendered
        assert "max runtime reached" in rendered
        assert "subnet 19 pool.price" in rendered

    def test_runtime_minutes(self) -> None:
        payload = TimeoutPayload(
            watcher=_watcher(),
            condition=ThresholdCondition(operator="below", target=0.05),
            reason="max_runtime_reached",
            budget=_budget(runtime_ms=120_000),
            process=_process(),
        )
        assert "timeout after 2m" in render_human(payload)

    def test_runtime_hours(self) -> None:
        payload = TimeoutPayload(
            watcher=_watcher(),
            condition=ThresholdCondition(operator="below", target=0.05),
            reason="max_runtime_reached",
            budget=_budget(runtime_ms=7_200_000),
            process=_process(),
        )
        assert "timeout after 2h" in render_human(payload)


class TestBudgetExhaustedRendering:
    def test_basic(self) -> None:
        payload = BudgetExhaustedPayload(
            watcher=_watcher(),
            condition=ThresholdCondition(operator="below", target=0.05),
            reason="max_ru_reached",
            budget=_budget(ru=10_000),
            process=_process(),
        )
        assert render_human(payload) == "budget exhausted: max_ru_reached after 10000 RU consumed"


# ---------------------------------------------------------------------------
# Error payloads
# ---------------------------------------------------------------------------


class TestUserErrorRendering:
    def test_basic(self) -> None:
        payload = UserErrorPayload(
            watcher=None,
            condition=None,
            budget=_budget(),
            process=_process(),
            message="unknown observable 'subnet.999.foo'",
            reason="unknown_observable",
        )
        assert render_human(payload) == "error: unknown observable 'subnet.999.foo'"


class TestProviderErrorRendering:
    def test_rpc_unreachable_appends_hint(self) -> None:
        payload = ProviderErrorPayload(
            watcher=None,
            condition=None,
            budget=_budget(),
            process=_process(),
            message="ConnectionError: refused",
            reason="rpc_unreachable",
        )
        rendered = render_human(payload)
        assert rendered.startswith("error: ConnectionError: refused")
        assert "Check your --rpc-url" in rendered

    def test_decode_failed_no_hint(self) -> None:
        payload = ProviderErrorPayload(
            watcher=None,
            condition=None,
            budget=_budget(),
            process=_process(),
            message="decode failed",
            reason="decode_failed",
        )
        assert render_human(payload) == "error: decode failed"


class TestAuthErrorRendering:
    def test_with_env_vars_and_docs_url(self) -> None:
        payload = AuthErrorPayload(
            watcher=None,
            condition=None,
            budget=_budget(),
            process=_process(),
            message="Authentication failed for chain 'bt'.",
            reason="auth_failed",
            api_key_env_vars=["CHAINWAKE_BT_API_KEY", "CHAINWAKE_API_KEY"],
            docs_url="https://blockmachine.io",
        )
        rendered = render_human(payload)
        assert "error: Authentication failed for chain 'bt'." in rendered
        assert "Set --api-key, or one of: CHAINWAKE_BT_API_KEY, CHAINWAKE_API_KEY" in rendered
        assert "See https://blockmachine.io for an API key." in rendered

    def test_without_env_vars(self) -> None:
        payload = AuthErrorPayload(
            watcher=None,
            condition=None,
            budget=_budget(),
            process=_process(),
            message="bad creds",
            reason="invalid_api_key",
        )
        assert render_human(payload) == "error: bad creds"


class TestInternalErrorRendering:
    def test_includes_bug_report_hint(self) -> None:
        payload = InternalErrorPayload(
            watcher=None,
            condition=None,
            budget=_budget(),
            process=_process(),
            message="ValueError: nope",
            reason="ValueError",
        )
        rendered = render_human(payload)
        assert rendered.startswith("error (internal): ValueError: nope")
        assert "bug" in rendered.lower()


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_payload_same_string(self) -> None:
        def make() -> MatchedPayload:
            return MatchedPayload(
                watcher=_watcher(),
                condition=ThresholdCondition(operator="below", target=0.05),
                observed=ObservedThreshold(
                    path="subnet.19.pool.price",
                    value=0.04,
                    block=100,
                    block_hash="0xabc",
                    timestamp=_ts(),
                ),
                budget=_budget(),
                process=_process(),
            )

        assert render_human(make()) == render_human(make())

    def test_no_timestamp_in_human_output(self) -> None:
        payload = MatchedPayload(
            watcher=_watcher(),
            condition=ThresholdCondition(operator="below", target=0.05),
            observed=ObservedThreshold(
                path="subnet.19.pool.price",
                value=0.04,
                block=100,
                block_hash="0xabc",
                timestamp=_ts(),
            ),
            budget=_budget(),
            process=_process(),
        )
        rendered = render_human(payload)
        assert "2026" not in rendered
        assert "12:00" not in rendered
