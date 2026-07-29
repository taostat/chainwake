"""Input models for ``chainwake bt network *`` commands."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from chainwake.cli.inputs.common import (
    AboveCondition,
    BelowCondition,
    DropPctCondition,
    ExternalPriceCondition,
    MovePctCondition,
    OnChangeCondition,
    PositiveMaxRuntime,
    RisePctCondition,
)

ThresholdCondition = Annotated[
    BelowCondition | AboveCondition,
    Field(discriminator="kind", description="Threshold condition."),
]

ThresholdOrDeltaCondition = Annotated[
    BelowCondition | AboveCondition | DropPctCondition | RisePctCondition | MovePctCondition,
    Field(discriminator="kind", description="Threshold or percentage-move condition."),
]


class NetworkSubnetRegistrationCostInput(BaseModel):
    """Input for ``bt network subnet-registration-cost [threshold flags]``."""

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


class NetworkSubnetCountInput(BaseModel):
    """Input for ``bt network subnet-count [condition flags]``."""

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


class NetworkTaoPriceInput(NetworkSubnetCountInput):
    """Input for ``bt network tao-price``."""

    condition: ExternalPriceCondition = Field(
        description="TAO/USD threshold or wall-clock percentage-move condition."
    )


class NetworkRuntimeVersionInput(BaseModel):
    """Input for ``bt network runtime-version --on-change``."""

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


class NetworkOnRuntimeUpgradedInput(BaseModel):
    """Input for ``bt network on-runtime-upgraded`` event watcher.

    Fires on ``System.CodeUpdated``. Equivalent semantically to
    ``runtime-version --on-change`` but routed through the event subscription
    instead of the state primitive.
    """

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


# OnChangeCondition re-exported for completeness.
__all__ = [
    "NetworkOnRuntimeUpgradedInput",
    "NetworkRuntimeVersionInput",
    "NetworkSubnetCountInput",
    "NetworkSubnetRegistrationCostInput",
    "NetworkTaoPriceInput",
    "OnChangeCondition",
]
