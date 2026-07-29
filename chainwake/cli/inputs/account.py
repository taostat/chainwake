"""Input models for ``chainwake bt account *`` commands."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from chainwake.cli.inputs.common import (
    AboveCondition,
    BelowCondition,
    BittensorSS58Address,
    DropPctCondition,
    MovePctCondition,
    OnChangeCondition,
    PositiveDuration,
    PositiveMaxRuntime,
    RisePctCondition,
)

BalanceCondition = Annotated[
    BelowCondition
    | AboveCondition
    | DropPctCondition
    | RisePctCondition
    | MovePctCondition
    | OnChangeCondition,
    Field(discriminator="kind", description="Threshold, delta, or state condition."),
]


class AccountBalanceInput(BaseModel):
    """Input for ``bt account <coldkey> balance [condition flags]``."""

    coldkey: BittensorSS58Address
    condition: BalanceCondition = Field(description="Threshold, delta, or state condition.")
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


class AccountActivityInput(BaseModel):
    """Input for ``bt account <coldkey> activity --silent-for <duration>``."""

    coldkey: BittensorSS58Address
    silent_for: PositiveDuration = Field(
        description="Fire after no account activity for this duration (e.g. '1h', '3epochs')."
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


__all__ = ["AccountActivityInput", "AccountBalanceInput"]
