"""Render-mode resolution and human-readable payload renderers.

The default stdout adapter (`DefaultAdapter`) emits JSON when its output is
piped or redirected and human-readable text when it's attached to a TTY.
The `RenderMode` enum captures the three states a CLI can request:

- ``AUTO`` — detect via ``sys.stdout.isatty()``; this is the default
- ``JSON`` — force JSON regardless of TTY (set by ``--json``)
- ``HUMAN`` — force human text regardless of TTY (set by ``--no-json``)

`resolve_render_mode` collapses ``AUTO`` to whichever concrete mode the
current stdout calls for, so call sites never have to repeat the TTY
check. `render_human` turns any payload into a single deterministic
string suitable for terminal display.

Only the bare-stdout adapter participates in render-mode switching. File,
stream, and apprise adapters always emit JSON — their consumers are
machines.
"""

from __future__ import annotations

import json as _json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast

from pydantic import JsonValue

from chainwake.output.schema import (
    AuthErrorPayload,
    Budget,
    BudgetExhaustedPayload,
    InternalErrorPayload,
    MatchedPayload,
    ObservedDelta,
    ObservedEvent,
    ObservedLiveness,
    ObservedState,
    ObservedThreshold,
    ObservedTx,
    Payload,
    Process,
    ProviderErrorPayload,
    StoppedPayload,
    TimeoutPayload,
    UserErrorPayload,
    Watcher,
)


class RenderMode(StrEnum):
    """How `DefaultAdapter` should render payloads to stdout."""

    AUTO = "auto"
    JSON = "json"
    HUMAN = "human"


def resolve_render_mode(explicit: RenderMode = RenderMode.AUTO) -> RenderMode:
    """Resolve to a concrete render mode.

    Args:
        explicit: Caller-supplied mode. ``AUTO`` triggers TTY detection;
            ``JSON`` and ``HUMAN`` are returned unchanged.

    Returns:
        ``RenderMode.HUMAN`` when ``explicit`` is ``AUTO`` and stdout is
        attached to a TTY; ``RenderMode.JSON`` when ``explicit`` is
        ``AUTO`` and stdout is piped/redirected; otherwise ``explicit``.
    """
    if explicit is not RenderMode.AUTO:
        return explicit
    return RenderMode.HUMAN if sys.stdout.isatty() else RenderMode.JSON


# Env var the cyclopts meta-app sets when ``--json`` / ``--no-json`` is
# given, so the dispatch layer can pick the right `DefaultAdapter` mode
# without threading a parameter through every leaf command.
RENDER_MODE_ENV_VAR: str = "CHAINWAKE_RENDER_MODE"


def render_mode_from_env() -> RenderMode:
    """Read `RENDER_MODE_ENV_VAR` and return a `RenderMode`.

    Returns ``RenderMode.AUTO`` when the env var is unset or holds an
    unrecognised value; that lets ``DefaultAdapter`` fall back to TTY
    detection.
    """
    raw = os.environ.get(RENDER_MODE_ENV_VAR, "").strip().lower()
    if raw == RenderMode.JSON.value:
        return RenderMode.JSON
    if raw == RenderMode.HUMAN.value:
        return RenderMode.HUMAN
    return RenderMode.AUTO


def _resource_label(watcher: Watcher | None) -> str:
    """Build a short ``<resource> [<id>] [<sub>]`` label for prose output."""
    if watcher is None:
        return ""
    parts: list[str] = [watcher.resource]
    if watcher.resource_id is not None:
        parts.append(watcher.resource_id)
    if watcher.sub_resource is not None:
        parts.append(watcher.sub_resource)
    return " ".join(parts)


def _format_window(window_unit: str, window_value: str) -> str:
    """Format a delta window as e.g. ``1h``, ``50 blocks``, ``5 epochs``."""
    if window_unit == "ever":
        return "since watcher start"
    if window_unit == "time":
        return window_value
    return f"{window_value} {window_unit}"


def _format_event_args(args: dict[str, JsonValue]) -> str:
    """Render an event-arg dict as ``k=v k=v`` for prose output."""
    if not args:
        return ""

    def render_value(value: JsonValue) -> str:
        if isinstance(value, str | int | float | bool | type(None)):
            return str(value)
        return _json.dumps(value, sort_keys=True, separators=(",", ":"))

    return " ".join(f"{key}={render_value(value)}" for key, value in args.items())


def _render_threshold_match(payload: MatchedPayload, observed: ObservedThreshold) -> str:
    condition = payload.condition
    operator = getattr(condition, "operator", "?")
    target = getattr(condition, "target", "?")
    label = _resource_label(payload.watcher)
    return f"matched: {label} = {observed.value} ({operator} {target}) at block {observed.block}"


def _render_delta_match(payload: MatchedPayload, observed: ObservedDelta) -> str:
    window = getattr(payload.condition, "window", None)
    window_text = _format_window(window.unit, window.value) if window is not None else "window"
    label = _resource_label(payload.watcher)
    return (
        f"matched: {label} moved {observed.delta_pct:.4g}% over {window_text} "
        f"({observed.previous_value} -> {observed.value}) at block {observed.block}"
    )


def _render_state_match(watcher: Watcher | None, observed: ObservedState) -> str:
    label = _resource_label(watcher)
    return (
        f"matched: {label} changed ({observed.previous_value} -> {observed.value}) "
        f"at block {observed.block}"
    )


def _render_liveness_match(watcher: Watcher | None, observed: ObservedLiveness) -> str:
    label = _resource_label(watcher)
    return f"matched: {label} silent for {observed.elapsed} at block {observed.block}"


def _render_event_match(observed: ObservedEvent) -> str:
    args_text = _format_event_args(observed.args)
    base = f"matched: {observed.event_type} at block {observed.block}"
    return f"{base} (args: {args_text})" if args_text else base


def _render_tx_match(observed: ObservedTx) -> str:
    base = f"matched: tx {observed.tx_hash} reached {observed.finality} at block {observed.block}"
    if observed.execution_status is not None:
        return (
            f"{base} ({observed.confirmations} confirmations, "
            f"{observed.execution_status}, gas {observed.gas_used} "
            f"at {observed.effective_gas_price_wei} wei)"
        )
    return base


def _render_match(payload: MatchedPayload) -> str:
    observed = payload.observed
    if isinstance(observed, ObservedThreshold):
        return _render_threshold_match(payload, observed)
    if isinstance(observed, ObservedDelta):
        return _render_delta_match(payload, observed)
    if isinstance(observed, ObservedState):
        return _render_state_match(payload.watcher, observed)
    if isinstance(observed, ObservedLiveness):
        return _render_liveness_match(payload.watcher, observed)
    if isinstance(observed, ObservedEvent):
        return _render_event_match(observed)
    return _render_tx_match(observed)


_MS_PER_SECOND: int = 1000
_SECONDS_PER_MINUTE: int = 60
_MINUTES_PER_HOUR: int = 60


def _format_runtime(runtime_ms: int) -> str:
    """Format a runtime duration as a short prose token (``30s``, ``5m``, ...)."""
    if runtime_ms < _MS_PER_SECOND:
        return f"{runtime_ms}ms"
    seconds = runtime_ms // _MS_PER_SECOND
    if seconds < _SECONDS_PER_MINUTE:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, _SECONDS_PER_MINUTE)
    if minutes < _MINUTES_PER_HOUR:
        return f"{minutes}m{sec}s" if sec else f"{minutes}m"
    hours, mn = divmod(minutes, _MINUTES_PER_HOUR)
    return f"{hours}h{mn}m" if mn else f"{hours}h"


_TIMEOUT_REASON_PROSE: dict[str, str] = {
    "max_runtime_reached": "max runtime reached",
}


def _render_timeout(payload: TimeoutPayload) -> str:
    runtime = _format_runtime(payload.budget.runtime_ms)
    label = _resource_label(payload.watcher)
    reason = _TIMEOUT_REASON_PROSE.get(payload.reason, payload.reason)
    if label:
        return f"timeout after {runtime}: {reason} watching {label}"
    return f"timeout after {runtime}: {reason}"


def _render_stopped(payload: StoppedPayload) -> str:
    label = _resource_label(payload.watcher)
    return f"stopped: shutdown requested{f' while watching {label}' if label else ''}"


def _render_budget_exhausted(payload: BudgetExhaustedPayload) -> str:
    ru = payload.budget.estimated_ru_consumed
    return f"budget exhausted: {payload.reason} after {ru} RU consumed"


def _render_user_error(payload: UserErrorPayload) -> str:
    return f"error: {payload.message}"


_PROVIDER_ERROR_HINTS: dict[str, str] = {
    "rpc_unreachable": "Check your --rpc-url and network connectivity.",
    "subscription_failed": "Check your --rpc-url and network connectivity.",
    "rate_limited": (
        "Provider rate-limited the request. Wait before retrying. "
        "For higher Blockmachine limits, sign up at https://blockmachine.io and pass --api-key."
    ),
}


def _render_provider_error(payload: ProviderErrorPayload) -> str:
    base = f"error: {payload.message}"
    hint = _PROVIDER_ERROR_HINTS.get(payload.reason)
    return f"{base}\n{hint}" if hint else base


def _render_auth_error(payload: AuthErrorPayload) -> str:
    lines = [f"error: {payload.message}"]
    if payload.api_key_env_vars:
        env_vars = ", ".join(payload.api_key_env_vars)
        lines.append(f"Set --api-key, or one of: {env_vars}")
    if payload.docs_url is not None:
        lines.append(f"See {payload.docs_url} for an API key.")
    return "\n".join(lines)


def _render_internal_error(payload: InternalErrorPayload) -> str:
    return f"error (internal): {payload.message}\nThis is a bug in chainwake; please report it."


# status discriminator → renderer. AuthError must precede ProviderError
# because AuthErrorPayload is its own status (spec §9.4 / Appendix D); a
# plain isinstance ladder caught it correctly but the dispatch table is
# clearer and avoids the ruff "too many returns" complaint.
_RENDERERS: dict[str, Callable[[Payload], str]] = {
    "matched": lambda p: _render_match(cast("MatchedPayload", p)),
    "timeout": lambda p: _render_timeout(cast("TimeoutPayload", p)),
    "stopped": lambda p: _render_stopped(cast("StoppedPayload", p)),
    "budget_exhausted": lambda p: _render_budget_exhausted(cast("BudgetExhaustedPayload", p)),
    "auth_error": lambda p: _render_auth_error(cast("AuthErrorPayload", p)),
    "provider_error": lambda p: _render_provider_error(cast("ProviderErrorPayload", p)),
    "internal_error": lambda p: _render_internal_error(cast("InternalErrorPayload", p)),
    "user_error": lambda p: _render_user_error(cast("UserErrorPayload", p)),
}


def render_human(payload: Payload) -> str:
    """Render any payload as a single deterministic prose string.

    The string contains no timestamps — the JSON form already carries
    those for machine consumers; the human reader cares about the chain
    block and the actionable message. A given payload always produces
    the same string.
    """
    return _RENDERERS[payload.status](payload)


def emit_user_error(reason: str, message: str) -> None:
    """Print a ``user_error`` envelope to stdout, honouring render mode.

    Resolves :data:`RENDER_MODE_ENV_VAR` (``CHAINWAKE_RENDER_MODE``);
    falls back to a TTY check when the env var is unset. Human mode
    prints ``error: <message>`` via :func:`render_human`; JSON mode
    prints the full Pydantic envelope as indented JSON.

    Args:
        reason: ``UserErrorPayload.reason`` discriminator
            (e.g. ``"invalid_input"``).
        message: Single-line user-facing description of what went wrong.

    The caller is responsible for the exit code; this helper only emits
    the payload so it can be reused by both pre-parse handlers (which
    need to ``sys.exit(2)``) and dispatch sites (which return an int).
    """
    payload = UserErrorPayload(
        watcher=None,
        condition=None,
        budget=Budget(runtime_ms=0, rpc_calls=0, estimated_ru_consumed=0),
        process=Process(pid=os.getpid(), started_at=datetime.now(UTC)),
        message=message,
        reason=reason,
    )
    mode = render_mode_from_env()
    if mode is RenderMode.AUTO:
        mode = RenderMode.HUMAN if sys.stdout.isatty() else RenderMode.JSON
    if mode is RenderMode.HUMAN:
        print(render_human(payload))
    else:
        print(_json.dumps(payload.model_dump(mode="json"), indent=2))


__all__ = [
    "RENDER_MODE_ENV_VAR",
    "RenderMode",
    "emit_user_error",
    "render_human",
    "render_mode_from_env",
    "resolve_render_mode",
]
