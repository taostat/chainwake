"""Chain-neutral CLI parsing and numeric watcher dispatch."""

from __future__ import annotations

import math
import os
from typing import Literal, Never

from chainwake.chains import ChainAlias
from chainwake.cli.chains.dispatch import dispatch_delta, dispatch_threshold
from chainwake.cli.duration import (
    InvalidDurationError,
    duration_to_seconds,
    parse_duration,
    parse_duration_components,
)
from chainwake.output.render import emit_user_error


def user_error(message: str) -> Never:
    """Emit the standard invalid-input envelope and exit 2."""
    emit_user_error("invalid_input", message)
    raise SystemExit(2)


def parse_max_runtime(raw: str | None) -> float | None:
    """Parse a positive duration or raw seconds, or return unbounded."""
    if raw is None:
        return None
    try:
        seconds = duration_to_seconds(raw)
    except InvalidDurationError:
        try:
            seconds = float(raw)
        except ValueError:
            user_error(f"--max-runtime {raw!r} — expected e.g. '30s', '10m', '2h'")
    if not math.isfinite(seconds) or seconds <= 0:
        user_error("--max-runtime must be finite and greater than zero when provided")
    return seconds


def validate_duration_flag(raw: str, flag: str) -> str:
    """Validate and canonicalize one positive chain/time duration."""
    try:
        canonical = parse_duration(raw)
        _, magnitude = parse_duration_components(canonical)
    except InvalidDurationError as exc:
        user_error(f"{flag} {raw!r} — {exc}")
    if not math.isfinite(magnitude) or magnitude <= 0:
        user_error(f"{flag} must be finite and greater than zero")
    return canonical


def validate_pct(value: float) -> float:
    """Reject percentage magnitudes that cannot represent a movement."""
    if not math.isfinite(value) or value <= 0:
        user_error("percentage target must be finite and greater than zero")
    return value


def resolve_threshold(
    below: float | None,
    above: float | None,
) -> tuple[Literal["below", "above"], float]:
    """Resolve exactly one finite threshold operator."""
    if below is None and above is None:
        user_error("one of --below or --above is required")
    if below is not None and above is not None:
        user_error("--below and --above are mutually exclusive")
    if below is not None:
        if not math.isfinite(below):
            user_error("--below must be finite")
        return "below", below
    if above is None:
        user_error("one of --below or --above is required")
    if not math.isfinite(above):
        user_error("--above must be finite")
    return "above", above


def resolve_window(
    window_time: str | None,
    window_blocks: int | None,
    window_epochs: int | None,
) -> tuple[Literal["ever", "time", "blocks", "epochs"], str]:
    """Resolve one rolling window or the watcher-start baseline."""
    n_window = sum(f is not None for f in (window_time, window_blocks, window_epochs))
    if n_window == 0:
        return "ever", "watcher-start"
    if n_window > 1:
        user_error("--window-time, --window-blocks, --window-epochs are mutually exclusive")
    if window_time is not None:
        canonical = validate_duration_flag(window_time, "--window-time")
        unit, _ = parse_duration_components(canonical)
        if unit != "time":
            user_error("--window-time requires a wall-clock duration such as '1h'")
        return "time", canonical
    if window_blocks is not None:
        if window_blocks <= 0:
            user_error("--window-blocks must be greater than zero")
        return "blocks", str(window_blocks)
    if window_epochs is None:
        user_error("one of --window-time, --window-blocks, --window-epochs is required")
    if window_epochs <= 0:
        user_error("--window-epochs must be greater than zero")
    return "epochs", str(window_epochs)


def resolve_delta(
    *,
    drop_pct: float | None,
    rise_pct: float | None,
    move_pct: float | None,
    window_time: str | None,
    window_blocks: int | None,
    window_epochs: int | None,
) -> tuple[
    Literal["drop-pct", "rise-pct", "move-pct"],
    float,
    Literal["ever", "time", "blocks", "epochs"],
    str,
]:
    """Resolve exactly one percentage movement and its baseline window."""
    n_pcts = sum(f is not None for f in (drop_pct, rise_pct, move_pct))
    if n_pcts == 0:
        user_error("one of --drop-pct, --rise-pct, --move-pct required")
    if n_pcts > 1:
        user_error("only one of --drop-pct, --rise-pct, --move-pct allowed")
    if drop_pct is not None:
        operator: Literal["drop-pct", "rise-pct", "move-pct"] = "drop-pct"
        value = drop_pct
    elif rise_pct is not None:
        operator = "rise-pct"
        value = rise_pct
    else:
        if move_pct is None:
            user_error("one of --drop-pct, --rise-pct, --move-pct required")
        operator = "move-pct"
        value = move_pct
    window_unit, window_value = resolve_window(window_time, window_blocks, window_epochs)
    return operator, validate_pct(value), window_unit, window_value


def resolve_api_key(cli_value: str | None, chain: str) -> str | None:
    """Resolve explicit, per-chain, then global provider credentials."""
    if cli_value is not None:
        return cli_value
    chain_specific = os.environ.get(f"CHAINWAKE_{chain.upper()}_API_KEY")
    if chain_specific:
        return chain_specific
    return os.environ.get("CHAINWAKE_API_KEY")


async def dispatch_numeric(
    *,
    chain: ChainAlias = "bt",
    resource: str,
    path_params: dict[str, str],
    sub_resource: str,
    entry_path: str,
    drop_pct: float | None,
    rise_pct: float | None,
    move_pct: float | None,
    window_time: str | None,
    window_blocks: int | None,
    window_epochs: int | None,
    below: float | None,
    above: float | None,
    rpc_url: str,
    max_runtime_seconds: float | None,
    poll_seconds: float | None,
    invocation: list[str],
    out_uris: list[str],
    name: str | None = None,
    max_ru: int | None = None,
    api_key: str | None = None,
    read_args: dict[str, object] | None = None,
) -> int:
    """Dispatch a threshold-or-delta observable from flat CLI flags."""
    n_threshold = sum(f is not None for f in (below, above))
    n_delta = sum(f is not None for f in (drop_pct, rise_pct, move_pct))
    if n_threshold == 0 and n_delta == 0:
        user_error("one of --below, --above, --drop-pct, --rise-pct, --move-pct is required")
    if n_threshold > 0 and n_delta > 0:
        user_error("--below/--above and --drop-pct/--rise-pct/--move-pct are mutually exclusive")
    if n_delta:
        operator, target, window_unit, window_value = resolve_delta(
            drop_pct=drop_pct,
            rise_pct=rise_pct,
            move_pct=move_pct,
            window_time=window_time,
            window_blocks=window_blocks,
            window_epochs=window_epochs,
        )
        return await dispatch_delta(
            chain=chain,
            resource=resource,
            path_params=path_params,
            sub_resource=sub_resource,
            entry_path=entry_path,
            operator=operator,
            target=target,
            window_unit=window_unit,
            window_value=window_value,
            rpc_url=rpc_url,
            max_runtime_seconds=max_runtime_seconds,
            poll_seconds=poll_seconds,
            invocation=invocation,
            out_uris=out_uris,
            name=name,
            max_ru=max_ru,
            api_key=api_key,
            read_args=read_args,
        )
    operator, target = resolve_threshold(below, above)
    return await dispatch_threshold(
        chain=chain,
        resource=resource,
        path_params=path_params,
        sub_resource=sub_resource,
        entry_path=entry_path,
        operator=operator,
        target=target,
        rpc_url=rpc_url,
        max_runtime_seconds=max_runtime_seconds,
        poll_seconds=poll_seconds,
        invocation=invocation,
        out_uris=out_uris,
        name=name,
        max_ru=max_ru,
        api_key=api_key,
        read_args=read_args,
    )


__all__ = [
    "dispatch_numeric",
    "parse_max_runtime",
    "resolve_api_key",
    "resolve_delta",
    "resolve_threshold",
    "resolve_window",
    "user_error",
    "validate_duration_flag",
    "validate_pct",
]
