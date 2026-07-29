"""CLI to runtime glue — all primitive shapes.

Each `dispatch_*` function:
  1. Builds a `WatcherSpec` from validated CLI args.
  2. Connects the provider.
  3. Runs the watcher to first match or timeout via `WatcherRunner`.
  4. Handles provider / internal errors and emits schema-valid JSON.

The dispatch layer stays thin: it maps CLI args onto specs and errors.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from datetime import UTC, datetime
from typing import Literal, cast

import websockets.exceptions
from async_substrate_interface.errors import SubstrateRequestException

from chainwake.chains import ChainAlias, ChainBackend, backend_for
from chainwake.core.budget import Budget as RuntimeBudget
from chainwake.core.errors import AuthError, ProviderError, UserError
from chainwake.core.primitives.base import Primitive, PrimitiveInput
from chainwake.core.primitives.delta import DeltaPrimitive
from chainwake.core.primitives.event import EventPrimitive
from chainwake.core.primitives.liveness import LivenessPrimitive
from chainwake.core.primitives.state import StatePrimitive
from chainwake.core.primitives.threshold import ThresholdOperator, ThresholdPrimitive
from chainwake.core.primitives.tx import TxPrimitive
from chainwake.core.registry import lookup
from chainwake.core.runtime import (
    WatcherRunner,
    WatcherSpec,
    build_auth_error_payload,
    build_internal_error_payload,
    build_provider_error_payload,
    build_watcher,
)
from chainwake.jobs.service import durable_requested, submit_durable_job
from chainwake.output.adapters import Adapter, DefaultAdapter, parse_adapter_uri
from chainwake.output.render import render_mode_from_env
from chainwake.output.schema import (
    Budget,
    DeltaCondition,
    EventCondition,
    LivenessCondition,
    Payload,
    Process,
    StateCondition,
    StoppedPayload,
    ThresholdCondition,
    TxCondition,
    UserErrorPayload,
    Window,
)
from chainwake.providers.base import ChainProvider, EventDirection, EventFilter, ProviderConfig

_PROVIDER_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionError,
    OSError,
    TimeoutError,
    asyncio.TimeoutError,
)

# HTTP status range that signals upstream RPC unavailability rather than a
# chainwake bug. 5xx → provider_error. 401 is handled separately as auth.
_HTTP_SERVER_ERROR_MIN: int = 500
_HTTP_SERVER_ERROR_MAX: int = 599
_HTTP_UNAUTHORIZED: int = 401

# Spec §10.2: per-chain env var first, global fallback second. Surfaced in
# the AuthErrorPayload so agents can recover without parsing the message.
_AUTH_DOCS_URL: str = "https://blockmachine.io"


def _auth_env_vars(chain: str) -> list[str]:
    """Env vars chainwake reads for an API key, in precedence order."""
    return [f"CHAINWAKE_{chain.upper()}_API_KEY", "CHAINWAKE_API_KEY"]


def _format_auth_error_message(chain: str) -> str:
    per_chain, fallback = _auth_env_vars(chain)
    return (
        f"Authentication failed for chain {chain!r}. "
        f"Set --api-key on the command line, or the "
        f"{per_chain} or {fallback} environment variable."
    )


def _emit_auth_error_stderr(chain: str) -> None:
    """Write the actionable one-liner to stderr before the JSON payload.

    Users running interactively see the recovery hint immediately without
    having to parse the JSON. Agents that consume stdout JSON ignore stderr.
    """
    per_chain, _ = _auth_env_vars(chain)
    print(
        f"chainwake: authentication failed for chain {chain!r}. Set --api-key or {per_chain}.",
        file=sys.stderr,
    )


def _emit_user_error(reason: str, message: str, *, started_at: datetime) -> int:
    payload: Payload = UserErrorPayload(
        watcher=None,
        condition=None,
        budget=Budget(runtime_ms=0, rpc_calls=0, estimated_ru_consumed=0),
        process=Process(pid=os.getpid(), started_at=started_at),
        message=message,
        reason=reason,
    )
    print(json.dumps(payload.model_dump(mode="json"), indent=2))
    return 2


def _parse_out_uris(out_uris: list[str]) -> list[Adapter]:
    """Resolve --out URIs to adapter instances.

    The bare-stdout `DefaultAdapter` honours the render mode set by the
    root-level ``--json`` / ``--no-json`` flags via the
    ``CHAINWAKE_RENDER_MODE`` env var (read once here). Other adapters
    always emit JSON.
    """
    if not out_uris:
        return [DefaultAdapter(render_mode=render_mode_from_env())]
    return [parse_adapter_uri(uri) for uri in out_uris]


async def _run_with_error_handling(  # noqa: PLR0911, PLR0912, PLR0915
    spec: WatcherSpec,
    *,
    entry_path: str,
    primitive: Primitive[PrimitiveInput],
    rpc_url: str,
    out_uris: list[str],
    api_key: str | None = None,
) -> int:
    """Connect provider, run watcher, handle errors. Returns exit code.

    Error classification ladder (spec §11):
      - UserError → user_error exit 2 (bad CLI input, invalid path params)
      - AuthError → auth_error exit 3 (RPC -32021; spec §11 keeps auth in
        the provider-error exit-code category but spec Appendix D gives it
        its own payload status with actionable env-var hints)
      - websockets InvalidStatus 401 → re-raised as AuthError (chains hosted
        behind authenticated WebSocket endpoints reject the handshake before
        any RPC frame is sent, so async-substrate-interface never sees an
        RPC-level -32021)
      - websockets InvalidStatus 5xx → provider_error exit 3
      - _PROVIDER_ERRORS → provider_error exit 3 (network unreachable)
      - SubstrateRequestException → provider_error exit 3 (rate-limited,
        subscription_failed, or rpc_unreachable based on message text;
        substrate RPC failures are always upstream, never internal)
      - Exception → internal_error exit 4 (unexpected; last-resort catchall)

    New error classes must be classified above the catchall, never folded
    into it; otherwise real bugs get masked as user_error and vice versa.
    """
    started_at = datetime.now(UTC)
    adapters: list[Adapter] | None = None
    error_adapter: Adapter | None = None
    backend: ChainBackend
    provider: ChainProvider | None = None
    connected = False
    try:
        try:
            backend = backend_for(spec.chain)
        except KeyError as exc:
            raise UserError(
                f"unsupported chain {spec.chain!r}",
                reason="unsupported_chain",
            ) from exc
        try:
            entry = lookup(entry_path, chain=spec.chain)
        except KeyError as exc:
            raise UserError(
                f"unknown observable {entry_path!r}",
                reason="unknown_observable",
            ) from exc
        durable_exit = _submit_if_durable(spec, out_uris)
        if durable_exit is not None:
            return durable_exit
        adapters = _parse_out_uris(out_uris)
        # Error payloads always go to the first configured adapter so callers
        # using --out file://... or --out stream receive errors in the same
        # sink as matches. Match/timeout payloads fan out to every adapter.
        error_adapter = adapters[0]
        provider = backend.create_provider()
        try:
            await provider.connect(ProviderConfig(rpc_url=rpc_url, api_key=api_key))
        except websockets.exceptions.InvalidStatus as exc:
            _classify_invalid_status(exc)
        connected = True
        runner = WatcherRunner(
            spec,
            entry=entry,
            provider=provider,
            primitive=primitive,
            adapters=adapters,
            budget=RuntimeBudget(
                max_runtime_seconds=spec.max_runtime_seconds,
                max_ru=spec.max_ru,
            ),
            runtime=backend.runtime,
        )
        return await runner.run()
    except asyncio.CancelledError:
        if error_adapter is None:
            error_adapter = _parse_out_uris(out_uris)[0]
        error_adapter.dispatch(
            StoppedPayload(
                watcher=build_watcher(spec),
                condition=spec.condition,
                budget=Budget(
                    runtime_ms=int((datetime.now(UTC) - started_at).total_seconds() * 1000),
                    rpc_calls=0,
                    estimated_ru_consumed=0,
                ),
                process=Process(pid=os.getpid(), started_at=started_at),
            )
        )
        return 1
    except UserError as exc:
        if error_adapter is None:
            if durable_requested():
                return _emit_user_error(exc.reason, str(exc), started_at=started_at)
            error_adapter = _parse_out_uris(out_uris)[0]
        error_adapter.dispatch(
            UserErrorPayload(
                watcher=None,
                condition=None,
                budget=Budget(runtime_ms=0, rpc_calls=0, estimated_ru_consumed=0),
                process=Process(pid=os.getpid(), started_at=started_at),
                message=str(exc),
                reason=exc.reason,
            )
        )
        return 2
    except AuthError:
        if error_adapter is None:
            error_adapter = _parse_out_uris(out_uris)[0]
        _emit_auth_error_stderr(spec.chain)
        error_adapter.dispatch(
            build_auth_error_payload(
                spec,
                started_at=started_at,
                message=_format_auth_error_message(spec.chain),
                api_key_env_vars=_auth_env_vars(spec.chain),
                docs_url=_AUTH_DOCS_URL,
            )
        )
        return 3
    except _PROVIDER_ERRORS as exc:
        if error_adapter is None:
            error_adapter = _parse_out_uris(out_uris)[0]
        error_adapter.dispatch(
            build_provider_error_payload(
                spec,
                started_at=started_at,
                message=f"{type(exc).__name__}: {exc}",
                reason="rpc_unreachable",
            )
        )
        return 3
    except ProviderError as exc:
        if error_adapter is None:
            error_adapter = _parse_out_uris(out_uris)[0]
        # Provider boundary now wraps SubstrateRequestException into
        # RateLimitError / SubscriptionFailedError / RPCUnreachableError
        # at connect() and read_observable() — those carry the right
        # reason directly. Catch them above the SubstrateRequestException
        # clause (which is dead post-wrapping but kept as defence in
        # depth) so they don't fall through to internal_error.
        error_adapter.dispatch(
            build_provider_error_payload(
                spec,
                started_at=started_at,
                message=f"{type(exc).__name__}: {exc}",
                reason=exc.reason,
            )
        )
        return 3
    except SubstrateRequestException as exc:
        if error_adapter is None:
            error_adapter = _parse_out_uris(out_uris)[0]
        error_adapter.dispatch(
            build_provider_error_payload(
                spec,
                started_at=started_at,
                message=f"{type(exc).__name__}: {exc}",
                reason=_classify_substrate_request(exc),
            )
        )
        return 3
    except Exception as exc:
        if error_adapter is None:
            error_adapter = _parse_out_uris(out_uris)[0]
        # Last-resort catchall. Any new exception class that needs a
        # different status/exit code MUST be classified above this line.
        error_adapter.dispatch(
            build_internal_error_payload(
                spec,
                started_at=started_at,
                message=f"{type(exc).__name__}: {exc}",
                reason=type(exc).__name__,
            )
        )
        return 4
    finally:
        if connected and provider is not None:
            with contextlib.suppress(Exception):
                await provider.disconnect()


def _submit_if_durable(spec: WatcherSpec, out_uris: list[str]) -> int | None:
    """Return a durable submission exit code, or ``None`` for a direct watch."""
    if not durable_requested():
        return None
    try:
        record = submit_durable_job(spec, out_uris=out_uris)
    except ValueError as exc:
        return _emit_user_error("invalid_input", str(exc), started_at=datetime.now(UTC))
    return record.exit_code if record.exit_code is not None else 0


_RATE_LIMIT_MARKERS: tuple[str, ...] = ("rate limit", "too many requests", "429")
_SUBSCRIPTION_MARKERS: tuple[str, ...] = ("subscribe", "subscription")


def _classify_substrate_request(
    exc: SubstrateRequestException,
) -> Literal["rate_limited", "subscription_failed", "rpc_unreachable"]:
    """Map a ``SubstrateRequestException`` message onto a provider_error reason.

    The async-substrate-interface library funnels every RPC-level failure
    through this single exception class with no structured detail beyond
    the message string, so we have to text-match. Rate limits and failed
    subscriptions are common, recoverable upstream conditions; everything
    else routes to ``rpc_unreachable`` rather than ``internal_error`` —
    a substrate RPC error is always upstream, never a chainwake bug.
    """
    msg = str(exc).lower()
    if any(marker in msg for marker in _RATE_LIMIT_MARKERS):
        return "rate_limited"
    if any(marker in msg for marker in _SUBSCRIPTION_MARKERS):
        return "subscription_failed"
    return "rpc_unreachable"


def _classify_invalid_status(exc: websockets.exceptions.InvalidStatus) -> None:
    """Map a WebSocket handshake rejection onto a chainwake error class.

    HTTP 401 means the server rejected the handshake as unauthenticated —
    the user did not supply (or supplied an invalid) API key. We re-raise
    as ``AuthError`` so the existing AuthError branch emits an
    ``AuthErrorPayload`` with env-var hints. HTTP 5xx is upstream
    unavailability — re-raise as ``ConnectionError`` so the existing
    provider-error branch emits ``provider_error / rpc_unreachable``.
    Anything else (4xx other than 401) falls through to the catchall as
    ``internal_error``; we cannot infer the user's recovery path from a
    teapot.
    """
    status_code = exc.response.status_code
    if status_code == _HTTP_UNAUTHORIZED:
        raise AuthError(f"chain rejected the websocket handshake (HTTP {status_code})") from exc
    if _HTTP_SERVER_ERROR_MIN <= status_code <= _HTTP_SERVER_ERROR_MAX:
        raise ConnectionError(f"chain RPC returned HTTP {status_code} during handshake") from exc
    raise exc


# ---------------------------------------------------------------------------
# Threshold dispatch
# ---------------------------------------------------------------------------


async def dispatch_threshold(
    *,
    resource: str,
    path_params: dict[str, str],
    sub_resource: str,
    entry_path: str,
    operator: ThresholdOperator,
    target: float,
    rpc_url: str,
    max_runtime_seconds: float | None,
    poll_seconds: float | None,
    invocation: list[str],
    out_uris: list[str],
    name: str | None = None,
    max_ru: int | None = None,
    api_key: str | None = None,
    read_args: dict[str, object] | None = None,
    chain: ChainAlias = "bt",
) -> int:
    spec = WatcherSpec(
        chain=chain,
        resource=resource,
        path_params=path_params,
        sub_resource=sub_resource,
        primitive_name="threshold",
        condition=ThresholdCondition(operator=operator, target=target),
        invocation=invocation,
        name=name,
        poll_seconds=poll_seconds,
        max_runtime_seconds=max_runtime_seconds,
        max_ru=max_ru,
        read_args=dict(read_args) if read_args else {},
    )
    primitive = ThresholdPrimitive(operator=operator, target=target)
    return await _run_with_error_handling(
        spec,
        entry_path=entry_path,
        primitive=cast("Primitive[PrimitiveInput]", primitive),
        rpc_url=rpc_url,
        out_uris=out_uris,
        api_key=api_key,
    )


# ---------------------------------------------------------------------------
# Delta dispatch
# ---------------------------------------------------------------------------


async def dispatch_delta(
    *,
    resource: str,
    path_params: dict[str, str],
    sub_resource: str,
    entry_path: str,
    operator: Literal["drop-pct", "rise-pct", "move-pct"],
    target: float,
    window_unit: Literal["ever", "time", "blocks", "epochs"],
    window_value: str,
    rpc_url: str,
    max_runtime_seconds: float | None,
    poll_seconds: float | None,
    invocation: list[str],
    out_uris: list[str],
    name: str | None = None,
    max_ru: int | None = None,
    api_key: str | None = None,
    read_args: dict[str, object] | None = None,
    chain: ChainAlias = "bt",
) -> int:
    spec = WatcherSpec(
        chain=chain,
        resource=resource,
        path_params=path_params,
        sub_resource=sub_resource,
        primitive_name="delta",
        condition=build_delta_condition(operator, target, window_unit, window_value),
        invocation=invocation,
        name=name,
        poll_seconds=poll_seconds,
        max_runtime_seconds=max_runtime_seconds,
        max_ru=max_ru,
        read_args=dict(read_args) if read_args else {},
    )
    primitive = DeltaPrimitive(
        operator=operator,
        target=target,
        window_unit=window_unit,
        window_value=window_value,
    )
    return await _run_with_error_handling(
        spec,
        entry_path=entry_path,
        primitive=cast("Primitive[PrimitiveInput]", primitive),
        rpc_url=rpc_url,
        out_uris=out_uris,
        api_key=api_key,
    )


# ---------------------------------------------------------------------------
# State dispatch
# ---------------------------------------------------------------------------


async def dispatch_state(
    *,
    resource: str,
    path_params: dict[str, str],
    sub_resource: str,
    entry_path: str,
    operator: Literal["on-change", "changes-to", "changes-from"],
    target: str | int | float | bool | None,
    rpc_url: str,
    max_runtime_seconds: float | None,
    poll_seconds: float | None,
    invocation: list[str],
    out_uris: list[str],
    name: str | None = None,
    max_ru: int | None = None,
    api_key: str | None = None,
    chain: ChainAlias = "bt",
) -> int:
    spec = WatcherSpec(
        chain=chain,
        resource=resource,
        path_params=path_params,
        sub_resource=sub_resource,
        primitive_name="state",
        condition=build_state_condition(operator, target),
        invocation=invocation,
        name=name,
        poll_seconds=poll_seconds,
        max_runtime_seconds=max_runtime_seconds,
        max_ru=max_ru,
    )
    primitive = StatePrimitive(operator=operator, target=target)
    return await _run_with_error_handling(
        spec,
        entry_path=entry_path,
        primitive=cast("Primitive[PrimitiveInput]", primitive),
        rpc_url=rpc_url,
        out_uris=out_uris,
        api_key=api_key,
    )


# ---------------------------------------------------------------------------
# Liveness dispatch
# ---------------------------------------------------------------------------


async def dispatch_liveness(
    *,
    resource: str,
    path_params: dict[str, str],
    sub_resource: str,
    entry_path: str,
    silent_for: str,
    rpc_url: str,
    max_runtime_seconds: float | None,
    poll_seconds: float | None,
    invocation: list[str],
    out_uris: list[str],
    name: str | None = None,
    max_ru: int | None = None,
    api_key: str | None = None,
    read_args: dict[str, object] | None = None,
    chain: ChainAlias = "bt",
) -> int:
    spec = WatcherSpec(
        chain=chain,
        resource=resource,
        path_params=path_params,
        sub_resource=sub_resource,
        primitive_name="liveness",
        condition=build_liveness_condition(silent_for),
        invocation=invocation,
        name=name,
        poll_seconds=poll_seconds,
        max_runtime_seconds=max_runtime_seconds,
        max_ru=max_ru,
        read_args=dict(read_args) if read_args else {},
    )
    primitive = LivenessPrimitive(silent_for=silent_for)
    return await _run_with_error_handling(
        spec,
        entry_path=entry_path,
        primitive=cast("Primitive[PrimitiveInput]", primitive),
        rpc_url=rpc_url,
        out_uris=out_uris,
        api_key=api_key,
    )


# ---------------------------------------------------------------------------
# Event dispatch
# ---------------------------------------------------------------------------


async def dispatch_event(
    *,
    event_type: str,
    args_match: dict[str, object] | None,
    entry_path: str,
    rpc_url: str,
    max_runtime_seconds: float | None,
    invocation: list[str],
    out_uris: list[str],
    name: str | None = None,
    max_ru: int | None = None,
    api_key: str | None = None,
    amount_min: int | None = None,
    direction: EventDirection | None = None,
    direction_address: str | None = None,
    resource: str = "event",
    sub_resource: str | None = None,
    chain: ChainAlias = "bt",
) -> int:
    spec = WatcherSpec(
        chain=chain,
        resource=resource,
        path_params={},
        sub_resource=sub_resource or event_type,
        primitive_name="event",
        condition=build_event_condition(
            event_type,
            cast("dict[str, str | int | float | bool | None] | None", args_match),
            amount_min=amount_min,
            direction=direction,
            direction_address=direction_address,
        ),
        invocation=invocation,
        name=name,
        max_runtime_seconds=max_runtime_seconds,
        max_ru=max_ru,
        event_filter=EventFilter(
            event_types=(event_type,),
            args_match=dict(args_match) if args_match else {},
            amount_min=amount_min,
            direction=direction,
            direction_address=direction_address,
        ),
    )
    primitive = EventPrimitive(event_type=event_type, args_match=args_match)
    return await _run_with_error_handling(
        spec,
        entry_path=entry_path,
        primitive=cast("Primitive[PrimitiveInput]", primitive),
        rpc_url=rpc_url,
        out_uris=out_uris,
        api_key=api_key,
    )


# ---------------------------------------------------------------------------
# Tx dispatch
# ---------------------------------------------------------------------------


async def dispatch_tx(
    *,
    tx_hash: str,
    finality: Literal["included", "safe", "finalized"],
    entry_path: str,
    rpc_url: str,
    max_runtime_seconds: float | None,
    invocation: list[str],
    out_uris: list[str],
    name: str | None = None,
    max_ru: int | None = None,
    api_key: str | None = None,
    chain: ChainAlias = "bt",
    confirmations: int | None = None,
) -> int:
    spec = WatcherSpec(
        chain=chain,
        resource="tx",
        path_params={"tx_hash": tx_hash},
        sub_resource="finality",
        primitive_name="tx",
        condition=build_tx_condition(finality, confirmations=confirmations),
        invocation=invocation,
        name=name,
        max_runtime_seconds=max_runtime_seconds,
        max_ru=max_ru,
        read_args={
            "finality": finality,
            **({"confirmations": confirmations} if confirmations is not None else {}),
        },
    )
    primitive = TxPrimitive(
        tx_hash=tx_hash,
        finality=finality,
        confirmations=confirmations,
    )
    return await _run_with_error_handling(
        spec,
        entry_path=entry_path,
        primitive=cast("Primitive[PrimitiveInput]", primitive),
        rpc_url=rpc_url,
        out_uris=out_uris,
        api_key=api_key,
    )


# ---------------------------------------------------------------------------
# Helpers for building schema condition objects
# ---------------------------------------------------------------------------


def build_delta_condition(
    operator: Literal["drop-pct", "rise-pct", "move-pct"],
    target: float,
    window_unit: Literal["ever", "time", "blocks", "epochs"],
    window_value: str,
) -> DeltaCondition:
    return DeltaCondition(
        operator=operator,
        target=target,
        window=Window(unit=window_unit, value=window_value),
    )


def build_state_condition(
    operator: Literal["on-change", "changes-to", "changes-from"],
    target: str | int | float | bool | None = None,
) -> StateCondition:
    return StateCondition(operator=operator, target=target)


def build_liveness_condition(duration: str) -> LivenessCondition:
    return LivenessCondition(operator="silent-for", duration=duration)


def build_event_condition(
    event_type: str,
    filters: dict[str, str | int | float | bool | None] | None = None,
    *,
    amount_min: int | None = None,
    direction: EventDirection | None = None,
    direction_address: str | None = None,
) -> EventCondition:
    """Build the schema EventCondition; fold predicate flags into `filters`.

    The output payload's `EventCondition.filters` is the user-facing record
    of what the watcher filtered on. The structured EventFilter the
    runtime hands to the provider is built separately by `dispatch_event`
    and stashed on the WatcherSpec.
    """
    merged: dict[str, str | int | float | bool | None] = dict(filters or {})
    if amount_min is not None:
        merged["amount_min"] = amount_min
    if direction is not None:
        merged["direction"] = direction
    if direction_address is not None:
        merged["direction_address"] = direction_address
    return EventCondition(event_type=event_type, filters=merged)


def build_tx_condition(
    finality: Literal["included", "safe", "finalized"],
    timeout: str | None = None,
    confirmations: int | None = None,
) -> TxCondition:
    return TxCondition(
        finality=finality,
        timeout=timeout,
        confirmations=confirmations,
    )


__all__ = [
    "build_delta_condition",
    "build_event_condition",
    "build_liveness_condition",
    "build_state_condition",
    "build_tx_condition",
    "dispatch_delta",
    "dispatch_event",
    "dispatch_liveness",
    "dispatch_state",
    "dispatch_threshold",
    "dispatch_tx",
]
