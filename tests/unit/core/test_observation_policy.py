"""Specification tests for declarative observable scheduling policies."""

from __future__ import annotations

import pytest

import chainwake.providers.bittensor as bittensor_provider
from chainwake.core.registry import lookup
from chainwake.mcp.tools import TOOL_SPECS

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("path", "primitive", "expected_driver"),
    [
        ("subnet.{netuid}.registration-cost", "threshold", "storage_change"),
        ("validator.{hotkey}.commission", "state", "storage_change"),
        ("subnet.{netuid}.pool.price", "threshold", "storage_change"),
        ("subnet.{netuid}.burn-rate", "threshold", "storage_change"),
        ("validator.{hotkey}.weights", "liveness", "subnet_epoch"),
        ("network.--on-runtime-upgraded", "event", "event_stream"),
        ("tx.{tx_hash}", "tx", "tx_status"),
    ],
)
def test_registry_policy_selects_the_observable_driver(
    path: str,
    primitive: str,
    expected_driver: str,
) -> None:
    policy = lookup(path).observation_policy

    assert policy.driver_for(primitive).value == expected_driver


def test_policy_can_select_different_drivers_for_one_observable() -> None:
    policy = lookup("account.{coldkey}.balance").observation_policy

    assert policy.driver_for("threshold").value == "storage_change"
    assert policy.driver_for("state").value == "storage_change"
    assert policy.driver_for("delta").value == "best_head"


def test_computed_price_uses_both_pool_storage_dependencies() -> None:
    policy = lookup("subnet.{netuid}.pool.price").observation_policy

    assert [
        (binding.module, binding.storage_function, binding.path_params)
        for binding in policy.storage_bindings
    ] == [
        ("SubtensorModule", "SubnetTAO", ("netuid",)),
        ("SubtensorModule", "SubnetAlphaIn", ("netuid",)),
    ]


def test_delta_without_explicit_window_uses_storage_changes() -> None:
    price_policy = lookup("subnet.{netuid}.pool.price").observation_policy
    burn_policy = lookup("subnet.{netuid}.burn-rate").observation_policy

    assert price_policy.driver_for("delta", window_unit="ever").value == "storage_change"
    assert burn_policy.driver_for("delta", window_unit="ever").value == "storage_change"


def test_explicit_delta_windows_preserve_natural_scheduling() -> None:
    price_policy = lookup("subnet.{netuid}.pool.price").observation_policy
    burn_policy = lookup("subnet.{netuid}.burn-rate").observation_policy

    assert price_policy.driver_for("delta", window_unit="time").value == "best_head"
    assert price_policy.driver_for("delta", window_unit="blocks").value == "best_head"
    assert price_policy.driver_for("delta", window_unit="epochs").value == "best_head"
    assert burn_policy.driver_for("delta", window_unit="epochs").value == "subnet_epoch"


def test_storage_binding_lives_with_the_registry_policy() -> None:
    policy = lookup("validator.{hotkey}.commission").observation_policy

    assert policy.storage_binding is not None
    assert policy.storage_binding.module == "SubtensorModule"
    assert policy.storage_binding.storage_function == "Delegates"
    assert policy.storage_binding.path_params == ("hotkey",)


def test_burn_rate_subscribes_to_the_value_not_the_epoch_clock() -> None:
    policy = lookup("subnet.{netuid}.burn-rate").observation_policy

    assert policy.driver_for("threshold").value == "storage_change"
    assert policy.storage_bindings[0].storage_function == "MinerBurned"


def test_provider_does_not_own_a_second_cadence_table() -> None:
    assert not hasattr(bittensor_provider, "_PATH_CADENCE")


def test_native_monitoring_tools_do_not_advertise_poll_seconds() -> None:
    tools_with_polling = [
        spec.name for spec in TOOL_SPECS if "poll_seconds" in spec.input_model.model_fields
    ]

    assert tools_with_polling == []


def test_wait_and_budget_controls_remain_available_to_every_tool() -> None:
    for spec in TOOL_SPECS:
        assert "max_runtime" in spec.input_model.model_fields, spec.name
        assert "max_ru" in spec.input_model.model_fields, spec.name
