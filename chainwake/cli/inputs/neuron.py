"""Input models for ``chainwake bt neuron *`` commands."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from chainwake.cli.inputs.common import (
    AboveCondition,
    BelowCondition,
    BittensorSS58Address,
    DropPctCondition,
    MechanismNetuid,
    MovePctCondition,
    Netuid,
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


class NeuronLastUpdateInput(BaseModel):
    """Input for ``bt neuron <netuid> <hotkey> last-update --silent-for <duration>``."""

    netuid: MechanismNetuid = Field(description="Mechanism-indexed subnet netuid (0-4095).")
    hotkey: BittensorSS58Address
    mechid: int = Field(
        0,
        ge=0,
        le=15,
        description="Subnet mechanism id. Defaults to the main mechanism (0).",
    )
    silent_for: PositiveDuration = Field(
        description="Fire after no last-update activity for this duration (e.g. '10blocks', '1h')."
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


class NeuronIncentiveInput(BaseModel):
    """Input for ``bt neuron <netuid> <hotkey> incentive [condition flags]``."""

    netuid: MechanismNetuid = Field(description="Mechanism-indexed subnet netuid (0-4095).")
    hotkey: BittensorSS58Address
    mechid: int = Field(
        0,
        ge=0,
        le=15,
        description="Subnet mechanism id. Defaults to the main mechanism (0).",
    )
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


class NeuronDividendsInput(BaseModel):
    """Input for ``bt neuron <netuid> <hotkey> dividends [condition flags]``."""

    netuid: Netuid = Field(description="Subnet netuid.")
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


class NeuronStakeInput(BaseModel):
    """Input for ``bt neuron <netuid> <hotkey> stake-alpha [condition flags]``."""

    netuid: Netuid = Field(description="Subnet netuid.")
    hotkey: BittensorSS58Address
    condition: ThresholdOrDeltaCondition = Field(
        description="Threshold or delta condition in this subnet's alpha token."
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


class NeuronImmunityBlocksInput(BaseModel):
    """Input for ``bt neuron <netuid> <hotkey> blocks-until-immunity-expires``."""

    netuid: Netuid = Field(description="Subnet netuid.")
    hotkey: BittensorSS58Address
    condition: ThresholdCondition = Field(description="Threshold condition.")
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
    "NeuronDividendsInput",
    "NeuronImmunityBlocksInput",
    "NeuronIncentiveInput",
    "NeuronLastUpdateInput",
    "NeuronStakeInput",
]
