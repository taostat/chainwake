"""MCP tool definitions for every executable Chainwake wake.

``TOOL_SPECS`` is the coverage contract between three surfaces:

* the canonical CLI command;
* the Pydantic input model advertised to agents;
* the approved observable path(s) in the core registry.

One generic event tool intentionally covers the raw event path and all curated
friendly-event paths. CLI spelling aliases (for example ``burnrate``) are not
duplicated as MCP tools because they execute the same observable.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from mcp.types import Tool
from pydantic import BaseModel

from chainwake.cli.inputs.account import AccountActivityInput, AccountBalanceInput
from chainwake.cli.inputs.event import EventInput
from chainwake.cli.inputs.evm import (
    BaseTxInput,
    BscTxInput,
    EthereumTxInput,
    EvmFeeInput,
    EvmTokenPriceInput,
)
from chainwake.cli.inputs.network import (
    NetworkOnRuntimeUpgradedInput,
    NetworkRuntimeVersionInput,
    NetworkSubnetCountInput,
    NetworkSubnetRegistrationCostInput,
    NetworkTaoPriceInput,
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
from chainwake.core.registry import FRIENDLY_EVENT_MAP
from chainwake.providers.evm import EVM_PROFILES, EvmFeeModel

_CHAIN = "bt"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One MCP wake and the executable/registry surface it covers."""

    chain: str
    slug: str
    command: tuple[str, ...]
    input_model: type[BaseModel]
    registry_paths: tuple[str, ...]
    description: str

    @property
    def name(self) -> str:
        return f"chainwake_{self.chain}_{self.slug}"


def _spec(
    slug: str,
    command: tuple[str, ...],
    input_model: type[BaseModel],
    registry_path: str,
    description: str,
) -> ToolSpec:
    return ToolSpec(_CHAIN, slug, command, input_model, (registry_path,), description)


_NUMERIC = "Wait for its threshold or percentage-move condition to match."
_STATE = "Wait for its requested on-chain state transition to match."

TOOL_SPECS: tuple[ToolSpec, ...] = (
    _spec(
        "subnet_price",
        ("subnet", "price"),
        SubnetPriceInput,
        "subnet.{netuid}.pool.price",
        f"Watch a subnet's alpha price in TAO. {_NUMERIC}",
    ),
    _spec(
        "subnet_tao_depth",
        ("subnet", "tao-depth"),
        SubnetPoolDepthInput,
        "subnet.{netuid}.pool.tao-depth",
        f"Watch a subnet pool's TAO reserve depth. {_NUMERIC}",
    ),
    _spec(
        "subnet_alpha_depth",
        ("subnet", "alpha-depth"),
        SubnetPoolDepthInput,
        "subnet.{netuid}.pool.alpha-depth",
        f"Watch a subnet pool's alpha reserve depth. {_NUMERIC}",
    ),
    _spec(
        "subnet_depth_for_trade",
        ("subnet", "depth-for-trade"),
        SubnetDepthForTradeInput,
        "subnet.{netuid}.pool.depth-for-trade",
        "Watch the slippage margin for a TAO-in trade until a threshold matches.",
    ),
    _spec(
        "subnet_alpha_supply",
        ("subnet", "alpha-supply"),
        SubnetPoolDepthInput,
        "subnet.{netuid}.pool.alpha-supply",
        f"Watch alpha supply outside a subnet pool. {_NUMERIC}",
    ),
    _spec(
        "subnet_moving_price",
        ("subnet", "moving-price"),
        SubnetPoolDepthInput,
        "subnet.{netuid}.pool.moving-price",
        f"Watch a subnet pool's moving price. {_NUMERIC}",
    ),
    _spec(
        "subnet_volume",
        ("subnet", "volume"),
        SubnetPoolDepthInput,
        "subnet.{netuid}.pool.volume",
        f"Watch a subnet pool's cumulative swap volume in TAO. {_NUMERIC}",
    ),
    _spec(
        "subnet_registration_cost",
        ("subnet", "registration-cost"),
        SubnetRegistrationCostInput,
        "subnet.{netuid}.registration-cost",
        "Watch a subnet's registration cost until a threshold matches.",
    ),
    _spec(
        "subnet_emission_share",
        ("subnet", "emission-share"),
        SubnetEmissionShareInput,
        "subnet.{netuid}.emission-share",
        f"Watch the fraction of network emission routed to a subnet. {_NUMERIC}",
    ),
    _spec(
        "subnet_burn_rate",
        ("subnet", "burn-rate"),
        SubnetBurnRateInput,
        "subnet.{netuid}.burn-rate",
        f"Watch a subnet's last-tempo miner-emission burn rate. {_NUMERIC}",
    ),
    _spec(
        "subnet_ema_tao_flow",
        ("subnet", "ema-tao-flow"),
        SubnetPoolDepthInput,
        "subnet.{netuid}.ema-tao-flow",
        f"Watch a subnet's signed EMA TAO flow. {_NUMERIC}",
    ),
    _spec(
        "subnet_hyperparams",
        ("subnet", "hyperparams"),
        SubnetHyperparamsInput,
        "subnet.{netuid}.hyperparams",
        "Wait for any subnet hyperparameter change.",
    ),
    _spec(
        "subnet_identity",
        ("subnet", "identity"),
        SubnetIdentityInput,
        "subnet.{netuid}.identity",
        "Watch a subnet's structured on-chain identity with the on-change operator.",
    ),
    _spec(
        "validator_dividends_alpha",
        ("validator", "dividends-alpha"),
        ValidatorDividendsInput,
        "validator.{netuid}.{hotkey}.dividends-alpha",
        f"Watch subnet-scoped validator dividends denominated in alpha. {_NUMERIC}",
    ),
    _spec(
        "validator_stake_alpha",
        ("validator", "stake-alpha"),
        ValidatorStakeInput,
        "validator.{netuid}.{hotkey}.stake-alpha",
        f"Watch subnet-scoped validator stake denominated in alpha. {_NUMERIC}",
    ),
    _spec(
        "validator_commission",
        ("validator", "commission"),
        ValidatorCommissionInput,
        "validator.{hotkey}.commission",
        (
            "Watch a validator's commission fraction for any change or a transition "
            "to/from a finite value from 0 to 1."
        ),
    ),
    _spec(
        "validator_weights",
        ("validator", "weights"),
        ValidatorWeightsInput,
        "validator.{hotkey}.weights",
        "Wait until a validator has been silent from weight setting for the requested duration.",
    ),
    _spec(
        "validator_child_keys",
        ("validator", "child-keys"),
        ValidatorChildKeysInput,
        "validator.{hotkey}.child-keys",
        "Wait for the validator's child-key state to change.",
    ),
    _spec(
        "validator_identity",
        ("validator", "identity"),
        ValidatorIdentityInput,
        "validator.{hotkey}.identity",
        "Watch a validator's structured on-chain identity with the on-change operator.",
    ),
    _spec(
        "neuron_incentive",
        ("neuron", "incentive"),
        NeuronIncentiveInput,
        "neuron.{netuid}.{hotkey}.incentive",
        f"Watch a neuron's mechanism-indexed incentive score. {_NUMERIC}",
    ),
    _spec(
        "neuron_dividends",
        ("neuron", "dividends"),
        NeuronDividendsInput,
        "neuron.{netuid}.{hotkey}.dividends",
        f"Watch a neuron's dividends. {_NUMERIC}",
    ),
    _spec(
        "neuron_stake_alpha",
        ("neuron", "stake-alpha"),
        NeuronStakeInput,
        "neuron.{netuid}.{hotkey}.stake-alpha",
        f"Watch subnet-scoped neuron stake denominated in alpha. {_NUMERIC}",
    ),
    _spec(
        "neuron_last_update",
        ("neuron", "last-update"),
        NeuronLastUpdateInput,
        "neuron.{netuid}.{hotkey}.last-update",
        "Wait until a neuron's mechanism-indexed last update has been silent long enough.",
    ),
    _spec(
        "neuron_blocks_until_immunity_expires",
        ("neuron", "blocks-until-immunity-expires"),
        NeuronImmunityBlocksInput,
        "neuron.{netuid}.{hotkey}.blocks-until-immunity-expires",
        "Watch the exact number of blocks until a neuron's registration immunity expires.",
    ),
    _spec(
        "account_balance",
        ("account", "balance"),
        AccountBalanceInput,
        "account.{coldkey}.balance",
        "Watch a coldkey's free TAO balance until its threshold, delta, or change matches.",
    ),
    _spec(
        "account_activity",
        ("account", "activity"),
        AccountActivityInput,
        "account.{coldkey}.activity",
        "Wait until a coldkey account has had no activity for the requested duration.",
    ),
    _spec(
        "network_subnet_registration_cost",
        ("network", "subnet-registration-cost"),
        NetworkSubnetRegistrationCostInput,
        "network.subnet-registration-cost",
        "Watch the chain-wide subnet registration cost until a threshold matches.",
    ),
    _spec(
        "network_tao_price",
        ("network", "tao-price"),
        NetworkTaoPriceInput,
        "network.tao-price",
        f"Watch TAO's aggregate USD price. {_NUMERIC}",
    ),
    _spec(
        "network_runtime_version",
        ("network", "runtime-version"),
        NetworkRuntimeVersionInput,
        "network.runtime-version",
        "Wait for the Bittensor runtime spec or implementation version to change.",
    ),
    _spec(
        "network_subnet_count",
        ("network", "subnet-count"),
        NetworkSubnetCountInput,
        "network.subnet-count",
        f"Watch the number of registered subnets. {_NUMERIC}",
    ),
    _spec(
        "network_on_runtime_upgraded",
        ("network", "on-runtime-upgraded"),
        NetworkOnRuntimeUpgradedInput,
        "network.--on-runtime-upgraded",
        "Wait for System.CodeUpdated when a runtime upgrade is applied.",
    ),
    ToolSpec(
        chain=_CHAIN,
        slug="event",
        command=("event",),
        input_model=EventInput,
        registry_paths=(
            "event.--type-raw",
            *(f"event.{name}" for name in FRIENDLY_EVENT_MAP),
        ),
        description=(
            "Wait for one Bittensor event matching exactly one curated friendly-name "
            "or raw canonical Module.Event filter."
        ),
    ),
    _spec(
        "tx",
        ("tx",),
        TxInput,
        "tx.{tx_hash}",
        "Wait for a Bittensor transaction to reach included or finalized status.",
    ),
)

_EVM_TX_INPUTS = {
    "eth": EthereumTxInput,
    "base": BaseTxInput,
    "bsc": BscTxInput,
}


def _evm_tool_specs() -> tuple[ToolSpec, ...]:
    specs: list[ToolSpec] = []
    for profile in EVM_PROFILES.values():
        if profile.fee_model in {EvmFeeModel.EIP1559, EvmFeeModel.OP_STACK}:
            specs.append(
                ToolSpec(
                    chain=profile.alias,
                    slug="network_base_fee",
                    command=("network", "base-fee"),
                    input_model=EvmFeeInput,
                    registry_paths=("network.base-fee",),
                    description=(
                        f"Watch {profile.name}'s EIP-1559 execution base fee in gwei. "
                        "Wait for its threshold or percentage-move condition to match."
                    ),
                )
            )
        if profile.fee_model == EvmFeeModel.OP_STACK:
            for command_name, description in (
                (
                    "l1-base-fee",
                    f"Watch the Ethereum L1 base fee observed by {profile.name}.",
                ),
                (
                    "l1-blob-base-fee",
                    f"Watch the Ethereum L1 blob base fee observed by {profile.name}.",
                ),
            ):
                specs.append(
                    ToolSpec(
                        chain=profile.alias,
                        slug=f"network_{command_name.replace('-', '_')}",
                        command=("network", command_name),
                        input_model=EvmFeeInput,
                        registry_paths=(f"network.{command_name}",),
                        description=description,
                    )
                )
        if profile.fee_model == EvmFeeModel.GAS_PRICE:
            specs.append(
                ToolSpec(
                    chain=profile.alias,
                    slug="network_gas_price",
                    command=("network", "gas-price"),
                    input_model=EvmFeeInput,
                    registry_paths=("network.gas-price",),
                    description=(
                        f"Watch {profile.name}'s suggested gas price in gwei. "
                        "Wait for its threshold or percentage-move condition to match."
                    ),
                )
            )
        specs.append(
            ToolSpec(
                chain=profile.alias,
                slug="token_price",
                command=("token", "price"),
                input_model=EvmTokenPriceInput,
                registry_paths=("token.{token}.price",),
                description=(
                    f"Watch a {profile.name} token's aggregate USD price by "
                    "chain-scoped symbol or contract address. "
                    "Wait for its threshold or percentage-move condition to match."
                ),
            )
        )
        finality = ", ".join(profile.supported_finality_levels)
        specs.append(
            ToolSpec(
                chain=profile.alias,
                slug="tx",
                command=("tx",),
                input_model=_EVM_TX_INPUTS[profile.alias],
                registry_paths=("tx.{tx_hash}",),
                description=(
                    f"Wait for a {profile.name} transaction to reach {finality}; "
                    "return execution and gas context."
                ),
            )
        )
    return tuple(specs)


EVM_TOOL_SPECS: tuple[ToolSpec, ...] = _evm_tool_specs()
ETH_TOOL_SPECS: tuple[ToolSpec, ...] = tuple(spec for spec in EVM_TOOL_SPECS if spec.chain == "eth")

_ALL_TOOL_SPECS = (*TOOL_SPECS, *EVM_TOOL_SPECS)
_SPECS_BY_CHAIN_SLUG = {(spec.chain, spec.slug): spec for spec in _ALL_TOOL_SPECS}


def tool_spec_for(slug: str, *, chain: str = _CHAIN) -> ToolSpec | None:
    """Return the executable manifest entry for one tool-name suffix."""
    return _SPECS_BY_CHAIN_SLUG.get((chain, slug))


def _mcp_input_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Return the model schema without CLI-only non-exiting output adapters."""
    schema = deepcopy(model.model_json_schema())
    schema.get("properties", {}).pop("out", None)
    if "required" in schema:
        schema["required"] = [field for field in schema["required"] if field != "out"]
    return schema


def build_tools(chain: str = _CHAIN) -> list[Tool]:
    """Return all executable, exit-oriented MCP wakes for *chain*."""
    specs = (
        _ALL_TOOL_SPECS
        if chain == "all"
        else tuple(spec for spec in _ALL_TOOL_SPECS if spec.chain == chain)
    )
    return [
        Tool(
            name=spec.name,
            description=spec.description,
            inputSchema=_mcp_input_schema(spec.input_model),
        )
        for spec in specs
    ]


__all__ = [
    "ETH_TOOL_SPECS",
    "EVM_TOOL_SPECS",
    "TOOL_SPECS",
    "ToolSpec",
    "build_tools",
    "tool_spec_for",
]
