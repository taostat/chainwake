"""Defaults asserted on chainwake.cli.inputs Pydantic models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from chainwake.cli.inputs.account import AccountActivityInput, AccountBalanceInput
from chainwake.cli.inputs.common import (
    CommissionChangesFromCondition,
    CommissionChangesToCondition,
)
from chainwake.cli.inputs.event import EventInput
from chainwake.cli.inputs.network import (
    NetworkOnRuntimeUpgradedInput,
    NetworkRuntimeVersionInput,
    NetworkSubnetCountInput,
    NetworkSubnetRegistrationCostInput,
)
from chainwake.cli.inputs.neuron import (
    NeuronDividendsInput,
    NeuronImmunityBlocksInput,
    NeuronIncentiveInput,
    NeuronLastUpdateInput,
    NeuronStakeInput,
)
from chainwake.cli.inputs.subnet import (
    SubnetBurnRateInput,
    SubnetEmissionShareInput,
    SubnetHyperparamsInput,
    SubnetIdentityInput,
    SubnetPoolDepthInput,
    SubnetPriceInput,
    SubnetRegistrationCostInput,
)
from chainwake.cli.inputs.tx import TxInput
from chainwake.cli.inputs.validator import (
    ValidatorChildKeysInput,
    ValidatorCommissionInput,
    ValidatorDividendsInput,
    ValidatorIdentityInput,
    ValidatorStakeInput,
    ValidatorWeightsInput,
)
from tests.ss58 import ALICE_SS58

pytestmark = pytest.mark.unit

_BELOW = {"kind": "below", "value": 1.0}
_THRESHOLD = {"kind": "below", "value": 1.0}
_ON_CHANGE = {"kind": "on-change"}

_ACCOUNT = ALICE_SS58
_HOTKEY = ALICE_SS58


_OTHER_MODELS: tuple[tuple[type, dict[str, object]], ...] = (
    (NetworkSubnetRegistrationCostInput, {"condition": _THRESHOLD}),
    (NetworkRuntimeVersionInput, {}),
    (NetworkOnRuntimeUpgradedInput, {}),
    (AccountActivityInput, {"coldkey": _ACCOUNT, "silent_for": "1h"}),
    (NeuronLastUpdateInput, {"netuid": 1, "hotkey": _HOTKEY, "silent_for": "1h"}),
    (NeuronIncentiveInput, {"netuid": 1, "hotkey": _HOTKEY, "condition": _BELOW}),
    (NeuronDividendsInput, {"netuid": 1, "hotkey": _HOTKEY, "condition": _BELOW}),
    (NeuronStakeInput, {"netuid": 1, "hotkey": _HOTKEY, "condition": _BELOW}),
    (
        NeuronImmunityBlocksInput,
        {"netuid": 1, "hotkey": _HOTKEY, "condition": _THRESHOLD},
    ),
    (ValidatorChildKeysInput, {"hotkey": _HOTKEY}),
    (TxInput, {"tx_hash": "0x" + "ab" * 32, "finality": "finalized"}),
    (EventInput, {"event_type": "transfer"}),
)


_POLICY_DRIVEN_MODELS: tuple[tuple[type, dict[str, object]], ...] = (
    (SubnetHyperparamsInput, {"netuid": 1, "condition": _ON_CHANGE}),
    (SubnetPriceInput, {"netuid": 1, "condition": _BELOW}),
    (SubnetRegistrationCostInput, {"netuid": 1, "condition": _THRESHOLD}),
    (SubnetPoolDepthInput, {"netuid": 1, "condition": _BELOW}),
    (SubnetEmissionShareInput, {"netuid": 1, "condition": _BELOW}),
    (SubnetBurnRateInput, {"netuid": 1, "condition": _BELOW}),
    (SubnetIdentityInput, {"netuid": 1, "condition": _ON_CHANGE}),
    (NetworkSubnetCountInput, {"condition": _BELOW}),
    (AccountBalanceInput, {"coldkey": _ACCOUNT, "condition": _BELOW}),
    (ValidatorWeightsInput, {"hotkey": _HOTKEY, "silent_for": "1h"}),
    (ValidatorCommissionInput, {"hotkey": _HOTKEY, "condition": _ON_CHANGE}),
    (ValidatorDividendsInput, {"netuid": 1, "hotkey": _HOTKEY, "condition": _BELOW}),
    (ValidatorStakeInput, {"netuid": 1, "hotkey": _HOTKEY, "condition": _BELOW}),
    (ValidatorIdentityInput, {"hotkey": _HOTKEY, "condition": _ON_CHANGE}),
)

_ALL_MODELS = _OTHER_MODELS + _POLICY_DRIVEN_MODELS


def test_event_input_rejects_unobservable_friendly_events() -> None:
    with pytest.raises(ValidationError):
        EventInput(event_type="neuron-deregistered")  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValidationError):
        EventInput(event_type="hyperparam-changed")  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    ("model", "identity"),
    [
        (SubnetIdentityInput, {"netuid": 1}),
        (ValidatorIdentityInput, {"hotkey": _HOTKEY}),
    ],
)
@pytest.mark.parametrize("kind", ["changes-to", "changes-from"])
def test_structured_identity_inputs_accept_only_on_change(
    model: type, identity: dict[str, object], kind: str
) -> None:
    with pytest.raises(ValidationError):
        model(**identity, condition={"kind": kind, "value": "Alice"})


@pytest.mark.parametrize("kind", ["changes-to", "changes-from"])
def test_commission_input_coerces_finite_fraction_target(kind: str) -> None:
    parsed = ValidatorCommissionInput(
        hotkey=_HOTKEY,
        condition={"kind": kind, "value": "0.18"},
    )
    assert isinstance(
        parsed.condition, CommissionChangesToCondition | CommissionChangesFromCondition
    )
    assert parsed.condition.value == 0.18


@pytest.mark.parametrize("value", ["Alice", "nan", "inf", -0.01, 1.01])
def test_commission_input_rejects_invalid_target(value: object) -> None:
    with pytest.raises(ValidationError):
        ValidatorCommissionInput(
            hotkey=_HOTKEY,
            condition={"kind": "changes-to", "value": value},
        )


def test_event_input_requires_exactly_one_event_type() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        EventInput()
    with pytest.raises(ValidationError, match="exactly one"):
        EventInput(event_type="transfer", type_raw="Balances.Transfer")


@pytest.mark.parametrize(
    "raw",
    [
        "Balances",
        ".Transfer",
        "Balances.",
        "Balances.Transfer.More",
        "Balances transfer",
    ],
)
def test_event_input_rejects_invalid_raw_event_type(raw: str) -> None:
    with pytest.raises(ValidationError, match=r"Module\.Event"):
        EventInput(type_raw=raw)


@pytest.mark.parametrize(("model", "kwargs"), _ALL_MODELS)
def test_max_runtime_defaults_to_none(model: type, kwargs: dict[str, object]) -> None:
    """Bug 1 fix — every input model defaults ``max_runtime`` to ``None``.

    ``None`` signals an unbounded watcher lifetime; the runtime treats it
    identically to ``0`` in ``Budget.is_runtime_exceeded``.
    """
    instance = model(**kwargs)
    assert instance.max_runtime is None


@pytest.mark.parametrize(("model", "kwargs"), _ALL_MODELS)
@pytest.mark.parametrize("max_runtime", ["0", "-1", "nan", "inf", "0s"])
def test_all_input_models_reject_invalid_explicit_max_runtime(
    model: type,
    kwargs: dict[str, object],
    max_runtime: str,
) -> None:
    with pytest.raises(ValidationError):
        model(**kwargs, max_runtime=max_runtime)
