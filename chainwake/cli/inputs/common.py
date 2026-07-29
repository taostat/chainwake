"""Shared condition variant models and cross-cutting field definitions.

These models are reused across resource-level input models so condition
semantics (mutual exclusion, window requirements) are defined exactly once.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from chainwake.core.duration import (
    InvalidDurationError,
    duration_to_seconds,
    parse_duration_components,
)
from chainwake.core.ss58 import BITTENSOR_SS58_LENGTH, validate_bittensor_ss58


def _positive_duration(value: str) -> str:
    """Validate a positive wall-clock or chain-native duration."""
    try:
        _, magnitude = parse_duration_components(value)
    except InvalidDurationError as exc:
        raise ValueError(str(exc)) from exc
    if not math.isfinite(magnitude) or magnitude <= 0:
        raise ValueError("duration must be finite and greater than zero")
    return value


def _positive_wall_clock_duration(value: str) -> str:
    """Validate a positive duration that can be resolved without chain state."""
    try:
        seconds = duration_to_seconds(value)
    except InvalidDurationError as exc:
        raise ValueError(str(exc)) from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("duration must be finite and greater than zero")
    return value


def _positive_max_runtime(value: str) -> str:
    """Validate CLI-compatible duration or raw-second max-runtime input."""
    try:
        seconds = duration_to_seconds(value)
    except InvalidDurationError:
        try:
            seconds = float(value)
        except ValueError as exc:
            raise ValueError("expected e.g. '30s', '10m', '2h'") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("max_runtime must be finite and greater than zero")
    return value


PositiveDuration = Annotated[str, AfterValidator(_positive_duration)]
PositiveMaxRuntime = Annotated[str, AfterValidator(_positive_max_runtime)]
PositiveWindowDuration = Annotated[str, AfterValidator(_positive_wall_clock_duration)]
BittensorSS58Address = Annotated[
    str,
    Field(
        min_length=BITTENSOR_SS58_LENGTH,
        max_length=BITTENSOR_SS58_LENGTH,
        pattern=r"^5[1-9A-HJ-NP-Za-km-z]{47}$",
        description=(
            "Canonical Bittensor SS58 address: format 42, 32-byte account id, and valid checksum."
        ),
    ),
    AfterValidator(validate_bittensor_ss58),
]
# Subtensor's public ``NetUid`` protocol type is ``u16``. Mechanism-indexed
# vectors introduced by spec 440 reserve 4096 slots per mechanism, so the
# small set of mechanism-aware watchers has a narrower operational domain.
Netuid = Annotated[int, Field(ge=0, le=65_535)]
MechanismNetuid = Annotated[int, Field(ge=0, le=4_095)]


class BelowCondition(BaseModel):
    """Fire when the observable drops below a fixed threshold."""

    kind: Literal["below"] = "below"
    value: float = Field(
        allow_inf_nan=False,
        description="Fire when value < this finite threshold.",
    )


class AboveCondition(BaseModel):
    """Fire when the observable rises above a fixed threshold."""

    kind: Literal["above"] = "above"
    value: float = Field(
        allow_inf_nan=False,
        description="Fire when value > this finite threshold.",
    )


class _DeltaCondition(BaseModel):
    """Shared exclusive-window contract for percentage-move conditions."""

    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {
                    "properties": {
                        "window_time": {"type": "null"},
                        "window_blocks": {"type": "null"},
                        "window_epochs": {"type": "null"},
                    }
                },
                {
                    "required": ["window_time"],
                    "properties": {
                        "window_time": {"type": "string"},
                        "window_blocks": {"type": "null"},
                        "window_epochs": {"type": "null"},
                    },
                },
                {
                    "required": ["window_blocks"],
                    "properties": {
                        "window_time": {"type": "null"},
                        "window_blocks": {"type": "integer"},
                        "window_epochs": {"type": "null"},
                    },
                },
                {
                    "required": ["window_epochs"],
                    "properties": {
                        "window_time": {"type": "null"},
                        "window_blocks": {"type": "null"},
                        "window_epochs": {"type": "integer"},
                    },
                },
            ]
        }
    )

    pct: float = Field(
        gt=0,
        allow_inf_nan=False,
        description="Finite, positive percentage magnitude to trigger on.",
    )
    window_time: PositiveWindowDuration | None = Field(
        None,
        description=(
            "Window duration (e.g. '1h', '30m', '5d'). "
            "Mutually exclusive with window_blocks / window_epochs. "
            "Omit all window fields to compare with the first successful observation."
        ),
    )
    window_blocks: int | None = Field(
        None,
        gt=0,
        description=(
            "Window length in blocks. Mutually exclusive with window_time / window_epochs."
        ),
    )
    window_epochs: int | None = Field(
        None,
        gt=0,
        description=(
            "Window length in epochs. Mutually exclusive with window_time / window_blocks."
        ),
    )

    @model_validator(mode="after")
    def _validate_window_exclusivity(self) -> Self:
        windows = (self.window_time, self.window_blocks, self.window_epochs)
        if sum(value is not None for value in windows) > 1:
            raise ValueError("window_time, window_blocks, and window_epochs are mutually exclusive")
        return self


class DropPctCondition(_DeltaCondition):
    """Fire on a percentage drop from the watcher-start or rolling baseline."""

    kind: Literal["drop-pct"] = "drop-pct"


class RisePctCondition(_DeltaCondition):
    """Fire on a percentage rise from the watcher-start or rolling baseline."""

    kind: Literal["rise-pct"] = "rise-pct"


class MovePctCondition(_DeltaCondition):
    """Fire on a percentage move from the watcher-start or rolling baseline."""

    kind: Literal["move-pct"] = "move-pct"


class _ExternalPriceDeltaCondition(BaseModel):
    """Percentage movement for timer-sampled external prices."""

    pct: float = Field(gt=0, allow_inf_nan=False)
    window_time: PositiveWindowDuration | None = Field(
        None,
        description=(
            "Optional wall-clock baseline window. Omit it to compare with "
            "the first successful observation."
        ),
    )


class ExternalPriceDropPctCondition(_ExternalPriceDeltaCondition):
    kind: Literal["drop-pct"] = "drop-pct"


class ExternalPriceRisePctCondition(_ExternalPriceDeltaCondition):
    kind: Literal["rise-pct"] = "rise-pct"


class ExternalPriceMovePctCondition(_ExternalPriceDeltaCondition):
    kind: Literal["move-pct"] = "move-pct"


ExternalPriceCondition = Annotated[
    BelowCondition
    | AboveCondition
    | ExternalPriceDropPctCondition
    | ExternalPriceRisePctCondition
    | ExternalPriceMovePctCondition,
    Field(discriminator="kind"),
]


class OnChangeCondition(BaseModel):
    """Fire on any state transition."""

    kind: Literal["on-change"] = "on-change"


class ChangesToCondition(BaseModel):
    """Fire when state transitions to a specific value."""

    kind: Literal["changes-to"] = "changes-to"
    value: str = Field(description="Target value to transition to.")


class ChangesFromCondition(BaseModel):
    """Fire when state transitions away from a specific value."""

    kind: Literal["changes-from"] = "changes-from"
    value: str = Field(description="Source value to transition away from.")


CommissionFraction = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class CommissionChangesToCondition(BaseModel):
    """Fire when validator commission transitions to a finite fraction."""

    kind: Literal["changes-to"] = "changes-to"
    value: CommissionFraction = Field(description="Target commission fraction from 0 to 1.")


class CommissionChangesFromCondition(BaseModel):
    """Fire when validator commission transitions away from a finite fraction."""

    kind: Literal["changes-from"] = "changes-from"
    value: CommissionFraction = Field(description="Source commission fraction from 0 to 1.")


# Re-export for convenience
__all__ = [
    "AboveCondition",
    "BelowCondition",
    "BittensorSS58Address",
    "ChangesFromCondition",
    "ChangesToCondition",
    "CommissionChangesFromCondition",
    "CommissionChangesToCondition",
    "CommissionFraction",
    "DropPctCondition",
    "ExternalPriceCondition",
    "ExternalPriceDropPctCondition",
    "ExternalPriceMovePctCondition",
    "ExternalPriceRisePctCondition",
    "MovePctCondition",
    "OnChangeCondition",
    "PositiveDuration",
    "PositiveMaxRuntime",
    "RisePctCondition",
]
