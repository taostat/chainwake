"""Input models for ``chainwake bt event`` command."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from chainwake.cli.inputs.common import BittensorSS58Address, PositiveMaxRuntime

FRIENDLY_EVENT_NAMES = Literal[
    "transfer",
    "stake-added",
    "stake-removed",
    "swap",
    "neuron-registered",
    "subnet-registered",
    "weights-set",
    "axon-served",
    "validator-permit-changed",
    "child-keys-set",
    "identity-set",
]

EventDirection = Literal["in", "out"]
_RAW_EVENT_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]*\.[A-Za-z][A-Za-z0-9_]*")


def is_raw_event_type(value: str) -> bool:
    """Return whether *value* is exactly one ASCII ``Module.Event`` identifier."""
    return _RAW_EVENT_PATTERN.fullmatch(value) is not None


class EventInput(BaseModel):
    """Input for ``bt event --type <name>`` or ``--type-raw <Module.Event>``.

    Exactly one of event_type or type_raw must be provided. The friendly
    event_type is validated against the 11 curated names from Appendix B;
    type_raw accepts any Substrate Module.Event string.

    Per-event filters (``from_addr``, ``to_addr``, ``amount_min``,
    ``direction``) translate at dispatch time to an exact-match
    ``args_match`` dict on the provider's ``EventFilter``. ``amount_min``
    is a lower bound that the dispatcher renders as a sentinel — the actual
    provider currently supports exact-match args; an inequality match is
    expressed by the dispatcher's logic, not the EventFilter contract.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {"required": ["event_type"], "not": {"required": ["type_raw"]}},
                {"required": ["type_raw"], "not": {"required": ["event_type"]}},
            ]
        }
    )

    event_type: FRIENDLY_EVENT_NAMES | None = Field(
        None,
        description=(
            "Friendly event name (e.g. 'transfer', 'swap', 'subnet-registered'). "
            "One of event_type or type_raw is required."
        ),
    )
    type_raw: str | None = Field(
        None,
        description=(
            "Raw Substrate event identifier (Module.Event, e.g. 'Balances.Transfer'). "
            "One of event_type or type_raw is required."
        ),
    )
    from_addr: BittensorSS58Address | None = Field(
        None,
        description=("Filter to events whose decoded 'from' field matches this SS58 address."),
    )
    to_addr: BittensorSS58Address | None = Field(
        None,
        description=("Filter to events whose decoded 'to' field matches this SS58 address."),
    )
    amount_min: int | None = Field(
        None,
        ge=0,
        description=(
            "Filter to events whose decoded 'amount' is >= this value (rao). "
            "Applied at dispatch; provider EventFilter only supports exact-match."
        ),
    )
    direction: EventDirection | None = Field(
        None,
        description=(
            "Filter by direction relative to a paired address: 'in' or 'out'. "
            "Currently only meaningful when combined with --from or --to."
        ),
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

    @field_validator("type_raw")
    @classmethod
    def _validate_type_raw(cls, value: str | None) -> str | None:
        if value is not None and not is_raw_event_type(value):
            raise ValueError("type_raw must use Module.Event syntax")
        return value

    @model_validator(mode="after")
    def _validate_event_selection_and_direction(self) -> EventInput:
        if (self.event_type is None) == (self.type_raw is None):
            raise ValueError("exactly one of event_type or type_raw must be provided")
        if self.direction == "in" and self.to_addr is None:
            raise ValueError("direction='in' requires to_addr")
        if self.direction == "out" and self.from_addr is None:
            raise ValueError("direction='out' requires from_addr")
        return self


__all__ = ["FRIENDLY_EVENT_NAMES", "EventDirection", "EventInput", "is_raw_event_type"]
