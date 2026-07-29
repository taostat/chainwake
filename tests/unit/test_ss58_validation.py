"""Canonical Bittensor SS58 validation across agent and provider boundaries."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import BaseModel, ValidationError

from chainwake.cli.inputs.account import AccountActivityInput, AccountBalanceInput
from chainwake.cli.inputs.event import EventInput
from chainwake.cli.inputs.neuron import (
    NeuronDividendsInput,
    NeuronImmunityBlocksInput,
    NeuronIncentiveInput,
    NeuronLastUpdateInput,
    NeuronStakeInput,
)
from chainwake.cli.inputs.validator import (
    ValidatorChildKeysInput,
    ValidatorCommissionInput,
    ValidatorDividendsInput,
    ValidatorIdentityInput,
    ValidatorStakeInput,
    ValidatorWeightsInput,
)
from chainwake.core.errors import UserError
from chainwake.providers.base import EventFilter
from chainwake.providers.bittensor import BittensorProvider

pytestmark = pytest.mark.unit

ALICE_SS58 = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
BOB_SS58 = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
BAD_CHECKSUM_SS58 = f"{ALICE_SS58[:-1]}Z"
OTHER_NETWORK_SS58 = "16ZqKq7hAVVZJBCUzqgdGywLLnxa3TBdeYCKRRQSEt9kYsd"

_BELOW = {"kind": "below", "value": 1}
_ON_CHANGE = {"kind": "on-change"}


def _address_model_cases() -> tuple[tuple[type[BaseModel], dict[str, object], str], ...]:
    return (
        (AccountBalanceInput, {"condition": _BELOW}, "coldkey"),
        (AccountActivityInput, {"silent_for": "1h"}, "coldkey"),
        (NeuronLastUpdateInput, {"netuid": 1, "silent_for": "1h"}, "hotkey"),
        (NeuronIncentiveInput, {"netuid": 1, "condition": _BELOW}, "hotkey"),
        (NeuronDividendsInput, {"netuid": 1, "condition": _BELOW}, "hotkey"),
        (NeuronStakeInput, {"netuid": 1, "condition": _BELOW}, "hotkey"),
        (NeuronImmunityBlocksInput, {"netuid": 1, "condition": _BELOW}, "hotkey"),
        (ValidatorWeightsInput, {"silent_for": "1h"}, "hotkey"),
        (ValidatorCommissionInput, {"condition": _ON_CHANGE}, "hotkey"),
        (ValidatorDividendsInput, {"netuid": 1, "condition": _BELOW}, "hotkey"),
        (ValidatorStakeInput, {"netuid": 1, "condition": _BELOW}, "hotkey"),
        (ValidatorChildKeysInput, {}, "hotkey"),
        (ValidatorIdentityInput, {"condition": _ON_CHANGE}, "hotkey"),
    )


@pytest.mark.parametrize(("model", "kwargs", "field"), _address_model_cases())
@pytest.mark.parametrize(
    "malformed",
    [
        "5Fxxx",
        BAD_CHECKSUM_SS58,
        OTHER_NETWORK_SS58,
        "0x" + "ab" * 32,
    ],
)
def test_every_hotkey_and_coldkey_model_rejects_non_bittensor_ss58(
    model: type[BaseModel],
    kwargs: dict[str, object],
    field: str,
    malformed: str,
) -> None:
    with pytest.raises(ValidationError):
        model(**kwargs, **{field: malformed})


@pytest.mark.parametrize(("model", "kwargs", "field"), _address_model_cases())
def test_every_hotkey_and_coldkey_model_accepts_canonical_dev_address(
    model: type[BaseModel],
    kwargs: dict[str, object],
    field: str,
) -> None:
    instance = model(**kwargs, **{field: ALICE_SS58})
    assert getattr(instance, field) == ALICE_SS58


@pytest.mark.parametrize("field", ["from_addr", "to_addr"])
def test_event_model_validates_each_ss58_filter(field: str) -> None:
    malformed_kwargs = (
        {"from_addr": BAD_CHECKSUM_SS58} if field == "from_addr" else {"to_addr": BAD_CHECKSUM_SS58}
    )
    with pytest.raises(ValidationError):
        EventInput.model_validate({"event_type": "transfer", **malformed_kwargs})

    valid_kwargs = {"from_addr": BOB_SS58} if field == "from_addr" else {"to_addr": BOB_SS58}
    instance = EventInput.model_validate({"event_type": "transfer", **valid_kwargs})
    assert getattr(instance, field) == BOB_SS58


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (AccountBalanceInput, "coldkey"),
        (ValidatorCommissionInput, "hotkey"),
        (EventInput, "from_addr"),
        (EventInput, "to_addr"),
    ],
)
def test_address_json_schemas_help_agents_reject_bad_shapes(
    model: type[BaseModel],
    field: str,
) -> None:
    schema = model.model_json_schema()["properties"][field]
    if "anyOf" in schema:
        schema = next(item for item in schema["anyOf"] if item.get("type") == "string")
    assert schema["minLength"] == 48
    assert schema["maxLength"] == 48
    assert schema["pattern"].startswith("^5")
    assert "checksum" in schema["description"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "account.5Fxxx.balance",
        "validator.5Fxxx.commission",
        "validator.1.5Fxxx.stake-alpha",
        "neuron.1.5Fxxx.incentive",
    ],
)
async def test_provider_rejects_bad_path_address_before_any_rpc(path: str) -> None:
    provider = BittensorProvider()

    with pytest.raises(UserError, match="Bittensor SS58"):
        await provider.read_observable(path, {})


@pytest.mark.parametrize(
    "build_filter",
    [
        lambda: EventFilter(event_types=("transfer",), args_match={"from": "5Fxxx"}),
        lambda: EventFilter(event_types=("transfer",), args_match={"to": "5Fxxx"}),
        lambda: EventFilter(
            event_types=("transfer",),
            direction="in",
            direction_address="5Fxxx",
        ),
    ],
)
def test_provider_rejects_bad_event_address_before_subscription(
    build_filter: Callable[[], EventFilter],
) -> None:
    provider = BittensorProvider()

    with pytest.raises(UserError, match="Bittensor SS58"):
        provider.subscribe_events(build_filter())
