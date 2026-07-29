"""Pydantic input models for every chainwake CLI command.

Each model is the single source of truth for one leaf command's input shape.
The CLI parses flat flags then calls a ``_resolve_*`` helper to construct the
model; the MCP layer calls ``Model.model_json_schema()`` directly.
"""

from __future__ import annotations

from chainwake.cli.inputs.account import AccountActivityInput, AccountBalanceInput
from chainwake.cli.inputs.common import (
    AboveCondition,
    BelowCondition,
    DropPctCondition,
    MovePctCondition,
    OnChangeCondition,
    RisePctCondition,
)
from chainwake.cli.inputs.event import FRIENDLY_EVENT_NAMES, EventInput
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
)
from chainwake.cli.inputs.subnet import (
    SubnetBurnRateInput,
    SubnetDepthForTradeInput,
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

__all__ = [
    "FRIENDLY_EVENT_NAMES",
    "AboveCondition",
    "AccountActivityInput",
    "AccountBalanceInput",
    "BelowCondition",
    "DropPctCondition",
    "EventInput",
    "MovePctCondition",
    "NetworkOnRuntimeUpgradedInput",
    "NetworkRuntimeVersionInput",
    "NetworkSubnetCountInput",
    "NetworkSubnetRegistrationCostInput",
    "NeuronDividendsInput",
    "NeuronImmunityBlocksInput",
    "NeuronIncentiveInput",
    "NeuronLastUpdateInput",
    "OnChangeCondition",
    "RisePctCondition",
    "SubnetBurnRateInput",
    "SubnetDepthForTradeInput",
    "SubnetEmissionShareInput",
    "SubnetHyperparamsInput",
    "SubnetIdentityInput",
    "SubnetPoolDepthInput",
    "SubnetPriceInput",
    "SubnetRegistrationCostInput",
    "TxInput",
    "ValidatorChildKeysInput",
    "ValidatorCommissionInput",
    "ValidatorDividendsInput",
    "ValidatorIdentityInput",
    "ValidatorStakeInput",
    "ValidatorWeightsInput",
]
