"""Input models for ``chainwake bt validator *`` commands."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from chainwake.cli.inputs.common import (
    AboveCondition,
    BelowCondition,
    BittensorSS58Address,
    CommissionChangesFromCondition,
    CommissionChangesToCondition,
    DropPctCondition,
    MechanismNetuid,
    MovePctCondition,
    Netuid,
    OnChangeCondition,
    PositiveDuration,
    PositiveMaxRuntime,
    RisePctCondition,
)

ThresholdOrDeltaCondition = Annotated[
    BelowCondition | AboveCondition | DropPctCondition | RisePctCondition | MovePctCondition,
    Field(discriminator="kind", description="Threshold or percentage-move condition."),
]

ThresholdCondition = Annotated[
    BelowCondition | AboveCondition,
    Field(discriminator="kind", description="Threshold condition."),
]

CommissionStateCondition = Annotated[
    OnChangeCondition | CommissionChangesToCondition | CommissionChangesFromCondition,
    Field(discriminator="kind", description="Commission state-transition condition."),
]


class ValidatorWeightsInput(BaseModel):
    """Input for ``bt validator <hotkey> weights --silent-for <duration>``."""

    hotkey: BittensorSS58Address
    netuid: MechanismNetuid = Field(
        1,
        ge=0,
        le=4095,
        description="Subnet netuid whose weight activity is monitored. Defaults to subnet 1.",
    )
    mechid: int = Field(
        0,
        ge=0,
        le=15,
        description="Subnet mechanism id. Defaults to the main mechanism (0).",
    )
    silent_for: PositiveDuration = Field(
        description=(
            "Fire after no weight-set activity for this duration (e.g. '3epochs', '10m', '2h')."
        )
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


class ValidatorCommissionInput(BaseModel):
    """Input for ``bt validator <hotkey> commission [state flags]``."""

    hotkey: BittensorSS58Address
    condition: CommissionStateCondition = Field(
        description="Any change, or a transition to/from a finite commission fraction."
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


class ValidatorDividendsInput(BaseModel):
    """Input for ``bt validator <hotkey> dividends-alpha --netuid N``."""

    netuid: Netuid = Field(description="Subnet whose alpha-token dividends to watch.")
    hotkey: BittensorSS58Address
    condition: ThresholdOrDeltaCondition = Field(description="Threshold or delta condition.")
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


class ValidatorStakeInput(BaseModel):
    """Input for ``bt validator <hotkey> stake-alpha --netuid N``."""

    netuid: Netuid = Field(description="Subnet whose alpha-token stake to watch.")
    hotkey: BittensorSS58Address
    condition: ThresholdOrDeltaCondition = Field(description="Threshold or delta condition.")
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


class ValidatorChildKeysInput(BaseModel):
    """Input for ``bt validator <hotkey> child-keys --on-change``."""

    hotkey: BittensorSS58Address
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


class ValidatorIdentityInput(BaseModel):
    """Input for ``bt validator <hotkey> identity [state flags]``."""

    hotkey: BittensorSS58Address
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
    "ValidatorChildKeysInput",
    "ValidatorCommissionInput",
    "ValidatorDividendsInput",
    "ValidatorIdentityInput",
    "ValidatorStakeInput",
    "ValidatorWeightsInput",
]
