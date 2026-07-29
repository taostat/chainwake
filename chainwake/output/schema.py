"""Pydantic models for the current chainwake output JSON contract.

The published JSON Schema lives at `schemas/output.json` and is regenerated
by `scripts/generate_json_schema.py`. The Pydantic models here are the
authoritative source; the JSON Schema is derived from them.

Every watcher invocation in JSON mode emits exactly one envelope on exit,
regardless of match / timeout / error class. Non-watcher CLI helpers are
outside this contract. The `status` field is the discriminator.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Annotated, Literal, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from chainwake.chains import ChainAlias
from chainwake.core.duration import (
    InvalidDurationError,
    duration_to_seconds,
    parse_duration_components,
)


class _StrictModel(BaseModel):
    """Base for every payload model. Forbids unknown fields, frozen by default."""

    model_config = ConfigDict(extra="forbid", frozen=True)


PrimitiveName = Literal["threshold", "delta", "event", "liveness", "state", "tx"]
WindowUnit = Literal["ever", "time", "blocks", "epochs"]


class Window(_StrictModel):
    unit: WindowUnit
    value: str = Field(
        description=(
            "Window length ('1h', '50', '5') or 'watcher-start' for the unbounded ever baseline."
        )
    )

    @model_validator(mode="after")
    def _validate_positive_explicit_window(self) -> Self:
        if self.unit == "ever":
            if self.value != "watcher-start":
                raise ValueError("ever window value must be 'watcher-start'")
            return self
        try:
            if self.unit == "time":
                magnitude = duration_to_seconds(self.value)
            else:
                magnitude = float(self.value)
        except (InvalidDurationError, ValueError) as exc:
            raise ValueError(f"invalid {self.unit} window {self.value!r}") from exc
        if not math.isfinite(magnitude) or magnitude <= 0:
            raise ValueError("explicit window must be finite and greater than zero")
        return self


class Watcher(_StrictModel):
    chain: ChainAlias
    resource: str
    resource_id: str | None = None
    sub_resource: str | None = None
    name: str | None = None
    primitive: PrimitiveName
    invocation: list[str] = Field(description="Original argv as invoked.")


class ThresholdCondition(_StrictModel):
    operator: Literal["below", "above"]
    target: float = Field(allow_inf_nan=False)


class DeltaCondition(_StrictModel):
    operator: Literal["drop-pct", "rise-pct", "move-pct"]
    target: float = Field(gt=0, allow_inf_nan=False)
    window: Window


class StateCondition(_StrictModel):
    operator: Literal["on-change", "changes-to", "changes-from"]
    target: str | int | float | bool | None = None


class LivenessCondition(_StrictModel):
    operator: Literal["silent-for"]
    duration: str = Field(description="Duration, e.g. '3epochs', '10m'.")

    @model_validator(mode="after")
    def _validate_positive_duration(self) -> Self:
        try:
            _, magnitude = parse_duration_components(self.duration)
        except InvalidDurationError as exc:
            raise ValueError(str(exc)) from exc
        if not math.isfinite(magnitude) or magnitude <= 0:
            raise ValueError("liveness duration must be finite and greater than zero")
        return self


class EventCondition(_StrictModel):
    event_type: str
    filters: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class TxCondition(_StrictModel):
    finality: Literal["included", "safe", "finalized"]
    confirmations: int | None = None
    timeout: str | None = None


Condition = Annotated[
    ThresholdCondition
    | DeltaCondition
    | StateCondition
    | LivenessCondition
    | EventCondition
    | TxCondition,
    Field(discriminator=None),
]


class ObservedThreshold(_StrictModel):
    path: str
    value: float = Field(allow_inf_nan=False)
    block: int
    block_hash: str
    timestamp: datetime
    meta: dict[str, JsonValue] = Field(
        default_factory=dict,
        exclude_if=lambda value: not value,
        description="Provider context and source provenance when available.",
    )


class ObservedDelta(_StrictModel):
    path: str
    value: float = Field(allow_inf_nan=False)
    previous_value: float = Field(allow_inf_nan=False)
    delta: float = Field(allow_inf_nan=False)
    delta_pct: float = Field(allow_inf_nan=False)
    block: int
    block_hash: str
    timestamp: datetime
    meta: dict[str, JsonValue] = Field(
        default_factory=dict,
        exclude_if=lambda value: not value,
        description="Provider context and source provenance when available.",
    )


class ObservedState(_StrictModel):
    path: str
    value: str | int | float | bool | dict[str, object] | list[JsonValue] | None
    previous_value: str | int | float | bool | dict[str, object] | list[JsonValue] | None
    changed_keys: list[str] | None = None
    block: int
    block_hash: str
    timestamp: datetime


class ObservedLiveness(_StrictModel):
    path: str
    last_seen_block: int
    last_seen_timestamp: datetime | None
    elapsed: str
    block: int
    block_hash: str
    timestamp: datetime


class ObservedEvent(_StrictModel):
    event_type: str
    raw_event: str
    args: dict[str, JsonValue]
    block: int
    block_hash: str
    timestamp: datetime
    extrinsic_hash: str | None = None


class ObservedTx(_StrictModel):
    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {
                    "required": [
                        "confirmations",
                        "execution_status",
                        "gas_used",
                        "effective_gas_price_wei",
                    ],
                    "properties": {
                        "confirmations": {"type": "integer", "minimum": 1},
                        "execution_status": {
                            "type": "string",
                            "enum": ["success", "reverted"],
                        },
                        "gas_used": {"type": "integer", "minimum": 0},
                        "effective_gas_price_wei": {"type": "integer", "minimum": 0},
                    },
                },
                {
                    "not": {
                        "anyOf": [
                            {"required": ["confirmations"]},
                            {"required": ["execution_status"]},
                            {"required": ["gas_used"]},
                            {"required": ["effective_gas_price_wei"]},
                        ]
                    },
                },
            ]
        }
    )

    tx_hash: str
    finality: Literal["included", "safe", "finalized"]
    block: int
    block_hash: str
    timestamp: datetime
    confirmations: int | None = Field(default=None, ge=1)
    execution_status: Literal["success", "reverted"] | None = None
    gas_used: int | None = Field(default=None, ge=0)
    effective_gas_price_wei: int | None = Field(default=None, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _validate_receipt_keys(cls, value: object) -> object:
        if isinstance(value, dict):
            data = cast(dict[str, object], value)
            receipt_keys = {
                "confirmations",
                "execution_status",
                "gas_used",
                "effective_gas_price_wei",
            }
            present_keys = receipt_keys.intersection(data)
            if present_keys and present_keys != receipt_keys:
                raise ValueError("transaction receipt context must be all present or all absent")
            if present_keys == receipt_keys and all(data[key] is None for key in receipt_keys):
                raise ValueError("absent transaction receipt context must omit all receipt fields")
        return value

    @model_validator(mode="after")
    def _validate_receipt_context(self) -> Self:
        receipt_context = (
            self.confirmations,
            self.execution_status,
            self.gas_used,
            self.effective_gas_price_wei,
        )
        if any(value is not None for value in receipt_context) and not all(
            value is not None for value in receipt_context
        ):
            raise ValueError("transaction receipt context must be all present or all absent")
        return self

    @model_serializer(mode="wrap")
    def _serialize_receipt_context(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> dict[str, object]:
        data = cast(dict[str, object], handler(self))
        if self.confirmations is None:
            for key in (
                "confirmations",
                "execution_status",
                "gas_used",
                "effective_gas_price_wei",
            ):
                data.pop(key, None)
        return data


Observed = Annotated[
    ObservedThreshold
    | ObservedDelta
    | ObservedState
    | ObservedLiveness
    | ObservedEvent
    | ObservedTx,
    Field(discriminator=None),
]


class Budget(_StrictModel):
    runtime_ms: int
    rpc_calls: int
    estimated_ru_consumed: int


class Process(_StrictModel):
    pid: int
    started_at: datetime


class _EnvelopeBase(_StrictModel):
    watcher: Watcher
    condition: Condition
    budget: Budget
    process: Process


class MatchedPayload(_EnvelopeBase):
    status: Literal["matched"] = "matched"
    observed: Observed


TimeoutReason = Literal["max_runtime_reached"]


class TimeoutPayload(_EnvelopeBase):
    status: Literal["timeout"] = "timeout"
    reason: TimeoutReason
    observed: None = None


class StoppedPayload(_EnvelopeBase):
    """A watcher ended because the process received SIGINT or SIGTERM."""

    status: Literal["stopped"] = "stopped"
    reason: Literal["shutdown_requested"] = "shutdown_requested"
    observed: None = None


BudgetExhaustedReason = Literal["max_ru_reached", "provider_compute_units_exhausted"]


class BudgetExhaustedPayload(_EnvelopeBase):
    status: Literal["budget_exhausted"] = "budget_exhausted"
    reason: BudgetExhaustedReason
    observed: None = None


class _ErrorEnvelopeBase(_StrictModel):
    watcher: Watcher | None = None
    condition: Condition | None = None
    budget: Budget
    process: Process
    observed: None = None
    message: str


class UserErrorPayload(_ErrorEnvelopeBase):
    status: Literal["user_error"] = "user_error"
    reason: str


class ProviderErrorPayload(_ErrorEnvelopeBase):
    status: Literal["provider_error"] = "provider_error"
    reason: Literal[
        "auth_failed",
        "rpc_unreachable",
        "rate_limited",
        "subscription_failed",
        "decode_failed",
    ]


AuthErrorReason = Literal[
    "auth_failed",
    "missing_api_key",
    "invalid_api_key",
    "expired_api_key",
]


class AuthErrorPayload(_ErrorEnvelopeBase):
    """Authentication failure — chain rejected the request as unauthenticated.

    Spec §9.4 / Appendix D: emitted as its own top-level status (not folded
    into ``provider_error``) so agents can match the documented spec string
    and so the payload itself carries actionable hints (env-var names, docs
    URL) rather than burying them in the message.
    """

    status: Literal["auth_error"] = "auth_error"
    reason: AuthErrorReason = "auth_failed"
    api_key_env_vars: list[str] = Field(
        default_factory=list,
        description="Environment variables the user can set to provide an API key.",
    )
    docs_url: str | None = Field(
        default=None,
        description="Link to provider signup / API-key documentation.",
    )


class InternalErrorPayload(_ErrorEnvelopeBase):
    status: Literal["internal_error"] = "internal_error"
    reason: str


Payload = Annotated[
    MatchedPayload
    | TimeoutPayload
    | StoppedPayload
    | BudgetExhaustedPayload
    | UserErrorPayload
    | ProviderErrorPayload
    | AuthErrorPayload
    | InternalErrorPayload,
    Field(discriminator="status"),
]


class Envelope(_StrictModel):
    """Discriminated wrapper used by the JSON Schema generator and CLI output.

    Use `Envelope.model_validate(json_dict).root` style only via `payload`. In
    practice we serialise the underlying `Payload` directly, since the
    `status` discriminator field is on the payload itself.
    """

    payload: Payload


def serialize(payload: Payload) -> dict[str, object]:
    """Return a JSON-ready dict for any payload type."""

    return payload.model_dump(mode="json")


__all__ = [
    "AuthErrorPayload",
    "AuthErrorReason",
    "Budget",
    "BudgetExhaustedPayload",
    "ChainAlias",
    "Condition",
    "DeltaCondition",
    "Envelope",
    "EventCondition",
    "InternalErrorPayload",
    "LivenessCondition",
    "MatchedPayload",
    "Observed",
    "ObservedDelta",
    "ObservedEvent",
    "ObservedLiveness",
    "ObservedState",
    "ObservedThreshold",
    "ObservedTx",
    "Payload",
    "PrimitiveName",
    "Process",
    "ProviderErrorPayload",
    "StateCondition",
    "StoppedPayload",
    "ThresholdCondition",
    "TimeoutPayload",
    "TxCondition",
    "UserErrorPayload",
    "Watcher",
    "Window",
    "WindowUnit",
    "serialize",
]
