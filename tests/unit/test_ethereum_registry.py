"""Ethereum backend and observable catalogue contract tests."""

from __future__ import annotations

import pytest

from chainwake.chains import backend_for
from chainwake.core.registry import ObservationDriver, lookup
from chainwake.core.runtime import WatcherSpec, _format_ru_banner
from chainwake.output.schema import ThresholdCondition
from chainwake.providers.base import Cadence

pytestmark = pytest.mark.unit


def test_builtin_ethereum_backend_owns_runtime_profile() -> None:
    backend = backend_for("eth")

    assert backend.alias == "eth"
    assert backend.runtime.block_seconds == 12.0
    assert backend.runtime.epoch_state_read_cost == 0
    assert backend.runtime.event_block_read_cost == 1
    assert backend.runtime.event_legacy_block_read_cost == 1

    provider = backend.create_provider()
    assert provider.name == "ethereum"
    assert provider.short_alias == "eth"


def test_ethereum_base_fee_is_a_subscribed_per_block_numeric_observable() -> None:
    entry = lookup("network.base-fee", chain="eth")

    assert entry.chain == "eth"
    assert entry.resource == "network"
    assert entry.type == "numeric"
    assert entry.natural_cadence == Cadence.PER_BLOCK
    assert entry.subscription_supported is True
    assert entry.applicable_primitives == ("threshold", "delta")
    assert entry.read_cost == 1
    assert "gwei" in entry.description.lower()

    policy = entry.observation_policy
    assert policy.driver_for("threshold") == ObservationDriver.BEST_HEAD
    assert policy.driver_for("delta", window_unit="ever") == ObservationDriver.BEST_HEAD
    assert policy.fallback_driver == ObservationDriver.TIMER_POLL


def test_ethereum_base_fee_has_no_substrate_storage_binding() -> None:
    policy = lookup("network.base-fee", chain="eth").observation_policy

    assert policy.storage_binding is None
    assert policy.storage_bindings == ()


def test_ethereum_transaction_status_uses_chain_neutral_tx_driver() -> None:
    entry = lookup("tx.{tx_hash}", chain="eth")

    assert entry.chain == "eth"
    assert entry.resource == "tx"
    assert entry.type == "tx-status"
    assert entry.subscription_supported is True
    assert entry.applicable_primitives == ("tx",)
    assert entry.observation_policy.driver_for("tx") == ObservationDriver.TX_STATUS


def test_ethereum_head_subscription_banner_does_not_claim_substrate_protocols() -> None:
    spec = WatcherSpec(
        chain="eth",
        resource="network",
        path_params={},
        sub_resource="base-fee",
        primitive_name="threshold",
        condition=ThresholdCondition(operator="below", target=10),
        invocation=["chainwake", "eth", "network", "base-fee", "--below", "10"],
    )

    banner = _format_ru_banner(
        spec,
        Cadence.PER_BLOCK,
        head_subscription=True,
        runtime=backend_for("eth").runtime,
    )

    assert "subscribed new heads" in banner
    assert "chainHead" not in banner
    assert "unpin" not in banner
    assert "legacy fallback" not in banner
