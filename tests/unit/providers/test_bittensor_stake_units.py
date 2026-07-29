"""Regression tests for Dynamic TAO stake and dividend units."""

from __future__ import annotations

from typing import cast

import pytest
from async_substrate_interface import AsyncSubstrateInterface

from chainwake.core.registry import all_entries, lookup
from chainwake.providers.bittensor import RAO_PER_ALPHA, BittensorProvider, _path_template

pytestmark = pytest.mark.unit


class _ScaleValue:
    def __init__(self, value: int) -> None:
        self.value = value


class _StakeUnitSubstrate:
    """Return distinct subnet-token values and retain the exact pinned read."""

    def __init__(self, values: dict[tuple[str, tuple[object, ...]], int]) -> None:
        self.values = values
        self.calls: list[tuple[str, str, list[object], str]] = []

    async def query(
        self,
        module: str,
        storage_fn: str,
        params: list[object] | None = None,
        block_hash: str = "",
    ) -> _ScaleValue:
        read_params = list(params or [])
        self.calls.append((module, storage_fn, read_params, block_hash))
        return _ScaleValue(self.values[(storage_fn, tuple(read_params))])


@pytest.mark.asyncio
async def test_validator_dividends_are_one_subnets_alpha_not_cross_token_sum() -> None:
    """Alpha from two subnet currencies must never be summed and labelled TAO."""
    provider = BittensorProvider()
    substrate = _StakeUnitSubstrate(
        {("AlphaDividendsPerSubnet", (19, "5Fvalidator")): 3 * RAO_PER_ALPHA}
    )
    provider._substrate = cast(AsyncSubstrateInterface, substrate)

    value = await provider._read_validator_dividends(19, "5Fvalidator", "0xpinned")

    assert value == 3.0
    assert substrate.calls == [
        (
            "SubtensorModule",
            "AlphaDividendsPerSubnet",
            [19, "5Fvalidator"],
            "0xpinned",
        )
    ]


@pytest.mark.asyncio
async def test_validator_stake_is_alpha_for_requested_subnet_not_hardcoded_subnet_one() -> None:
    provider = BittensorProvider()
    substrate = _StakeUnitSubstrate({("TotalHotkeyAlpha", ("5Fvalidator", 19)): 7 * RAO_PER_ALPHA})
    provider._substrate = cast(AsyncSubstrateInterface, substrate)

    value = await provider._read_validator_stake(19, "5Fvalidator", "0xpinned")

    assert value == 7.0
    assert substrate.calls == [
        ("SubtensorModule", "TotalHotkeyAlpha", ["5Fvalidator", 19], "0xpinned")
    ]


def test_registry_exposes_per_subnet_alpha_units_only() -> None:
    paths = {entry.path_template for entry in all_entries()}

    assert "validator.{netuid}.{hotkey}.dividends-alpha" in paths
    assert "validator.{netuid}.{hotkey}.stake-alpha" in paths
    assert "neuron.{netuid}.{hotkey}.stake-alpha" in paths
    assert "validator.{hotkey}.dividends" not in paths
    assert "validator.{hotkey}.stake" not in paths
    assert "neuron.{netuid}.{hotkey}.stake" not in paths

    for path in (
        "validator.{netuid}.{hotkey}.dividends-alpha",
        "validator.{netuid}.{hotkey}.stake-alpha",
        "neuron.{netuid}.{hotkey}.stake-alpha",
    ):
        entry = lookup(path)
        assert "alpha" in entry.description.lower()
        assert "TAO" not in entry.description


def test_path_template_recognises_truthful_validator_and_neuron_alpha_paths() -> None:
    assert (
        _path_template("validator.19.5Fvalidator.dividends-alpha")
        == "validator.{netuid}.{hotkey}.dividends-alpha"
    )
    assert (
        _path_template("validator.19.5Fvalidator.stake-alpha")
        == "validator.{netuid}.{hotkey}.stake-alpha"
    )
    assert (
        _path_template("neuron.19.5Fvalidator.stake-alpha")
        == "neuron.{netuid}.{hotkey}.stake-alpha"
    )
