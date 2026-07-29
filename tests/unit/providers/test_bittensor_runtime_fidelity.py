"""Regression tests for Subtensor spec-440 runtime semantics."""

from __future__ import annotations

from typing import cast

import pytest
from async_substrate_interface import AsyncSubstrateInterface

from chainwake.core.errors import DecodeError
from chainwake.providers.bittensor import BittensorProvider

pytestmark = pytest.mark.unit


class _ScaleType:
    def __init__(self, value: object) -> None:
        self.value = value


class _CurrentHyperparamsSubstrate:
    """Spec-440 hyperparameters expose a tempo-relative activity factor."""

    async def create_storage_key(
        self, module: str, storage_fn: str, params: list[object]
    ) -> tuple[str, str, list[object]]:
        assert module == "SubtensorModule"
        assert params == [1]
        assert storage_fn != "ActivityCutoff", "deprecated absolute cutoff must not be read"
        return module, storage_fn, params

    async def query_multi(
        self,
        storage_keys: list[tuple[str, str, list[object]]],
        block_hash: str = "",
    ) -> list[tuple[object, _ScaleType]]:
        assert block_hash == "0x440"
        values = {
            "Tempo": 99,
            "ImmunityPeriod": 7_200,
            "MinAllowedWeights": 1,
            "MaxWeightsLimit": 65_535,
            "MaxAllowedValidators": 64,
            "MaxAllowedUids": 4_096,
            "ActivityCutoffFactorMilli": 50_000,
            "AdjustmentInterval": 112,
            "WeightsVersionKey": 0,
            "WeightsSetRateLimit": 100,
            "Kappa": 32_767,
            "Rho": 10,
        }
        return [(key, _ScaleType(values[key[1]])) for key in storage_keys]


@pytest.mark.asyncio
async def test_hyperparams_compute_effective_activity_cutoff_from_factor_and_tempo() -> None:
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, _CurrentHyperparamsSubstrate())

    result = await provider._read_subnet_hyperparams(1, "0x440")

    assert result["activity_cutoff_factor_milli"] == 50_000
    assert result["activity_cutoff"] == 4_950


class _CurrentDynamicIdentitySubstrate:
    async def runtime_call(
        self,
        api: str,
        method: str,
        params: list[object],
        block_hash: str = "",
    ) -> dict[str, object]:
        assert (api, method, params, block_hash) == (
            "SubnetInfoRuntimeApi",
            "get_dynamic_info",
            [19],
            "0x440",
        )
        return {
            "netuid": 19,
            "owner_hotkey": "5Hot",
            "owner_coldkey": "5Cold",
            "subnet_identity": {
                "subnet_name": "blockmachine",
                "github_repo": "https://github.com/taostat/blockmachine/",
                "subnet_contact": "team@blockmachine.io",
                "subnet_url": "blockmachine.io",
                "discord": "",
                "description": "Infrastructure subnet",
                "logo_url": "https://blockmachine.io/logo.svg",
                "additional": "",
            },
        }


@pytest.mark.asyncio
async def test_subnet_identity_uses_full_dynamic_info_identity_shape() -> None:
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, _CurrentDynamicIdentitySubstrate())

    result = await provider._read_subnet_identity(19, "0x440")

    assert result == {
        "netuid": 19,
        "owner_hotkey": "5Hot",
        "owner_coldkey": "5Cold",
        "subnet_identity": {
            "subnet_name": "blockmachine",
            "github_repo": "https://github.com/taostat/blockmachine/",
            "subnet_contact": "team@blockmachine.io",
            "subnet_url": "blockmachine.io",
            "discord": "",
            "description": "Infrastructure subnet",
            "logo_url": "https://blockmachine.io/logo.svg",
            "additional": "",
        },
    }


class _MechanismSubstrate:
    def __init__(self, mechanism_count: int = 2) -> None:
        self.mechanism_count = mechanism_count
        self.queries: list[tuple[str, list[object], str]] = []

    async def query(
        self,
        module: str,
        storage_fn: str,
        params: list[object] | None = None,
        block_hash: str = "",
    ) -> _ScaleType:
        assert module == "SubtensorModule"
        query_params = params or []
        self.queries.append((storage_fn, query_params, block_hash))
        values: dict[str, object] = {
            "NetworksAdded": True,
            "MechanismCountCurrent": self.mechanism_count,
            "Uids": 0,
            "Incentive": [32_768],
            "LastUpdate": [8_700_000],
        }
        return _ScaleType(values[storage_fn])


@pytest.mark.asyncio
async def test_neuron_incentive_reads_requested_mechanism_storage_index() -> None:
    substrate = _MechanismSubstrate()
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, substrate)

    result = await provider._read_neuron_incentive(19, "5Hot", "0x440", mechid=1)

    assert result == pytest.approx(32_768 / 65_535)
    assert ("Incentive", [4_115], "0x440") in substrate.queries


@pytest.mark.asyncio
async def test_neuron_last_update_rejects_nonexistent_mechanism_clearly() -> None:
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, _MechanismSubstrate(mechanism_count=1))

    with pytest.raises(DecodeError, match=r"mechanism 1.*subnet 19.*count is 1"):
        await provider._read_neuron_last_update(19, "5Hot", "0x440", mechid=1)


@pytest.mark.asyncio
async def test_dispatch_neuron_forwards_explicit_mechid() -> None:
    substrate = _MechanismSubstrate()
    provider = BittensorProvider()
    provider._substrate = cast(AsyncSubstrateInterface, substrate)

    value = await provider._dispatch_neuron(
        "neuron.{netuid}.{hotkey}.incentive",
        ["neuron", "19", "5Hot", "incentive"],
        {"mechid": 1},
        8_700_001,
        "0x440",
    )

    assert value == pytest.approx(32_768 / 65_535)
    assert ("Incentive", [4_115], "0x440") in substrate.queries
