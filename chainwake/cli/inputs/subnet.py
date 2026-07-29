"""Input models for ``chainwake bt subnet *`` commands."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from chainwake.cli.inputs.common import (
    AboveCondition,
    BelowCondition,
    ChangesFromCondition,
    ChangesToCondition,
    DropPctCondition,
    MovePctCondition,
    Netuid,
    OnChangeCondition,
    PositiveMaxRuntime,
    RisePctCondition,
)

# Threshold-or-delta union for numeric observables.
PriceCondition = Annotated[
    BelowCondition | AboveCondition | DropPctCondition | RisePctCondition | MovePctCondition,
    Field(discriminator="kind", description="Threshold or percentage-move condition."),
]

ThresholdConditionVariant = Annotated[
    BelowCondition | AboveCondition,
    Field(discriminator="kind", description="Threshold condition (below or above)."),
]

StateOrThresholdCondition = Annotated[
    OnChangeCondition | ChangesToCondition | ChangesFromCondition | BelowCondition | AboveCondition,
    Field(discriminator="kind", description="State-transition or threshold condition."),
]


class SubnetPriceInput(BaseModel):
    """Input for ``bt subnet <netuid> price [condition flags]``.

    Validates that exactly one condition variant is expressed. Delta windows
    are optional; omission uses the first successful observation as baseline.
    """

    netuid: Netuid = Field(description="Subnet netuid (0-65535).")
    condition: PriceCondition = Field(description="Threshold or delta condition.")

    rpc_url: str | None = Field(
        None, description="Override RPC endpoint URL (env: CHAINWAKE_BT_RPC_URL)."
    )
    out: list[str] = Field(
        default_factory=list,
        description="Output adapter URI (repeatable). Default: JSON to stdout.",
    )
    name: str | None = Field(None, description="Human-readable watcher label.")
    max_runtime: PositiveMaxRuntime | None = Field(
        None,
        description=(
            "Hard upper-bound on watcher lifetime (e.g. '10m', '2h', '1d'). Default: unbounded."
        ),
    )
    max_ru: int | None = Field(
        None,
        ge=0,
        description=(
            "Registry-estimated observation budget; not a provider billing cap."
            " Excludes connection bootstrap, retries, and hidden SDK RPCs."
            " Default: unbounded."
        ),
    )


class SubnetRegistrationCostInput(BaseModel):
    """Input for ``bt subnet <netuid> registration-cost [condition flags]``.

    Threshold-only: exactly one of --below or --above is required.
    """

    netuid: Netuid = Field(description="Subnet netuid.")
    condition: ThresholdConditionVariant = Field(description="Threshold condition.")

    rpc_url: str | None = Field(None, description="Override RPC endpoint URL.")
    out: list[str] = Field(default_factory=list, description="Output adapter URIs.")
    name: str | None = Field(None, description="Human-readable watcher label.")
    max_runtime: PositiveMaxRuntime | None = Field(
        None,
        description="Hard upper-bound on watcher lifetime. Default: unbounded.",
    )
    max_ru: int | None = Field(
        None,
        ge=0,
        description=(
            "Registry-estimated observation budget; not a provider billing cap."
            " Excludes connection bootstrap, retries, and hidden SDK RPCs."
            " Default: unbounded."
        ),
    )


class SubnetPoolDepthInput(BaseModel):
    """Input for ``bt subnet <netuid> pool tao-depth|alpha-depth [condition flags]``."""

    netuid: Netuid = Field(description="Subnet netuid.")
    condition: PriceCondition = Field(description="Threshold or delta condition.")
    rpc_url: str | None = Field(None, description="Override RPC endpoint URL.")
    out: list[str] = Field(default_factory=list, description="Output adapter URIs.")
    name: str | None = Field(None, description="Human-readable watcher label.")
    max_runtime: PositiveMaxRuntime | None = Field(
        None,
        description="Hard upper-bound on watcher lifetime. Default: unbounded.",
    )
    max_ru: int | None = Field(
        None,
        ge=0,
        description=(
            "Registry-estimated observation budget; not a provider billing cap."
            " Excludes connection bootstrap, retries, and hidden SDK RPCs."
            " Default: unbounded."
        ),
    )


class SubnetDepthForTradeInput(BaseModel):
    """Input for ``bt subnet <netuid> pool depth-for-trade --size --max-bps``.

    Computed observable: returns slippage margin (bps) for a TAO-in trade
    of ``--size`` TAO within ``--max-bps`` slippage budget. Threshold-only.
    """

    netuid: Netuid = Field(description="Subnet netuid.")
    size: float = Field(
        gt=0,
        allow_inf_nan=False,
        description="Finite, positive trade size in TAO.",
    )
    max_bps: float = Field(
        gt=0,
        allow_inf_nan=False,
        description="Finite, positive slippage budget in basis points.",
    )
    condition: ThresholdConditionVariant = Field(description="Threshold condition.")
    rpc_url: str | None = Field(None, description="Override RPC endpoint URL.")
    out: list[str] = Field(default_factory=list, description="Output adapter URIs.")
    name: str | None = Field(None, description="Human-readable watcher label.")
    max_runtime: PositiveMaxRuntime | None = Field(
        None,
        description="Hard upper-bound on watcher lifetime. Default: unbounded.",
    )
    max_ru: int | None = Field(
        None,
        ge=0,
        description=(
            "Registry-estimated observation budget; not a provider billing cap."
            " Excludes connection bootstrap, retries, and hidden SDK RPCs."
            " Default: unbounded."
        ),
    )


class SubnetEmissionShareInput(BaseModel):
    """Input for ``bt subnet <netuid> emission-share [condition flags]``."""

    netuid: Netuid = Field(description="Subnet netuid.")
    condition: PriceCondition = Field(description="Threshold or delta condition.")
    rpc_url: str | None = Field(None, description="Override RPC endpoint URL.")
    out: list[str] = Field(default_factory=list, description="Output adapter URIs.")
    name: str | None = Field(None, description="Human-readable watcher label.")
    max_runtime: PositiveMaxRuntime | None = Field(
        None,
        description="Hard upper-bound on watcher lifetime. Default: unbounded.",
    )
    max_ru: int | None = Field(
        None,
        ge=0,
        description=(
            "Registry-estimated observation budget; not a provider billing cap."
            " Excludes connection bootstrap, retries, and hidden SDK RPCs."
            " Default: unbounded."
        ),
    )


class SubnetBurnRateInput(BaseModel):
    """Input for ``bt subnet <netuid> burn-rate [condition flags]``."""

    netuid: Netuid = Field(description="Subnet netuid.")
    condition: PriceCondition = Field(description="Threshold or delta condition.")
    rpc_url: str | None = Field(None, description="Override RPC endpoint URL.")
    out: list[str] = Field(default_factory=list, description="Output adapter URIs.")
    name: str | None = Field(None, description="Human-readable watcher label.")
    max_runtime: PositiveMaxRuntime | None = Field(
        None,
        description="Hard upper-bound on watcher lifetime. Default: unbounded.",
    )
    max_ru: int | None = Field(
        None,
        ge=0,
        description=(
            "Registry-estimated observation budget; not a provider billing cap."
            " Excludes connection bootstrap, retries, and hidden SDK RPCs."
            " Default: unbounded."
        ),
    )


class SubnetHyperparamsInput(BaseModel):
    """Input for ``bt subnet <netuid> hyperparams [state flags]``."""

    netuid: Netuid = Field(description="Subnet netuid.")
    rpc_url: str | None = Field(None, description="Override RPC endpoint URL.")
    out: list[str] = Field(default_factory=list, description="Output adapter URIs.")
    name: str | None = Field(None, description="Human-readable watcher label.")
    max_runtime: PositiveMaxRuntime | None = Field(
        None,
        description="Hard upper-bound on watcher lifetime. Default: unbounded.",
    )
    max_ru: int | None = Field(
        None,
        ge=0,
        description=(
            "Registry-estimated observation budget; not a provider billing cap."
            " Excludes connection bootstrap, retries, and hidden SDK RPCs."
            " Default: unbounded."
        ),
    )


class SubnetIdentityInput(BaseModel):
    """Input for ``bt subnet <netuid> identity [state flags]``."""

    netuid: Netuid = Field(description="Subnet netuid.")
    condition: OnChangeCondition = Field(
        description="Fire on any transition of the structured identity record."
    )
    rpc_url: str | None = Field(None, description="Override RPC endpoint URL.")
    out: list[str] = Field(default_factory=list, description="Output adapter URIs.")
    name: str | None = Field(None, description="Human-readable watcher label.")
    max_runtime: PositiveMaxRuntime | None = Field(
        None,
        description="Hard upper-bound on watcher lifetime. Default: unbounded.",
    )
    max_ru: int | None = Field(
        None,
        ge=0,
        description=(
            "Registry-estimated observation budget; not a provider billing cap."
            " Excludes connection bootstrap, retries, and hidden SDK RPCs."
            " Default: unbounded."
        ),
    )


__all__ = [
    "PriceCondition",
    "StateOrThresholdCondition",
    "SubnetBurnRateInput",
    "SubnetDepthForTradeInput",
    "SubnetEmissionShareInput",
    "SubnetHyperparamsInput",
    "SubnetIdentityInput",
    "SubnetPoolDepthInput",
    "SubnetPriceInput",
    "SubnetRegistrationCostInput",
    "ThresholdConditionVariant",
]
