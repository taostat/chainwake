"""Watcher runtime: wires provider → primitive → adapter fan-out.

Drives the watcher loop with:
  - subscription-first, polling fallback per spec §9.2
  - transient-retry backoff per spec §9.4
  - rate-limit bounded backoff per spec §9.4
  - registry-estimated observation guard (--max-ru) per spec §11
  - runtime deadline enforcement (--max-runtime)
  - multi-adapter fan-out: all adapters receive every match
  - clean SIGTERM/SIGINT shutdown

Exit codes:
  0  matched (condition fired)
  1  timeout / budget_exhausted
  3  provider_error (auth failed, rpc unreachable, etc.)
  4  internal_error (unexpected exception)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import signal
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import IO, Literal, TypeVar, cast

from chainwake.chains import ChainAlias, ChainRuntimeConfig, backend_for
from chainwake.core.budget import Budget
from chainwake.core.duration import parse_duration_components
from chainwake.core.errors import (
    AuthError,
    BudgetExhaustedError,
    CUExhaustedError,
    HeadUnavailableError,
    ProviderError,
    RateLimitError,
    RPCUnreachableError,
    SubscriptionFailedError,
    UserError,
)
from chainwake.core.primitives.base import Match, Primitive, PrimitiveInput
from chainwake.core.registry import ObservationDriver, RegistryEntry
from chainwake.core.retry import RateLimitGuard, with_transient_retry
from chainwake.output.adapters import Adapter
from chainwake.output.schema import (
    AuthErrorPayload,
    AuthErrorReason,
    BudgetExhaustedPayload,
    Condition,
    DeltaCondition,
    EventCondition,
    InternalErrorPayload,
    LivenessCondition,
    MatchedPayload,
    Observed,
    ObservedDelta,
    ObservedEvent,
    ObservedLiveness,
    ObservedState,
    ObservedThreshold,
    ObservedTx,
    Payload,
    PrimitiveName,
    Process,
    ProviderErrorPayload,
    StoppedPayload,
    ThresholdCondition,
    TimeoutPayload,
)
from chainwake.output.schema import (
    Budget as SchemaBudget,
)
from chainwake.output.schema import (
    Watcher as SchemaWatcher,
)
from chainwake.providers.base import (
    BlockRef,
    Cadence,
    ChainProvider,
    EpochProvider,
    EpochState,
    Event,
    EventFilter,
    EventSubscriptionProvider,
    HeadSubscriptionProvider,
    ObservableValue,
    StorageSubscriptionProvider,
    StorageUpdate,
)

_log = logging.getLogger(__name__)

ProviderErrorReason = Literal[
    "auth_failed",
    "rpc_unreachable",
    "rate_limited",
    "subscription_failed",
    "decode_failed",
]

_DEFAULT_RUNTIME = backend_for("bt").runtime


class _SubscriptionDeadlineReachedError(Exception):
    """The subscription stayed silent until the watcher deadline."""


class _SubscriptionShutdownError(Exception):
    """The watcher received a shutdown signal while awaiting an event."""


class _WatcherDeadlineReachedError(Exception):
    """A provider call or its retry backoff consumed the remaining runtime."""


_T = TypeVar("_T")


_SECONDS_PER_DAY: int = 86_400


def _resolve_effective_poll(
    spec_poll_seconds: float | None,
    *,
    policy_poll_seconds: float | None = None,
    runtime: ChainRuntimeConfig = _DEFAULT_RUNTIME,
) -> float:
    """Pick the runtime's effective poll interval for a watcher.

    When the user passes ``--poll-seconds N`` the explicit override stays in
    force. When unset, the runtime falls back to the chain's natural block
    time so per-block watchers don't waste 11/12 polls on a 12-second-block
    chain.

    Bittensor mainnet's block time is hardcoded at 12s here. We do not query
    ``Babe.SlotDuration``/``Aura.SlotDuration`` on connect because (a) it
    would re-introduce the very RPC dependency the cadence-aware default is
    meant to remove, (b) the value is fixed at runtime upgrade granularity,
    and (c) per-chain block-time deltas (FAST_BLOCKS localnet, future chains)
    are best handled by callers that know their environment passing
    ``--poll-seconds`` explicitly. ``Cadence.PER_EPOCH`` uses this interval
    to inspect the stateful epoch marker once per block so owner-triggered
    epochs are observed promptly. ``Cadence.PER_EVENT`` ignores it.
    """
    if spec_poll_seconds is not None:
        return spec_poll_seconds
    if policy_poll_seconds is not None:
        return policy_poll_seconds
    return runtime.block_seconds


@dataclass(slots=True)
class WatcherSpec:
    """Watcher description passed from the CLI to the runtime.

    `path_params` is the explicit map of registry path-template parameter
    names to their CLI-supplied values (e.g. ``{"netuid": "19", "hotkey":
    "5Fxxx"}``). The runtime feeds it directly to ``RegistryEntry.render_path``,
    so multi-param resources (neuron = netuid + hotkey, etc.) work without
    losing individual params.

    The output payload's ``Watcher.resource_id`` is a flat string for human
    consumption: registry-template order, dot-joined (e.g. ``"19.5Fxxx"``).

    `read_args` carries the extra arguments declared by computed observables
    (e.g. `--size` and `--max-bps` for `subnet.{netuid}.pool.depth-for-trade`)
    through to `provider.read_observable(path, args)`. Empty for non-computed
    observables.
    """

    chain: ChainAlias
    resource: str
    path_params: dict[str, str]
    sub_resource: str
    primitive_name: str
    condition: Condition
    invocation: list[str]
    name: str | None = None
    poll_seconds: float | None = None
    max_runtime_seconds: float | None = None
    max_ru: int | None = None
    read_args: dict[str, object] = field(default_factory=dict)
    event_filter: EventFilter | None = None

    def __post_init__(self) -> None:
        if self.path_params is None:  # pragma: no cover - defensive
            object.__setattr__(self, "path_params", {})
        if self.read_args is None:  # pragma: no cover - defensive
            object.__setattr__(self, "read_args", {})
        _validate_positive_optional_number(self.poll_seconds, "poll_seconds")
        _validate_positive_optional_number(
            self.max_runtime_seconds,
            "max_runtime_seconds",
        )
        if isinstance(self.condition, ThresholdCondition) and not math.isfinite(
            self.condition.target
        ):
            raise ValueError("threshold target must be finite")
        if isinstance(self.condition, DeltaCondition):
            _validate_positive_number(self.condition.target, "delta target")
            window = self.condition.window
            if window.unit != "ever":
                if window.unit == "time":
                    unit, magnitude = parse_duration_components(window.value)
                    if unit != "time":
                        raise ValueError("time window requires a wall-clock duration")
                else:
                    magnitude = float(window.value)
                _validate_positive_number(magnitude, f"{window.unit} window")
        if isinstance(self.condition, LivenessCondition):
            _, magnitude = parse_duration_components(self.condition.duration)
            _validate_positive_number(magnitude, "liveness duration")


def _validate_positive_optional_number(value: float | None, name: str) -> None:
    if value is not None:
        _validate_positive_number(value, name)


def _validate_positive_number(value: float, name: str) -> None:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and greater than zero")


# ---------------------------------------------------------------------------
# Schema builder helpers
# ---------------------------------------------------------------------------


def _format_resource_id(path_params: dict[str, str]) -> str | None:
    """Join path-param values into a flat human-readable string, or None.

    Output payload's `Watcher.resource_id` is a string in the schema; we
    keep that surface flat by joining values in insertion order. The runtime
    uses the dict directly via `entry.render_path(path_params)` — only the
    output representation is flattened.
    """
    if not path_params:
        return None
    return ".".join(path_params.values())


def build_watcher(spec: WatcherSpec) -> SchemaWatcher:
    return SchemaWatcher(
        chain=spec.chain,
        resource=spec.resource,
        resource_id=_format_resource_id(spec.path_params),
        sub_resource=spec.sub_resource,
        name=spec.name,
        primitive=cast(PrimitiveName, spec.primitive_name),
        invocation=list(spec.invocation),
    )


_OBSERVED_BUILDERS: dict[
    str,
    type[ObservedThreshold]
    | type[ObservedDelta]
    | type[ObservedState]
    | type[ObservedLiveness]
    | type[ObservedEvent]
    | type[ObservedTx],
] = {
    "threshold": ObservedThreshold,
    "delta": ObservedDelta,
    "state": ObservedState,
    "liveness": ObservedLiveness,
    "event": ObservedEvent,
    "tx": ObservedTx,
}


def _build_observed(
    primitive_name: str,
    raw: dict[str, object],
) -> Observed:
    """Build the right Observed* model from a primitive's match.observed dict.

    `raw` already has the field names matching the corresponding `Observed*`
    Pydantic model — primitives are responsible for that contract.
    Pydantic does its own coercion (e.g. ISO datetime strings).
    """
    cls = _OBSERVED_BUILDERS.get(primitive_name)
    if cls is None:
        raise ValueError(
            f"unknown primitive_name {primitive_name!r}; cannot build Observed payload"
        )
    return cls.model_validate(raw)


def build_budget(budget: Budget) -> SchemaBudget:
    return SchemaBudget(
        runtime_ms=budget.runtime_ms,
        rpc_calls=budget.rpc_calls,
        estimated_ru_consumed=budget.estimated_ru_consumed,
    )


def build_process(started_at: datetime) -> Process:
    return Process(pid=os.getpid(), started_at=started_at)


def _build_match_payload(
    spec: WatcherSpec,
    match: Match,
    budget: Budget,
) -> Payload:
    observed = _build_observed(spec.primitive_name, match.observed)
    return MatchedPayload(
        watcher=build_watcher(spec),
        condition=spec.condition,
        observed=observed,
        budget=build_budget(budget),
        process=build_process(budget.started_at),
    )


def _build_timeout_payload(spec: WatcherSpec, budget: Budget) -> Payload:
    return TimeoutPayload(
        watcher=build_watcher(spec),
        condition=spec.condition,
        reason="max_runtime_reached",
        budget=build_budget(budget),
        process=build_process(budget.started_at),
    )


def _build_stopped_payload(spec: WatcherSpec, budget: Budget) -> Payload:
    return StoppedPayload(
        watcher=build_watcher(spec),
        condition=spec.condition,
        budget=build_budget(budget),
        process=build_process(budget.started_at),
    )


def _build_budget_exhausted_payload(
    spec: WatcherSpec,
    budget: Budget,
    reason: Literal["max_ru_reached", "provider_compute_units_exhausted"],
) -> Payload:
    return BudgetExhaustedPayload(
        watcher=build_watcher(spec),
        condition=spec.condition,
        reason=reason,
        budget=build_budget(budget),
        process=build_process(budget.started_at),
    )


def build_provider_error_payload(
    spec: WatcherSpec,
    *,
    started_at: datetime,
    message: str,
    reason: ProviderErrorReason = "rpc_unreachable",
    rpc_calls: int = 0,
) -> ProviderErrorPayload:
    fake_budget = SchemaBudget(
        runtime_ms=int((datetime.now(UTC) - started_at).total_seconds() * 1000),
        rpc_calls=rpc_calls,
        estimated_ru_consumed=rpc_calls,
    )
    return ProviderErrorPayload(
        watcher=build_watcher(spec),
        condition=spec.condition,
        budget=fake_budget,
        process=build_process(started_at),
        message=message,
        reason=reason,
    )


def build_auth_error_payload(
    spec: WatcherSpec,
    *,
    started_at: datetime,
    message: str,
    reason: AuthErrorReason = "auth_failed",
    api_key_env_vars: list[str] | None = None,
    docs_url: str | None = None,
    rpc_calls: int = 0,
) -> AuthErrorPayload:
    fake_budget = SchemaBudget(
        runtime_ms=int((datetime.now(UTC) - started_at).total_seconds() * 1000),
        rpc_calls=rpc_calls,
        estimated_ru_consumed=rpc_calls,
    )
    return AuthErrorPayload(
        watcher=build_watcher(spec),
        condition=spec.condition,
        budget=fake_budget,
        process=build_process(started_at),
        message=message,
        reason=reason,
        api_key_env_vars=list(api_key_env_vars) if api_key_env_vars else [],
        docs_url=docs_url,
    )


def build_internal_error_payload(
    spec: WatcherSpec,
    *,
    started_at: datetime,
    message: str,
    reason: str,
    rpc_calls: int = 0,
) -> InternalErrorPayload:
    fake_budget = SchemaBudget(
        runtime_ms=int((datetime.now(UTC) - started_at).total_seconds() * 1000),
        rpc_calls=rpc_calls,
        estimated_ru_consumed=rpc_calls,
    )
    return InternalErrorPayload(
        watcher=build_watcher(spec),
        condition=spec.condition,
        budget=fake_budget,
        process=build_process(started_at),
        message=message,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Registry-estimated RU/day startup banner (spec §9.5)
# ---------------------------------------------------------------------------


def _estimate_ru_per_day(
    cadence: Cadence,
    poll_seconds: float,
    read_cost: int = 1,
    *,
    epoch_state_read_cost: int | None = None,
    runtime: ChainRuntimeConfig = _DEFAULT_RUNTIME,
) -> int:
    """Approximate daily registry cost for the spec §9.5 startup banner.

    The number is a back-of-envelope observation estimate, not a provider
    billing figure. Connection bootstrap, retries, and SDK-internal RPCs are
    excluded. Per-event observables perform two baseline reads for every
    direct-hash notified block (events and chain timestamp); legacy fallback
    also reads the block hash and number. Subscription setup, batched unpins,
    and uncached metadata add occasional reads. Per-block observables poll on
    `poll_seconds`. Per-epoch observables inspect four chain-owned epoch-state
    values per poll and may also read the observable when the marker advances;
    because tempo and owner triggers are mutable, the banner uses the
    conservative upper bound of one observable read per state poll. Anything
    else (Cadence.OTHER) is treated as polled.

    `read_cost` is the registry's per-entry storage-read count: a computed
    observable that hits N storage keys per tick contributes ``N * ticks/day``
    to the daily total.
    """
    # Tests use poll_seconds=0 for fast loops; clamp to "very high" so the
    # banner doesn't divide by zero. Production callers use a positive value.
    if cadence == Cadence.PER_EVENT:
        blocks_per_day = int(_SECONDS_PER_DAY / runtime.block_seconds)
        return blocks_per_day * runtime.event_block_read_cost
    ticks_per_day = _SECONDS_PER_DAY if poll_seconds <= 0 else int(_SECONDS_PER_DAY / poll_seconds)
    state_cost = (
        runtime.epoch_state_read_cost
        if epoch_state_read_cost is None and cadence == Cadence.PER_EPOCH
        else (epoch_state_read_cost or 0)
    )
    return ticks_per_day * (state_cost + read_cost)


def _format_ru_banner(
    spec: WatcherSpec,
    cadence: Cadence,
    read_cost: int = 1,
    *,
    effective_poll: float | None = None,
    epoch_state_read_cost: int | None = None,
    storage_subscription: bool = False,
    storage_subscription_keys: int = 1,
    head_subscription: bool = False,
    runtime: ChainRuntimeConfig = _DEFAULT_RUNTIME,
) -> str:
    """Format the spec §9.5 RU/day startup banner.

    Layout:
        Registry-estimated RU: ~N,NNN/day · cadence <c> · poll Xs · 1 read/tick
            x N RU/read · runtime <bound> · max_ru <bound>

    The banner is informational; the runtime writes one line to stderr at
    `WatcherRunner.run()` startup. Numbers are approximate per spec §9.5.
    `read_cost` comes from the matching `RegistryEntry.read_cost` so the
    banner reflects per-observable cost rather than a fixed 1 RU/read.

    `effective_poll` is the runtime-resolved poll interval (after applying
    cadence-aware defaults). When the caller is the `WatcherRunner`, this
    is the value the loop will actually sleep on. When omitted, the
    raw-spec value is used so direct callers get back the previous shape.
    """
    poll_for_banner = (
        effective_poll
        if effective_poll is not None
        else _resolve_effective_poll(spec.poll_seconds, runtime=runtime)
    )
    runtime_label = (
        "runtime unbounded"
        if spec.max_runtime_seconds is None or spec.max_runtime_seconds <= 0
        else f"runtime {spec.max_runtime_seconds:g}s"
    )
    max_ru_label = (
        "max_ru estimate unset" if spec.max_ru is None else f"max_ru estimate {spec.max_ru}"
    )
    if storage_subscription:
        # One key-construction RPC per key, one subscription RPC, then the
        # initial change-set notification plus its block-number lookup.
        setup_cost = storage_subscription_keys + 3
        return (
            "Registry-estimated RU: change-driven"
            f" · cadence {cadence.value}"
            f" · subscribed storage ({storage_subscription_keys} "
            f"{'key' if storage_subscription_keys == 1 else 'keys'})"
            f" · ~{setup_cost} RU/setup incl. initial snapshot"
            f" · {read_cost} RU/baseline"
            f" · ~{read_cost + 2} RU/change"
            f" · {runtime_label}"
            f" · {max_ru_label}"
            " · excludes retries/uncached SDK metadata"
        )
    if head_subscription:
        blocks_per_day = int(_SECONDS_PER_DAY / runtime.block_seconds)
        state_cost = epoch_state_read_cost or 0
        direct_ru_per_day = blocks_per_day * (read_cost + state_cost)
        if spec.chain != "bt":
            return (
                f"Registry-estimated RU: ~{direct_ru_per_day:,}/day"
                f" · cadence {cadence.value}"
                " · subscribed new heads"
                f" · {read_cost} RU/observable read"
                f" · {runtime_label}"
                f" · {max_ru_label}"
                " · excludes subscription setup/retries"
            )
        legacy_ru_per_day = blocks_per_day * (read_cost + state_cost + 1)
        epoch_label = "" if not state_cost else f" · {state_cost} RU/epoch state"
        observable_label = (
            f"up to {read_cost} RU/observable read"
            if state_cost
            else f"{read_cost} RU/observable read"
        )
        return (
            f"Registry-estimated RU: ~{direct_ru_per_day:,}/day + batched unpins"
            f" (legacy fallback ~{legacy_ru_per_day:,}/day)"
            f" · cadence {cadence.value}"
            " · chainHead direct hashes"
            " · 0 lookup RPC/block"
            f"{epoch_label}"
            f" · {observable_label}"
            f" · {runtime_label}"
            f" · {max_ru_label}"
            " · excludes setup/retries/uncached SDK metadata"
        )

    ru_per_day = _estimate_ru_per_day(
        cadence,
        poll_for_banner,
        read_cost,
        epoch_state_read_cost=epoch_state_read_cost,
        runtime=runtime,
    )
    cadence_label = cadence.value
    poll_label = "subscribed" if cadence == Cadence.PER_EVENT else f"poll {poll_for_banner:g}s"
    epoch_state_label = (
        "" if not epoch_state_read_cost else f" · epoch state {epoch_state_read_cost} RU/tick"
    )
    read_label = (
        f"{runtime.event_block_read_cost}+ baseline RPCs/block"
        if cadence == Cadence.PER_EVENT
        else f"1 read/tick x {read_cost} RU/read"
    )
    ru_label = f"~{ru_per_day:,}/day"
    if cadence == Cadence.PER_EVENT:
        blocks_per_day = int(_SECONDS_PER_DAY / runtime.block_seconds)
        legacy_ru_per_day = blocks_per_day * runtime.event_legacy_block_read_cost
        ru_label = (
            f"~{ru_per_day:,}/day + batched unpins (legacy fallback ~{legacy_ru_per_day:,}/day)"
        )
    return (
        f"Registry-estimated RU: {ru_label}"
        f" · cadence {cadence_label}"
        f" · {poll_label}"
        f" · {read_label}"
        f"{epoch_state_label}"
        f" · {runtime_label}"
        f" · {max_ru_label}"
        " · excludes bootstrap/retries/SDK RPCs"
    )


def _emit_startup_banner(
    spec: WatcherSpec,
    cadence: Cadence,
    *,
    read_cost: int = 1,
    stream: IO[str] | None = None,
    effective_poll: float | None = None,
    epoch_state_read_cost: int | None = None,
    storage_subscription: bool = False,
    storage_subscription_keys: int = 1,
    head_subscription: bool = False,
    runtime: ChainRuntimeConfig = _DEFAULT_RUNTIME,
) -> None:
    """Write the §9.5 banner to stderr (or `stream` when provided).

    Always-on per spec §9.5; the CLI has no `--quiet` flag. Failures are
    swallowed — banner output never breaks the watcher.
    """
    target = stream if stream is not None else sys.stderr
    line = _format_ru_banner(
        spec,
        cadence,
        read_cost,
        effective_poll=effective_poll,
        epoch_state_read_cost=epoch_state_read_cost,
        storage_subscription=storage_subscription,
        storage_subscription_keys=storage_subscription_keys,
        head_subscription=head_subscription,
        runtime=runtime,
    )
    with contextlib.suppress(Exception):
        target.write(line + "\n")
        target.flush()


def _condition_uses_epochs(condition: Condition) -> bool:
    if isinstance(condition, DeltaCondition):
        return condition.window.unit == "epochs"
    if isinstance(condition, LivenessCondition):
        return condition.duration.lower().endswith(("epoch", "epochs"))
    return False


def _with_epoch_state(value: ObservableValue, state: EpochState) -> ObservableValue:
    """Attach pinned epoch context without mutating provider-owned metadata."""
    return replace(
        value,
        meta={
            **value.meta,
            "epoch_netuid": state.netuid,
            "epoch_index": state.epoch_index,
            "last_epoch_block": state.last_epoch_block,
            "next_epoch_start_block": state.next_epoch_start_block,
            "tempo": state.tempo,
        },
    )


def _emit_status(line: str, *, stream: IO[str] | None = None) -> None:
    """Overwrite a single status line on stderr.

    Uses ``\\r`` to update in place so a long-running watcher shows
    liveness without scrolling. Only emits when the target is a TTY —
    redirected stderr stays clean for log capture and CI runs. Failures
    are swallowed (a broken stderr never kills the watcher).

    The line lives on stderr so JSON consumers parsing stdout never see
    it; matches the same channel as the §9.5 startup banner.
    """
    target = stream if stream is not None else sys.stderr
    is_tty = getattr(target, "isatty", lambda: False)()
    if not is_tty:
        return
    with contextlib.suppress(Exception):
        target.write("\r" + line.ljust(_STATUS_LINE_WIDTH))
        target.flush()


# Width to which status lines are padded so a shorter line cleanly
# overwrites a longer prior one without leftover characters.
_STATUS_LINE_WIDTH: int = 80


# Max width for non-numeric values rendered in the heartbeat line. Keeps
# the full status line below one terminal row so the \r overwrite stays
# clean (long values like SS58 addresses would otherwise wrap).
_HEARTBEAT_VALUE_MAX_WIDTH: int = 24


def _format_observed_value(value: object) -> str:
    """Format an observable value for the heartbeat line.

    Floats print at high precision so users can eyeball volatility on
    nearly-static observables (subnet prices that drift in the 9th
    decimal). Non-numeric values fall back to ``str()``; truncated to
    keep the line under one terminal row.
    """
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.10g}"
    text = str(value)
    if len(text) <= _HEARTBEAT_VALUE_MAX_WIDTH:
        return text
    return text[: _HEARTBEAT_VALUE_MAX_WIDTH - 3] + "..."


def _emit_heartbeat(
    block: int,
    rpc_calls: int,
    value: object = None,
    *,
    stream: IO[str] | None = None,
) -> None:
    """Show a successful-poll status line.

    When ``value`` is provided it's appended to the line so users can
    see the observable tick in real time and tune watcher thresholds
    from observed volatility rather than guessing.
    """
    if value is None:
        line = f"polled block {block} · {rpc_calls} RPCs total"
    else:
        line = f"polled block {block} · value={_format_observed_value(value)} · {rpc_calls} RPCs"
    _emit_status(line, stream=stream)


def _emit_waiting_status(reason: str, *, stream: IO[str] | None = None) -> None:
    """Show a 'waiting/backing off' status line so the watcher doesn't appear hung.

    Used when the poll loop is blocked on rate-limit backoff or similar —
    cases where no read has succeeded yet, so ``_emit_heartbeat`` can't
    fire, but the user still needs to see the watcher is alive.
    """
    _emit_status(reason, stream=stream)


def _clear_heartbeat(stream: IO[str] | None = None) -> None:
    """Clear the heartbeat line so a final payload prints on its own row."""
    target = stream if stream is not None else sys.stderr
    is_tty = getattr(target, "isatty", lambda: False)()
    if not is_tty:
        return
    with contextlib.suppress(Exception):
        target.write("\r" + " " * _STATUS_LINE_WIDTH + "\r")
        target.flush()


# ---------------------------------------------------------------------------
# Watcher class
# ---------------------------------------------------------------------------


class WatcherRunner:
    """Drives the provider → primitive → adapter fan-out loop.

    Lifecycle:
      1. Attempt subscription on the provider; fall back to polling.
      2. Per tick: call provider.read_observable (with transient retry), feed
         result to the primitive, dispatch matches to all adapters.
      3. Exit when any adapter's should_exit_after_dispatch is True, or when
         the runtime deadline / registry-estimated RU guard is exhausted.
      4. SIGTERM/SIGINT causes clean shutdown via asyncio cancellation.
    """

    def __init__(
        self,
        spec: WatcherSpec,
        *,
        entry: RegistryEntry,
        provider: ChainProvider,
        primitive: Primitive[PrimitiveInput],
        adapters: list[Adapter],
        budget: Budget,
        banner_stream: IO[str] | None = None,
        runtime: ChainRuntimeConfig | None = None,
    ) -> None:
        if not adapters:
            raise ValueError("WatcherRunner requires at least one adapter")
        if entry.chain != spec.chain:
            raise ValueError(
                f"observable chain {entry.chain!r} does not match watcher chain {spec.chain!r}"
            )
        self._spec = spec
        self._entry = entry
        self._provider = provider
        self._primitive = primitive
        self._adapters = adapters
        self._budget = budget
        self._banner_stream = banner_stream
        self._runtime = runtime or backend_for(spec.chain).runtime
        self._match_count = 0
        self._shutdown_event = asyncio.Event()
        # Effective poll is resolved once per run from spec + cadence in
        # ``run()``; storing it here keeps the loops free of cadence checks.
        self._effective_poll = self._runtime.block_seconds

    @property
    def match_count(self) -> int:
        return self._match_count

    def _should_exit_after_dispatch(self) -> bool:
        return any(a.should_exit_after_dispatch for a in self._adapters)

    async def _dispatch(self, payload: Payload) -> None:
        """Fan a payload out to all adapters without blocking the event loop.

        Adapter implementations are synchronous. Some adapters (notably
        AppriseAdapter) make
        blocking HTTP calls inside ``dispatch``; running them on the event-loop
        thread stalls the watcher's polling/subscription loop. ``asyncio.to_thread``
        offloads each dispatch to the default executor so blocking sinks
        (apprise) and non-blocking sinks (file/stream) coexist without one
        starving the other.

        Clears the in-place stderr heartbeat first so the payload prints on
        a fresh row rather than running on after the partial heartbeat
        line. Cursor moves to the next row via a trailing newline so
        stdout payload output starts cleanly.
        """
        _clear_heartbeat()
        for adapter in self._adapters:
            try:
                await asyncio.to_thread(adapter.dispatch, payload)
            except Exception:
                _log.exception("Adapter %r raised during dispatch", adapter.name)

    async def _close_all(self) -> None:
        for adapter in self._adapters:
            try:
                await asyncio.to_thread(adapter.close)
            except Exception:
                _log.exception("Adapter %r raised during close", adapter.name)

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._shutdown_event.set)
            except (NotImplementedError, RuntimeError):
                # Windows event loops do not implement POSIX signal handlers,
                # and non-main-thread loops cannot install them. The watcher
                # remains cancellable through its normal task/runtime controls.
                return

    def _remove_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(sig)

    async def run(self) -> int:
        """Drive the watcher. Returns process exit code."""
        self._install_signal_handlers()
        # WatcherSpec.path_params is the explicit param map; the registry
        # entry validates names and substitutes them into the template.
        # Missing or unknown params are CLI-input errors, not chainwake bugs.
        # `KeyError(msg).args[0]` gives the original message without the repr
        # quotes that `str(KeyError(msg))` adds around it.
        try:
            path = self._entry.render_path(self._spec.path_params)
        except KeyError as exc:
            message = exc.args[0] if exc.args else "invalid path params"
            raise UserError(str(message), reason="invalid_path_params") from exc
        rate_guard = RateLimitGuard()

        # Spec §9.5: print an estimated RU/day banner to stderr at startup.
        # Observation policy is registry-owned so the CLI, MCP schema, runtime,
        # and provider cannot silently select different monitoring methods.
        cadence = self._entry.observation_policy.natural_cadence
        driver = self._observation_driver()
        banner_driver = self._available_driver(driver)
        storage_subscription = banner_driver == ObservationDriver.STORAGE_CHANGE
        head_subscription = (
            self._spec.poll_seconds is None
            and self._head_provider() is not None
            and banner_driver
            in {
                ObservationDriver.BEST_HEAD,
                ObservationDriver.SUBNET_EPOCH,
                ObservationDriver.TX_STATUS,
            }
        )
        epoch_provider = self._provider if isinstance(self._provider, EpochProvider) else None
        epoch_netuid = (
            epoch_provider.epoch_netuid_for(path, self._spec.read_args)
            if epoch_provider is not None
            else None
        )
        uses_epoch_state = epoch_netuid is not None and (
            cadence == Cadence.PER_EPOCH or _condition_uses_epochs(self._spec.condition)
        )
        # Resolve the effective poll interval once: respect the user's
        # override, otherwise fall back to the chain's natural block time
        # so per-block watchers don't burn 11/12 polls on a 12-second-block
        # chain (Bittensor mainnet). The banner reports this resolved value
        # so users see what the loop will actually do.
        self._effective_poll = _resolve_effective_poll(
            self._spec.poll_seconds,
            policy_poll_seconds=self._entry.observation_policy.default_poll_seconds,
            runtime=self._runtime,
        )
        _emit_startup_banner(
            self._spec,
            cadence,
            read_cost=self._entry.read_cost,
            stream=self._banner_stream,
            effective_poll=self._effective_poll,
            epoch_state_read_cost=(self._runtime.epoch_state_read_cost if uses_epoch_state else 0),
            storage_subscription=storage_subscription,
            storage_subscription_keys=len(self._entry.observation_policy.storage_bindings),
            head_subscription=head_subscription,
            runtime=self._runtime,
        )

        try:
            return await self._run_loop(path, rate_guard, driver=driver)
        except asyncio.CancelledError:
            # On event loops without add_signal_handler (notably Windows),
            # asyncio.run translates Ctrl+C into cancellation of the main
            # task. Preserve the one-payload exit contract instead of leaking
            # CancelledError/KeyboardInterrupt without watcher context.
            self._shutdown_event.set()
            return await self._emit_stopped()
        finally:
            self._remove_signal_handlers()
            await self._close_all()

    async def _run_loop(
        self,
        path: str,
        rate_guard: RateLimitGuard,
        *,
        driver: ObservationDriver,
    ) -> int:
        if driver == ObservationDriver.EVENT_STREAM:
            loop = self._subscription_loop
        elif driver == ObservationDriver.STORAGE_CHANGE:
            loop = self._storage_subscription_loop
        elif driver == ObservationDriver.BEST_HEAD and self._spec.poll_seconds is None:
            loop = self._head_subscription_loop
        elif driver == ObservationDriver.TX_STATUS and self._spec.poll_seconds is None:
            loop = (
                self._head_subscription_loop
                if self._head_provider() is not None
                else self._poll_loop
            )
        elif driver == ObservationDriver.SUBNET_EPOCH:
            loop = (
                self._per_epoch_head_loop
                if self._spec.poll_seconds is None
                else self._per_epoch_loop
            )
        else:
            loop = self._poll_loop
        return await loop(path, rate_guard)

    def _observation_driver(self) -> ObservationDriver:
        """Resolve the registry policy for this watch's primitive."""
        window_unit = (
            self._spec.condition.window.unit
            if isinstance(self._spec.condition, DeltaCondition)
            else None
        )
        return self._entry.observation_policy.driver_for(
            self._spec.primitive_name,
            window_unit=window_unit,
        )

    def _available_driver(self, driver: ObservationDriver) -> ObservationDriver:
        """Resolve capability-dependent fallbacks for truthful startup output."""
        if driver == ObservationDriver.STORAGE_CHANGE and self._storage_provider() is None:
            return self._entry.observation_policy.fallback_driver or driver
        if driver == ObservationDriver.BEST_HEAD and self._head_provider() is None:
            return ObservationDriver.TIMER_POLL
        if driver == ObservationDriver.TX_STATUS and self._head_provider() is None:
            return ObservationDriver.TIMER_POLL
        if driver == ObservationDriver.SUBNET_EPOCH and self._epoch_provider() is None:
            return self._entry.observation_policy.fallback_driver or ObservationDriver.TIMER_POLL
        if (
            driver == ObservationDriver.EVENT_STREAM
            and self._event_provider() is None
            and self._entry.type != "event"
        ):
            return ObservationDriver.TIMER_POLL
        return driver

    def _epoch_provider(self) -> EpochProvider | None:
        if isinstance(self._provider, EpochProvider):
            return self._provider
        return None

    def _head_provider(self) -> HeadSubscriptionProvider | None:
        if isinstance(self._provider, HeadSubscriptionProvider):
            return self._provider
        return None

    def _storage_provider(self) -> StorageSubscriptionProvider | None:
        if isinstance(self._provider, StorageSubscriptionProvider):
            return self._provider
        return None

    def _event_provider(self) -> EventSubscriptionProvider | None:
        if isinstance(self._provider, EventSubscriptionProvider):
            return self._provider
        return None

    def _prefers_storage_subscription(self, cadence: Cadence) -> bool:
        """Compatibility predicate backed by the registry observation policy."""
        del cadence
        return self._observation_driver() == ObservationDriver.STORAGE_CHANGE

    def _prefers_head_subscription(self, cadence: Cadence) -> bool:
        """Compatibility predicate for best-head scheduling."""
        del cadence
        return (
            self._observation_driver() in {ObservationDriver.BEST_HEAD, ObservationDriver.TX_STATUS}
            and self._spec.poll_seconds is None
        )

    def _prefers_epoch_head_subscription(self, cadence: Cadence) -> bool:
        """Compatibility predicate for epoch checks driven by best heads."""
        del cadence
        return (
            self._observation_driver() == ObservationDriver.SUBNET_EPOCH
            and self._spec.poll_seconds is None
        )

    async def _per_epoch_loop(  # noqa: PLR0911
        self,
        path: str,
        rate_guard: RateLimitGuard,
    ) -> int:
        """Read once when the subnet's chain-owned epoch marker advances.

        Tempo changes re-anchor the schedule and an owner can trigger an epoch
        before its predicted start. The runtime therefore checks
        ``SubnetEpochIndex``/``LastEpochBlock`` once per block instead of
        deriving epochs with block-number modulo arithmetic.

        A path with no single governing subnet falls back to ordinary polling.
        This costs more reads but cannot miss a change behind a fake global
        epoch.
        """
        epoch_provider = self._epoch_provider()
        if epoch_provider is None:
            _log.info("Epoch capability unavailable for %s; falling back to polling", path)
            return await self._poll_loop(path, rate_guard)
        netuid = epoch_provider.epoch_netuid_for(path, self._spec.read_args)
        if netuid is None:
            return await self._poll_loop(path, rate_guard)

        read_with_retry = with_transient_retry(
            self._provider.read_observable,
            max_delay_seconds=self._spec.max_runtime_seconds,
        )
        epoch_with_retry = with_transient_retry(
            epoch_provider.get_epoch_state,
            max_delay_seconds=self._spec.max_runtime_seconds,
        )
        last_marker: tuple[int, int] | None = None
        while not self._shutdown_event.is_set():
            if self._budget.is_runtime_exceeded():
                return await self._emit_timeout()

            state, early_exit = await self._fetch_epoch_state(
                netuid,
                epoch_with_retry,
                rate_guard,
            )
            if early_exit is not None:
                return early_exit
            if state is None:
                continue

            marker = (state.epoch_index, state.last_epoch_block)
            if marker != last_marker:
                value, early_exit = await self._fetch(
                    path,
                    read_with_retry,
                    rate_guard,
                    at_block=BlockRef(number=state.block, hash=state.block_hash),
                )
                if early_exit is not None:
                    return early_exit
                if value is None:
                    continue
                value = _with_epoch_state(value, state)
                exit_code = await self._evaluate_and_dispatch(value)
                if exit_code is not None:
                    return exit_code
                # A chain epoch is consumed only after its observation was
                # successfully fetched and evaluated.  A rate-limit retry
                # therefore sees the same marker again.
                last_marker = marker

            await self._interruptible_sleep(self._effective_poll)

        return await self._final_loop_exit()

    async def _per_epoch_head_loop(  # noqa: PLR0911, PLR0912, PLR0915
        self,
        path: str,
        rate_guard: RateLimitGuard,
    ) -> int:
        """Inspect the subnet epoch marker at each notified best block."""
        epoch_provider = self._epoch_provider()
        if epoch_provider is None:
            return await self._head_subscription_loop(path, rate_guard)
        head_provider = self._head_provider()
        if head_provider is None:
            return await self._per_epoch_loop(path, rate_guard)
        netuid = epoch_provider.epoch_netuid_for(path, self._spec.read_args)
        if netuid is None:
            return await self._head_subscription_loop(path, rate_guard)

        read_with_retry = with_transient_retry(
            self._provider.read_observable,
            max_delay_seconds=self._spec.max_runtime_seconds,
        )
        epoch_with_retry = with_transient_retry(
            epoch_provider.get_epoch_state,
            max_delay_seconds=self._spec.max_runtime_seconds,
        )
        last_marker: tuple[int, int] | None = None
        reconnect_attempt = 0

        while True:
            if self._shutdown_event.is_set():
                return await self._emit_stopped()
            if self._budget.is_runtime_exceeded():
                return await self._emit_timeout()

            stream: AsyncIterator[BlockRef] | None = None
            pending_head: asyncio.Task[BlockRef] | None = None
            try:
                stream = head_provider.subscribe_heads(
                    charge_rpc=self._charge_subscription_rpc,
                )
                iterator = stream.__aiter__()
                pending_head = asyncio.ensure_future(anext(iterator))

                while last_marker is None:
                    state, early_exit = await self._fetch_epoch_state(
                        netuid,
                        epoch_with_retry,
                        rate_guard,
                    )
                    if early_exit is not None:
                        return early_exit
                    if state is None:
                        continue
                    value, early_exit = await self._fetch(
                        path,
                        read_with_retry,
                        rate_guard,
                        at_block=BlockRef(number=state.block, hash=state.block_hash),
                    )
                    if early_exit is not None:
                        return early_exit
                    if value is None:
                        continue
                    value = _with_epoch_state(value, state)
                    _emit_heartbeat(value.block, self._budget.rpc_calls, value.value)
                    exit_code = await self._evaluate_and_dispatch(value)
                    if exit_code is not None:
                        _clear_heartbeat()
                        return exit_code
                    last_marker = (state.epoch_index, state.last_epoch_block)
                    _emit_waiting_status(f"watching {path} · awaiting epoch blocks…")

                while True:
                    try:
                        head = await self._await_subscription_task(pending_head)
                    except _SubscriptionDeadlineReachedError:
                        return await self._emit_timeout()
                    except _SubscriptionShutdownError:
                        return await self._emit_stopped()
                    except StopAsyncIteration as exc:
                        raise SubscriptionFailedError(
                            "new-head subscription ended unexpectedly"
                        ) from exc
                    finally:
                        pending_head = None

                    reconnect_attempt = 0
                    rate_guard.reset()
                    if head.number is None or head.hash is None:
                        raise SubscriptionFailedError(
                            "new-head subscription yielded an unpinned block reference"
                        )
                    state, early_exit = await self._fetch_epoch_state(
                        netuid,
                        epoch_with_retry,
                        rate_guard,
                        at_block=head,
                    )
                    if early_exit is not None:
                        return early_exit
                    if state is None:
                        pending_head = asyncio.ensure_future(anext(iterator))
                        continue

                    marker = (state.epoch_index, state.last_epoch_block)
                    if marker != last_marker:
                        while True:
                            value, early_exit = await self._fetch(
                                path,
                                read_with_retry,
                                rate_guard,
                                at_block=BlockRef(number=state.block, hash=state.block_hash),
                            )
                            if early_exit is not None:
                                return early_exit
                            if value is not None:
                                break
                        value = _with_epoch_state(value, state)
                        _emit_heartbeat(value.block, self._budget.rpc_calls, value.value)
                        exit_code = await self._evaluate_and_dispatch(value)
                        if exit_code is not None:
                            _clear_heartbeat()
                            return exit_code
                        last_marker = marker
                    pending_head = asyncio.ensure_future(anext(iterator))
            except NotImplementedError:
                _log.info(
                    "New-head subscription unavailable for %s; falling back to epoch polling",
                    path,
                )
                return await self._per_epoch_loop(path, rate_guard)
            except RateLimitError as exc:
                try:
                    retry = await self._await_subscription_backoff(rate_guard.handle(exc))
                except _SubscriptionDeadlineReachedError:
                    return await self._emit_timeout()
                except _SubscriptionShutdownError:
                    return await self._emit_stopped()
                if not retry:
                    return await self._handle_provider_exception(exc)
            except (
                SubscriptionFailedError,
                RPCUnreachableError,
                ConnectionError,
                OSError,
                TimeoutError,
            ) as exc:
                reconnect_attempt += 1
                delay = min(0.5 * (2 ** (reconnect_attempt - 1)), 30.0)
                _log.warning(
                    "Epoch head subscription interrupted; reconnecting after %.1fs "
                    "(attempt %d): %s",
                    delay,
                    reconnect_attempt,
                    exc,
                )
                await self._interruptible_sleep(delay)
            except Exception as exc:
                return await self._handle_provider_exception(exc)
            finally:
                if pending_head is not None:
                    if not pending_head.done():
                        pending_head.cancel()
                    await asyncio.gather(pending_head, return_exceptions=True)
                await self._close_subscription_stream(stream)

    async def _evaluate_and_dispatch(self, value: ObservableValue) -> int | None:
        """Run the primitive on a fresh value; dispatch on Match.

        Shared between the per-block and per-epoch loops for evaluate-after-
        fetch semantics — the per-epoch loop already gates on epoch
        transitions, so no second gate is needed here.
        """
        outcome = self._primitive.evaluate(value)
        if isinstance(outcome, Match):
            payload = _build_match_payload(self._spec, outcome, self._budget)
            await self._dispatch(payload)
            self._match_count += 1
            if self._should_exit_after_dispatch():
                return 0
        return None

    async def _final_loop_exit(self) -> int:
        if self._shutdown_event.is_set():
            return await self._emit_stopped()
        if self._match_count > 0:
            return 0
        return await self._emit_timeout()

    async def _poll_loop(self, path: str, rate_guard: RateLimitGuard) -> int:
        read_with_retry = with_transient_retry(
            self._provider.read_observable,
            max_delay_seconds=self._spec.max_runtime_seconds,
        )
        epoch_netuid: int | None = None
        epoch_with_retry: Callable[..., Awaitable[EpochState]] | None = None
        if _condition_uses_epochs(self._spec.condition):
            epoch_provider = self._epoch_provider()
            if epoch_provider is None:
                raise UserError(
                    f"{self._spec.chain!r} does not provide epoch context",
                    reason="epoch_context_unavailable",
                )
            epoch_netuid = epoch_provider.epoch_netuid_for(path, self._spec.read_args)
            if epoch_netuid is None:
                raise UserError(
                    f"{path!r} has no single subnet epoch; use a time or block window",
                    reason="epoch_context_unavailable",
                )
            epoch_with_retry = with_transient_retry(
                epoch_provider.get_epoch_state,
                max_delay_seconds=self._spec.max_runtime_seconds,
            )

        # User-visible alive signal between banner and first-read result —
        # if the very first read trips a rate limit, the heartbeat (which
        # only fires on success) wouldn't otherwise show up at all. Keep
        # the message short so it fits one line for clean \r overwrite.
        _emit_waiting_status(f"watching {path} · first poll…")

        while not self._shutdown_event.is_set():
            if self._budget.is_runtime_exceeded():
                _clear_heartbeat()
                return await self._emit_timeout()

            exit_code = await self._tick(
                path,
                read_with_retry,
                rate_guard,
                epoch_netuid=epoch_netuid,
                epoch_with_retry=epoch_with_retry,
            )
            if exit_code is not None:
                return exit_code

            await self._interruptible_sleep(self._effective_poll)

        _clear_heartbeat()
        return await self._final_loop_exit()

    async def _tick(
        self,
        path: str,
        read_with_retry: Callable[..., Awaitable[ObservableValue]],
        rate_guard: RateLimitGuard,
        *,
        epoch_netuid: int | None = None,
        epoch_with_retry: Callable[..., Awaitable[EpochState]] | None = None,
    ) -> int | None:
        """Perform one fetch-evaluate-dispatch cycle.

        Returns an exit code if the loop should terminate, or None to continue.
        """
        value, early_exit = await self._fetch(path, read_with_retry, rate_guard)
        if early_exit is not None:
            _clear_heartbeat()
            return early_exit
        if value is None:
            return None
        if epoch_netuid is not None and epoch_with_retry is not None:
            state, early_exit = await self._fetch_epoch_state(
                epoch_netuid,
                epoch_with_retry,
                rate_guard,
                at_block=BlockRef(number=value.block, hash=value.block_hash),
            )
            if early_exit is not None:
                return early_exit
            if state is None:
                return None
            value = _with_epoch_state(value, state)
        _emit_heartbeat(value.block, self._budget.rpc_calls, value.value)
        exit_code = await self._evaluate_and_dispatch(value)
        if exit_code is not None:
            _clear_heartbeat()
        return exit_code

    async def _fetch(
        self,
        path: str,
        read_with_retry: Callable[..., Awaitable[ObservableValue]],
        rate_guard: RateLimitGuard,
        *,
        at_block: BlockRef | None = None,
    ) -> tuple[ObservableValue | None, int | None]:
        """Call provider.read_observable; handle all error cases.

        Returns (value, None) on success, (None, exit_code) on terminal error,
        or (None, None) when a rate-limit backoff was applied (caller retries).
        """
        try:
            read_cost = self._entry.read_cost
            self._budget.reserve_ru(read_cost)
            read_args = dict(self._spec.read_args)
            if isinstance(self._spec.condition, LivenessCondition):
                unit, _magnitude = parse_duration_components(self._spec.condition.duration)
                read_args["_liveness_unit"] = unit
            try:
                if at_block is None:
                    value = await self._call_with_deadline(lambda: read_with_retry(path, read_args))
                else:
                    value = await self._call_with_deadline(
                        lambda: read_with_retry(path, read_args, at_block)
                    )
            except BaseException:
                self._budget.release_ru_reservation(read_cost)
                raise
            self._budget.charge_reserved_rpc_call(ru_cost=read_cost)
            rate_guard.reset()
            return value, None
        except _WatcherDeadlineReachedError:
            return None, await self._emit_timeout()
        except HeadUnavailableError:
            raise
        except RateLimitError as exc:
            # Short message so the line doesn't wrap and break the \r
            # in-place overwrite. Detail is in the eventual provider_error
            # payload + the always-on log warning from RateLimitGuard.
            _emit_waiting_status(
                "rate-limited · backing off (wait, poll less, or sign up for higher limits)"
            )
            retry = await rate_guard.handle(exc, sleep=self._interruptible_sleep)
            if not retry:
                return None, await self._handle_provider_exception(exc)
            return None, None
        except Exception as exc:
            return None, await self._handle_provider_exception(exc)

    async def _attach_epoch_state(
        self,
        value: ObservableValue,
        epoch_netuid: int | None,
        epoch_with_retry: Callable[..., Awaitable[EpochState]] | None,
        rate_guard: RateLimitGuard,
    ) -> tuple[ObservableValue | None, int | None]:
        """Attach epoch metadata to one pinned observation when required."""
        if epoch_netuid is None or epoch_with_retry is None:
            return value, None
        state, early_exit = await self._fetch_epoch_state(
            epoch_netuid,
            epoch_with_retry,
            rate_guard,
            at_block=BlockRef(number=value.block, hash=value.block_hash),
        )
        if early_exit is not None:
            return None, early_exit
        if state is None:
            return None, None
        return _with_epoch_state(value, state), None

    async def _fetch_epoch_state(
        self,
        netuid: int,
        read_with_retry: Callable[..., Awaitable[EpochState]],
        rate_guard: RateLimitGuard,
        *,
        at_block: BlockRef | None = None,
    ) -> tuple[EpochState | None, int | None]:
        """Read epoch state with the observable fetch retry/error policy."""
        try:
            epoch_cost = self._runtime.epoch_state_read_cost
            self._budget.reserve_ru(epoch_cost)
            try:
                state = await self._call_with_deadline(lambda: read_with_retry(netuid, at_block))
            except BaseException:
                self._budget.release_ru_reservation(epoch_cost)
                raise
            self._budget.charge_reserved_rpc_call(ru_cost=epoch_cost)
            rate_guard.reset()
            return state, None
        except _WatcherDeadlineReachedError:
            return None, await self._emit_timeout()
        except RateLimitError as exc:
            _emit_waiting_status(
                "rate-limited · backing off (wait, poll less, or sign up for higher limits)"
            )
            retry = await rate_guard.handle(exc, sleep=self._interruptible_sleep)
            if not retry:
                return None, await self._handle_provider_exception(exc)
            return None, None
        except Exception as exc:
            return None, await self._handle_provider_exception(exc)

    async def _head_subscription_loop(  # noqa: PLR0911, PLR0912, PLR0915
        self,
        path: str,
        rate_guard: RateLimitGuard,
    ) -> int:
        """Evaluate once at startup, then once for each notified best block."""
        head_provider = self._head_provider()
        if head_provider is None:
            _log.info("New-head subscription unavailable for %s; falling back to polling", path)
            return await self._poll_loop(path, rate_guard)
        read_with_retry = with_transient_retry(
            self._provider.read_observable,
            max_delay_seconds=self._spec.max_runtime_seconds,
        )
        epoch_netuid: int | None = None
        epoch_with_retry: Callable[..., Awaitable[EpochState]] | None = None
        if _condition_uses_epochs(self._spec.condition):
            epoch_provider = self._epoch_provider()
            if epoch_provider is None:
                raise UserError(
                    f"{self._spec.chain!r} does not provide epoch context",
                    reason="epoch_context_unavailable",
                )
            epoch_netuid = epoch_provider.epoch_netuid_for(path, self._spec.read_args)
            if epoch_netuid is None:
                raise UserError(
                    f"{path!r} has no single subnet epoch; use a time or block window",
                    reason="epoch_context_unavailable",
                )
            epoch_with_retry = with_transient_retry(
                epoch_provider.get_epoch_state,
                max_delay_seconds=self._spec.max_runtime_seconds,
            )
        baseline_done = False
        last_block = -1
        last_block_hash: str | None = None
        reconnect_attempt = 0

        while True:
            if self._shutdown_event.is_set():
                return await self._emit_stopped()
            if self._budget.is_runtime_exceeded():
                return await self._emit_timeout()

            stream: AsyncIterator[BlockRef] | None = None
            pending_head: asyncio.Task[BlockRef] | None = None
            try:
                stream = head_provider.subscribe_heads(
                    charge_rpc=self._charge_subscription_rpc,
                )
                iterator = stream.__aiter__()
                # Attach before the baseline read so a block produced during
                # bootstrap remains queued for evaluation.
                pending_head = asyncio.ensure_future(anext(iterator))

                while not baseline_done:
                    value, early_exit = await self._fetch(path, read_with_retry, rate_guard)
                    if early_exit is not None:
                        return early_exit
                    if value is None:
                        continue
                    value, early_exit = await self._attach_epoch_state(
                        value,
                        epoch_netuid,
                        epoch_with_retry,
                        rate_guard,
                    )
                    if early_exit is not None:
                        return early_exit
                    if value is None:
                        continue
                    _emit_heartbeat(value.block, self._budget.rpc_calls, value.value)
                    exit_code = await self._evaluate_and_dispatch(value)
                    if exit_code is not None:
                        _clear_heartbeat()
                        return exit_code
                    baseline_done = True
                    last_block = value.block
                    last_block_hash = value.block_hash
                    _emit_waiting_status(f"watching {path} · awaiting new blocks…")

                while True:
                    try:
                        head = await self._await_subscription_task(pending_head)
                    except _SubscriptionDeadlineReachedError:
                        return await self._emit_timeout()
                    except _SubscriptionShutdownError:
                        return await self._emit_stopped()
                    except StopAsyncIteration as exc:
                        raise SubscriptionFailedError(
                            "new-head subscription ended unexpectedly"
                        ) from exc
                    finally:
                        pending_head = None

                    reconnect_attempt = 0
                    rate_guard.reset()
                    if head.number is None or head.hash is None:
                        raise SubscriptionFailedError(
                            "new-head subscription yielded an unpinned block reference"
                        )
                    if head.number < last_block or (
                        head.number == last_block and head.hash == last_block_hash
                    ):
                        pending_head = asyncio.ensure_future(anext(iterator))
                        continue

                    skip_head = False
                    while True:
                        try:
                            value, early_exit = await self._fetch(
                                path,
                                read_with_retry,
                                rate_guard,
                                at_block=head,
                            )
                        except HeadUnavailableError:
                            _log.info(
                                "Notified head %s is unavailable; awaiting the next head",
                                head.hash,
                            )
                            skip_head = True
                            break
                        if early_exit is not None:
                            return early_exit
                        if value is not None:
                            break
                    if skip_head:
                        pending_head = asyncio.ensure_future(anext(iterator))
                        continue
                    if value is None:
                        raise RuntimeError(
                            "observable fetch completed without a value or early exit"
                        )
                    value, early_exit = await self._attach_epoch_state(
                        value,
                        epoch_netuid,
                        epoch_with_retry,
                        rate_guard,
                    )
                    if early_exit is not None:
                        return early_exit
                    if value is None:
                        pending_head = asyncio.ensure_future(anext(iterator))
                        continue

                    last_block = value.block
                    last_block_hash = value.block_hash
                    _emit_heartbeat(value.block, self._budget.rpc_calls, value.value)
                    exit_code = await self._evaluate_and_dispatch(value)
                    if exit_code is not None:
                        _clear_heartbeat()
                        return exit_code
                    pending_head = asyncio.ensure_future(anext(iterator))
            except NotImplementedError:
                _log.info("New-head subscription unavailable for %s; falling back to polling", path)
                return await self._poll_loop(path, rate_guard)
            except RateLimitError as exc:
                try:
                    retry = await self._await_subscription_backoff(rate_guard.handle(exc))
                except _SubscriptionDeadlineReachedError:
                    return await self._emit_timeout()
                except _SubscriptionShutdownError:
                    return await self._emit_stopped()
                if not retry:
                    return await self._handle_provider_exception(exc)
            except (
                SubscriptionFailedError,
                RPCUnreachableError,
                ConnectionError,
                OSError,
                TimeoutError,
            ) as exc:
                reconnect_attempt += 1
                delay = min(0.5 * (2 ** (reconnect_attempt - 1)), 30.0)
                _log.warning(
                    "New-head subscription interrupted; reconnecting after %.1fs (attempt %d): %s",
                    delay,
                    reconnect_attempt,
                    exc,
                )
                await self._interruptible_sleep(delay)
            except Exception as exc:
                return await self._handle_provider_exception(exc)
            finally:
                if pending_head is not None:
                    if not pending_head.done():
                        pending_head.cancel()
                    await asyncio.gather(pending_head, return_exceptions=True)
                await self._close_subscription_stream(stream)

    async def _storage_subscription_loop(  # noqa: PLR0911, PLR0912, PLR0915
        self,
        path: str,
        rate_guard: RateLimitGuard,
    ) -> int:
        """Evaluate a baseline, then re-read only when a backing key changes.

        Storage callback values are transport-shaped SCALE values, not public
        Chainwake observables. Each notification is therefore only a wake
        signal: the normal provider reader runs at the notified block so
        decoding, entity checks, timestamps, RU guards, and output payloads
        remain identical to polling.
        """
        storage_provider = self._storage_provider()
        if storage_provider is None:
            return await self._storage_fallback(path, rate_guard)
        read_with_retry = with_transient_retry(
            self._provider.read_observable,
            max_delay_seconds=self._spec.max_runtime_seconds,
        )
        baseline_done = False
        last_block = -1
        last_block_hash: str | None = None
        reconnect_attempt = 0

        while True:
            if self._shutdown_event.is_set():
                return await self._emit_stopped()
            if self._budget.is_runtime_exceeded():
                return await self._emit_timeout()

            stream: AsyncIterator[StorageUpdate] | None = None
            pending_update: asyncio.Task[StorageUpdate] | None = None
            try:
                stream = storage_provider.subscribe_storage(
                    path,
                    charge_rpc=self._charge_subscription_rpc,
                )
                iterator = stream.__aiter__()
                # Start attaching the subscription before the baseline read.
                # Notifications that race with the read remain queued.
                pending_update = asyncio.ensure_future(anext(iterator))

                while not baseline_done:
                    value, early_exit = await self._fetch(path, read_with_retry, rate_guard)
                    if early_exit is not None:
                        return early_exit
                    if value is None:
                        continue
                    _emit_heartbeat(value.block, self._budget.rpc_calls, value.value)
                    exit_code = await self._evaluate_and_dispatch(value)
                    if exit_code is not None:
                        _clear_heartbeat()
                        return exit_code
                    baseline_done = True
                    last_block = value.block
                    last_block_hash = value.block_hash
                    _emit_waiting_status(f"watching {path} · subscribed…")

                while True:
                    try:
                        update = await self._await_subscription_task(pending_update)
                    except _SubscriptionDeadlineReachedError:
                        return await self._emit_timeout()
                    except _SubscriptionShutdownError:
                        return await self._emit_stopped()
                    except StopAsyncIteration as exc:
                        raise SubscriptionFailedError(
                            "storage subscription ended unexpectedly"
                        ) from exc
                    finally:
                        pending_update = None

                    reconnect_attempt = 0
                    rate_guard.reset()
                    if update.path != path:
                        raise SubscriptionFailedError(
                            f"storage subscription for {path!r} yielded {update.path!r}"
                        )

                    # Most state_subscribeStorage implementations emit an
                    # initial snapshot. The baseline already covered it.
                    if update.block < last_block or (
                        update.block == last_block and update.block_hash == last_block_hash
                    ):
                        pending_update = asyncio.ensure_future(anext(iterator))
                        continue

                    while True:
                        value, early_exit = await self._fetch(
                            path,
                            read_with_retry,
                            rate_guard,
                            at_block=BlockRef(number=update.block, hash=update.block_hash),
                        )
                        if early_exit is not None:
                            return early_exit
                        if value is not None:
                            break

                    last_block = value.block
                    last_block_hash = value.block_hash
                    _emit_heartbeat(value.block, self._budget.rpc_calls, value.value)
                    exit_code = await self._evaluate_and_dispatch(value)
                    if exit_code is not None:
                        _clear_heartbeat()
                        return exit_code
                    pending_update = asyncio.ensure_future(anext(iterator))
            except NotImplementedError:
                return await self._storage_fallback(path, rate_guard)
            except RateLimitError as exc:
                try:
                    retry = await self._await_subscription_backoff(rate_guard.handle(exc))
                except _SubscriptionDeadlineReachedError:
                    return await self._emit_timeout()
                except _SubscriptionShutdownError:
                    return await self._emit_stopped()
                if not retry:
                    return await self._handle_provider_exception(exc)
            except (
                SubscriptionFailedError,
                RPCUnreachableError,
                ConnectionError,
                OSError,
                TimeoutError,
            ) as exc:
                reconnect_attempt += 1
                delay = min(0.5 * (2 ** (reconnect_attempt - 1)), 30.0)
                _log.warning(
                    "Storage subscription interrupted; reconnecting after %.1fs (attempt %d): %s",
                    delay,
                    reconnect_attempt,
                    exc,
                )
                await self._interruptible_sleep(delay)
            except Exception as exc:
                return await self._handle_provider_exception(exc)
            finally:
                if pending_update is not None:
                    if not pending_update.done():
                        pending_update.cancel()
                    await asyncio.gather(pending_update, return_exceptions=True)
                await self._close_subscription_stream(stream)

    async def _storage_fallback(self, path: str, rate_guard: RateLimitGuard) -> int:
        fallback = self._entry.observation_policy.fallback_driver
        if fallback is None:
            return await self._handle_provider_exception(
                SubscriptionFailedError(
                    f"storage subscription unavailable for {path!r} "
                    "and its registry policy has no fallback driver"
                )
            )
        _log.info("Storage subscription unavailable for %s; using %s", path, fallback)
        return await self._run_loop(path, rate_guard, driver=fallback)

    async def _subscription_loop(  # noqa: PLR0911, PLR0912
        self, path: str, rate_guard: RateLimitGuard
    ) -> int:
        """Drive via subscribe_events for event-cadence observables.

        Non-event observables may fall back to polling when a subscription is
        unavailable. Event observables never poll: transient failures recreate
        the subscription with backoff, while terminal errors produce the
        corresponding exit payload.

        The provider receives a synchronous budget callback and invokes it
        immediately before every visible subscription/per-block RPC. This
        applies the registry-estimated ``--max-ru`` guard inside the callback
        bridge; it is not transport-level provider billing metering.
        """
        event_filter = self._subscription_event_filter()
        event_provider = self._event_provider()
        if event_provider is None:
            if self._entry.type == "event":
                return await self._handle_provider_exception(
                    SubscriptionFailedError(
                        f"event subscription unavailable for chain {self._spec.chain!r}"
                    )
                )
            return await self._poll_loop(path, rate_guard)

        reconnect_attempt = 0
        while True:
            if self._shutdown_event.is_set():
                return await self._emit_stopped()
            if self._budget.is_runtime_exceeded():
                return await self._emit_timeout()

            stream: AsyncIterator[Event] | None = None
            try:
                stream = event_provider.subscribe_events(
                    event_filter,
                    charge_rpc=self._charge_subscription_rpc,
                )
                iterator = stream.__aiter__()
                while True:
                    try:
                        event = await self._next_subscription_event(iterator)
                    except _SubscriptionDeadlineReachedError:
                        return await self._emit_timeout()
                    except _SubscriptionShutdownError:
                        return await self._emit_stopped()
                    except StopAsyncIteration as exc:
                        raise SubscriptionFailedError(
                            "event subscription ended unexpectedly"
                        ) from exc

                    reconnect_attempt = 0
                    rate_guard.reset()
                    exit_code = await self._process_event(event)
                    if exit_code is not None:
                        return exit_code
            except NotImplementedError as exc:
                if self._entry.type == "event":
                    return await self._handle_provider_exception(SubscriptionFailedError(str(exc)))
                _log.info("Subscription unavailable for %s; falling back to polling", path)
                return await self._poll_loop(path, rate_guard)
            except RateLimitError as exc:
                await self._close_subscription_stream(stream)
                stream = None
                try:
                    retry = await self._await_subscription_backoff(rate_guard.handle(exc))
                except _SubscriptionDeadlineReachedError:
                    return await self._emit_timeout()
                except _SubscriptionShutdownError:
                    return await self._emit_stopped()
                if not retry:
                    return await self._handle_provider_exception(exc)
            except (
                SubscriptionFailedError,
                RPCUnreachableError,
                ConnectionError,
                OSError,
                TimeoutError,
            ) as exc:
                await self._close_subscription_stream(stream)
                stream = None
                reconnect_attempt += 1
                delay = min(0.5 * (2 ** (reconnect_attempt - 1)), 30.0)
                _log.warning(
                    "Event subscription interrupted; reconnecting after %.1fs (attempt %d): %s",
                    delay,
                    reconnect_attempt,
                    exc,
                )
                await self._interruptible_sleep(delay)
            except Exception as exc:
                return await self._handle_provider_exception(exc)
            finally:
                await self._close_subscription_stream(stream)

    def _subscription_event_filter(self) -> EventFilter:
        """Resolve the fully structured event filter for this watcher."""
        if self._spec.event_filter is not None:
            return self._spec.event_filter
        event_type: str = self._entry.path_template
        if isinstance(self._spec.condition, EventCondition):
            event_type = self._spec.condition.event_type
        return EventFilter(event_types=(event_type,))

    @staticmethod
    async def _close_subscription_stream(stream: AsyncIterator[object] | None) -> None:
        if stream is None:
            return
        close = getattr(stream, "aclose", None)
        if callable(close):
            with contextlib.suppress(Exception):
                await close()

    async def _await_subscription_backoff(self, backoff: Awaitable[bool]) -> bool:
        """Wait for rate-limit recovery without outliving deadline or shutdown."""
        remaining = self._remaining_runtime_seconds()
        backoff_task = asyncio.ensure_future(backoff)
        shutdown_wait = asyncio.create_task(self._shutdown_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {backoff_task, shutdown_wait},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise _SubscriptionDeadlineReachedError
            if shutdown_wait in done:
                raise _SubscriptionShutdownError
            return backoff_task.result()
        finally:
            for task in (backoff_task, shutdown_wait):
                if not task.done():
                    task.cancel()
            await asyncio.gather(backoff_task, shutdown_wait, return_exceptions=True)

    async def _await_subscription_task(
        self,
        item_task: asyncio.Task[_T],
    ) -> _T:
        """Await an already-started subscription item within watcher limits."""
        if self._shutdown_event.is_set():
            raise _SubscriptionShutdownError
        if self._budget.is_runtime_exceeded():
            raise _SubscriptionDeadlineReachedError

        remaining = self._remaining_runtime_seconds()
        if remaining is not None and remaining <= 0:
            raise _SubscriptionDeadlineReachedError

        shutdown_wait = asyncio.create_task(self._shutdown_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {item_task, shutdown_wait},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise _SubscriptionDeadlineReachedError
            if shutdown_wait in done:
                raise _SubscriptionShutdownError
            return item_task.result()
        finally:
            if not shutdown_wait.done():
                shutdown_wait.cancel()
            await asyncio.gather(shutdown_wait, return_exceptions=True)
            if not item_task.done():
                item_task.cancel()
                await asyncio.gather(item_task, return_exceptions=True)

    async def _next_subscription_event(self, iterator: AsyncIterator[Event]) -> Event:
        """Await one event while remaining responsive to deadline and shutdown."""
        if self._shutdown_event.is_set():
            raise _SubscriptionShutdownError
        if self._budget.is_runtime_exceeded():
            raise _SubscriptionDeadlineReachedError

        remaining = self._remaining_runtime_seconds()
        if remaining is not None and remaining <= 0:
            raise _SubscriptionDeadlineReachedError

        next_event = asyncio.ensure_future(anext(iterator))
        shutdown_wait = asyncio.create_task(self._shutdown_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {next_event, shutdown_wait},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise _SubscriptionDeadlineReachedError
            if shutdown_wait in done:
                raise _SubscriptionShutdownError
            return next_event.result()
        finally:
            for task in (next_event, shutdown_wait):
                if not task.done():
                    task.cancel()
            await asyncio.gather(next_event, shutdown_wait, return_exceptions=True)

    async def _process_event(self, event: object) -> int | None:
        """Evaluate one subscription event; dispatch on match.

        Returns an exit code if the loop should terminate, or None to continue.
        """
        outcome = self._primitive.evaluate(event)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
        if isinstance(outcome, Match):
            payload = _build_match_payload(self._spec, outcome, self._budget)
            await self._dispatch(payload)
            self._match_count += 1
            if self._should_exit_after_dispatch():
                return 0
        return None

    def _charge_subscription_rpc(self, ru_cost: int) -> None:
        """Preflight and record one event-subscription RPC before it runs.

        The callback is synchronous and the event bridge runs on this same
        event loop, so no other budget mutation can interleave between the
        preflight and charge.
        """
        self._budget.ensure_ru_available(ru_cost)
        self._budget.charge_rpc_call(ru_cost=ru_cost)

    async def _call_with_deadline(self, call: Callable[[], Awaitable[_T]]) -> _T:
        """Run a provider operation and all its retries within time remaining."""
        remaining = self._remaining_runtime_seconds()
        if remaining is not None and remaining <= 0:
            raise _WatcherDeadlineReachedError
        if remaining is None:
            return await call()
        try:
            async with asyncio.timeout(remaining):
                return await call()
        except TimeoutError as exc:
            raise _WatcherDeadlineReachedError from exc

    async def _handle_provider_exception(self, exc: BaseException) -> int:
        """Convert provider/budget/unexpected exceptions to exit codes.

        Shared between _fetch and _subscription_loop so both paths produce
        consistent exit payloads.
        """
        if isinstance(exc, (BudgetExhaustedError, CUExhaustedError)):
            reason = (
                exc.reason
                if isinstance(exc, BudgetExhaustedError)
                else "provider_compute_units_exhausted"
            )
            return await self._emit_budget_exhausted(reason)  # type: ignore[arg-type]
        if isinstance(exc, UserError):
            # Entity/path validation can only be performed against pinned
            # chain state inside the provider. Preserve its user_error type
            # for the outer CLI/MCP dispatch layer instead of masking it as an
            # internal failure.
            raise exc
        if isinstance(exc, AuthError):
            return await self._emit_provider_error(str(exc), "auth_failed")
        if isinstance(exc, ProviderError):
            return await self._emit_provider_error(str(exc), exc.reason)
        if isinstance(exc, ConnectionError | OSError | asyncio.TimeoutError):
            # WS drop / DNS / refused / read timeout mid-poll — these are
            # provider failures, not chainwake bugs. Match the dispatch-layer
            # classification at connect time.
            return await self._emit_provider_error(
                f"{type(exc).__name__}: {exc}", "rpc_unreachable"
            )
        if isinstance(exc, Exception):
            return await self._emit_internal_error(exc)
        return await self._emit_internal_error(RuntimeError(str(exc)))

    async def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep until the next poll, deadline, or shutdown signal."""
        remaining = self._remaining_runtime_seconds()
        timeout = seconds if remaining is None else min(seconds, max(0.0, remaining))
        if timeout <= 0:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.shield(self._shutdown_event.wait()),
                timeout=timeout,
            )

    def _remaining_runtime_seconds(self) -> float | None:
        """Return time left on the watcher deadline, or None when unbounded."""
        if self._spec.max_runtime_seconds is None:
            return None
        return self._spec.max_runtime_seconds - (self._budget.runtime_ms / 1000)

    async def _emit_timeout(self) -> int:
        payload = _build_timeout_payload(self._spec, self._budget)
        await self._dispatch(payload)
        return 1

    async def _emit_stopped(self) -> int:
        payload = _build_stopped_payload(self._spec, self._budget)
        await self._dispatch(payload)
        return 1

    async def _emit_budget_exhausted(
        self,
        reason: Literal["max_ru_reached", "provider_compute_units_exhausted"],
    ) -> int:
        payload = _build_budget_exhausted_payload(self._spec, self._budget, reason)
        await self._dispatch(payload)
        return 1

    async def _emit_provider_error(self, message: str, reason: ProviderErrorReason) -> int:
        payload = ProviderErrorPayload(
            watcher=build_watcher(self._spec),
            condition=self._spec.condition,
            budget=build_budget(self._budget),
            process=build_process(self._budget.started_at),
            message=message,
            reason=reason,
        )
        await self._dispatch(payload)
        return 3

    async def _emit_internal_error(self, exc: Exception) -> int:
        payload = InternalErrorPayload(
            watcher=build_watcher(self._spec),
            condition=self._spec.condition,
            budget=build_budget(self._budget),
            process=build_process(self._budget.started_at),
            message=f"{type(exc).__name__}: {exc}",
            reason=type(exc).__name__,
        )
        await self._dispatch(payload)
        return 4


# ---------------------------------------------------------------------------
__all__ = [
    "Budget",
    "ProviderErrorReason",
    "WatcherRunner",
    "WatcherSpec",
    "build_auth_error_payload",
    "build_budget",
    "build_internal_error_payload",
    "build_provider_error_payload",
    "build_watcher",
]
