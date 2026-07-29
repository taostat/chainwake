"""Input model for ``chainwake bt tx`` command."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field

from chainwake.cli.inputs.common import PositiveMaxRuntime
from chainwake.core.tx_hash import validate_tx_hash

TxHash = Annotated[
    str,
    Field(pattern=r"^0x[0-9a-fA-F]{64}$"),
    AfterValidator(validate_tx_hash),
]


class TxInput(BaseModel):
    """Input for ``bt tx <hash> --finality <level>``.

    Waits for a transaction to reach the specified finality level.
    """

    tx_hash: TxHash = Field(description="32-byte 0x-prefixed transaction hash.")
    finality: Literal["included", "finalized"] = Field(
        description="Required finality level: 'included' or 'finalized'."
    )
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


__all__ = ["TxInput"]
