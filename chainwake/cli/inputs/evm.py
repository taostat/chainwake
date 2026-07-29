"""MCP input models for profile-driven EVM wakes."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, model_validator

from chainwake.cli.inputs.common import (
    AboveCondition,
    BelowCondition,
    ExternalPriceCondition,
    PositiveMaxRuntime,
    PositiveWindowDuration,
)
from chainwake.cli.inputs.tx import TxHash


class _EvmDeltaCondition(BaseModel):
    """Percentage movement with time, block, or watcher-start baseline."""

    pct: float = Field(gt=0, allow_inf_nan=False)
    window_time: PositiveWindowDuration | None = None
    window_blocks: int | None = Field(None, gt=0)

    @model_validator(mode="after")
    def _exclusive_window(self) -> Self:
        if self.window_time is not None and self.window_blocks is not None:
            raise ValueError("window_time and window_blocks are mutually exclusive")
        return self


class EvmDropPctCondition(_EvmDeltaCondition):
    kind: Literal["drop-pct"] = "drop-pct"


class EvmRisePctCondition(_EvmDeltaCondition):
    kind: Literal["rise-pct"] = "rise-pct"


class EvmMovePctCondition(_EvmDeltaCondition):
    kind: Literal["move-pct"] = "move-pct"


EvmNumericCondition = Annotated[
    BelowCondition
    | AboveCondition
    | EvmDropPctCondition
    | EvmRisePctCondition
    | EvmMovePctCondition,
    Field(discriminator="kind"),
]


class _EvmWatcherInput(BaseModel):
    """Common controls for profile-driven EVM numeric wakes."""

    rpc_url: str | None = Field(None, description="Override the chain's WebSocket RPC endpoint.")
    out: list[str] = Field(default_factory=list, description="Output adapter URIs.")
    name: str | None = Field(None, description="Human-readable watcher label.")
    max_runtime: PositiveMaxRuntime | None = Field(
        None,
        description="Hard upper-bound on watcher lifetime. Default: unbounded.",
    )
    max_ru: int | None = Field(
        None,
        ge=0,
        description="Registry-estimated observation budget; not a provider billing cap.",
    )


class EvmFeeInput(_EvmWatcherInput):
    """Input for an EVM fee observable."""

    condition: EvmNumericCondition = Field(
        description="Threshold in gwei or percentage movement condition."
    )


class EvmTokenPriceInput(_EvmWatcherInput):
    """Input for an EVM token aggregate USD price watcher."""

    token: str = Field(
        min_length=1,
        description="Chain-scoped token symbol or 20-byte contract address.",
    )
    condition: ExternalPriceCondition = Field(
        description="Threshold in USD or percentage movement condition."
    )


class _EvmTxInput(BaseModel):
    """Common transaction wait inputs."""

    tx_hash: TxHash = Field(description="32-byte 0x-prefixed transaction hash.")
    confirmations: int | None = Field(
        None,
        ge=1,
        description="Required canonical confirmations. Defaults to 1 for included.",
    )
    rpc_url: str | None = Field(None, description="Override the chain's WebSocket RPC endpoint.")
    out: list[str] = Field(default_factory=list, description="Output adapter URIs.")
    name: str | None = Field(None, description="Human-readable watcher label.")
    max_runtime: PositiveMaxRuntime | None = Field(
        None,
        description="Hard upper-bound on watcher lifetime. Default: unbounded.",
    )
    max_ru: int | None = Field(
        None,
        ge=0,
        description="Registry-estimated observation budget; not a provider billing cap.",
    )

    @model_validator(mode="after")
    def _validate_finality(self) -> Self:
        if getattr(self, "finality", "included") != "included" and self.confirmations is not None:
            raise ValueError("confirmations can only be combined with included finality")
        return self


class EthereumTxInput(_EvmTxInput):
    """Input for ``eth tx <hash>``."""

    finality: Literal["included", "safe", "finalized"] = Field(
        "included",
        description="Wait for inclusion confirmations, safe head, or finalized head.",
    )


class BaseTxInput(_EvmTxInput):
    """Input for ``base tx <hash>``."""

    finality: Literal["included", "safe", "finalized"] = Field(
        "included",
        description="Wait for inclusion confirmations, Base safe head, or finalized L1 data.",
    )


class BscTxInput(_EvmTxInput):
    """Input for ``bsc tx <hash>``."""

    finality: Literal["included", "finalized"] = Field(
        "included",
        description="Wait for canonical confirmations or BSC fast finality.",
    )


__all__ = [
    "BaseTxInput",
    "BscTxInput",
    "EthereumTxInput",
    "EvmDropPctCondition",
    "EvmFeeInput",
    "EvmMovePctCondition",
    "EvmRisePctCondition",
    "EvmTokenPriceInput",
]
