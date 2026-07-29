"""Bittensor ``bt`` subcommand tree — resource-first surface.

Command shape per spec §5.1:

    chainwake bt <resource> [<id>] <sub-resource> [flags]

Implementation: nested cyclopts Apps. Each resource is a sub-App; each
sub-resource is a command on that app. CLI flags are declared flat (no
--condition.value notation). A ``_resolve_*`` helper per command converts
the flat flags into the matching Pydantic input model.

Cross-cutting flags (--out, --name, --max-runtime, --rpc-url) appear on
every leaf command.

Boolean flags that should not generate a negative form use
``Parameter(negative="")``.
"""

from __future__ import annotations

import asyncio
import math
import os
import sys
from typing import Annotated, Literal, Never

import cyclopts

from chainwake.cli.chains.common import (
    dispatch_numeric as _dispatch_numeric,
)
from chainwake.cli.chains.common import (
    parse_max_runtime as _parse_max_runtime,
)
from chainwake.cli.chains.common import (
    resolve_api_key,
)
from chainwake.cli.chains.common import (
    resolve_delta as _resolve_delta,
)
from chainwake.cli.chains.common import (
    resolve_threshold as _resolve_threshold,
)
from chainwake.cli.chains.common import (
    validate_duration_flag as _validate_duration_flag,
)
from chainwake.cli.chains.common import (
    validate_pct as _validate_pct,
)
from chainwake.cli.chains.dispatch import (
    dispatch_delta,
    dispatch_event,
    dispatch_liveness,
    dispatch_state,
    dispatch_threshold,
    dispatch_tx,
)
from chainwake.cli.inputs.common import (
    AboveCondition,
    BelowCondition,
    CommissionChangesFromCondition,
    CommissionChangesToCondition,
    DropPctCondition,
    MovePctCondition,
    OnChangeCondition,
    RisePctCondition,
)
from chainwake.cli.inputs.event import is_raw_event_type
from chainwake.core.registry import FRIENDLY_EVENT_MAP
from chainwake.core.ss58 import validate_bittensor_ss58
from chainwake.core.tx_hash import validate_tx_hash
from chainwake.output.render import emit_user_error
from chainwake.providers.bittensor import DEFAULT_RPC_URL

# ``None`` means "let the runtime resolve from the chain's natural cadence".
# The runtime falls back to the Bittensor block time (12s) for per-block
# observables; per-epoch and per-event watchers ignore the value entirely.
_DEFAULT_POLL: float | None = None

# Suppress the auto-generated ``--empty-out`` negative for list parameters.
_OUT_PARAM = cyclopts.Parameter(
    name="--out",
    help="Output adapter URI (repeatable).",
    negative_iterable="",
)

# Provider API key — falls back to CHAINWAKE_BT_API_KEY then CHAINWAKE_API_KEY.
# The env_var declaration only handles the chain-specific path; the second
# fallback is applied by ``resolve_api_key`` inside each command.
_API_KEY_PARAM = cyclopts.Parameter(
    name="--api-key",
    env_var="CHAINWAKE_BT_API_KEY",
    help=(
        "Provider API key (falls back to $CHAINWAKE_BT_API_KEY, then "
        "$CHAINWAKE_API_KEY). Forwarded to the RPC provider as a "
        "Bearer Authorization header."
    ),
)

# Cross-cutting registry-estimated observation budget. Transport-level
# provider billing metering is deliberately not claimed here.
_MAX_RU_PARAM = cyclopts.Parameter(
    name="--max-ru",
    env_var="CHAINWAKE_BT_MAX_RU",
    help=(
        "Registry-estimated observation budget. Exits with a budget_exhausted "
        "payload when declared observation costs reach the limit. This is not a "
        "provider billing cap: connection bootstrap, retries, and hidden SDK RPCs "
        "are excluded."
    ),
)

_MECHID_PARAM = cyclopts.Parameter(
    name="--mechid",
    help=(
        "Subnet mechanism id (0-15). Defaults to 0, the main mechanism. "
        "Non-zero ids are verified against current chain state."
    ),
)
_MAX_MECHANISM_ID = 15
_MAX_MECHANISM_NETUID = 4_095
_MAX_NETUID = 65_535

# Canonical, successful wake commands registered below. MCP tests compare their
# executable manifest to this CLI-owned set so a newly wired wake cannot
# silently miss discovery.
BT_WIRED_WAKE_COMMANDS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("subnet", "price"),
        ("subnet", "tao-depth"),
        ("subnet", "alpha-depth"),
        ("subnet", "depth-for-trade"),
        ("subnet", "alpha-supply"),
        ("subnet", "moving-price"),
        ("subnet", "volume"),
        ("subnet", "registration-cost"),
        ("subnet", "emission-share"),
        ("subnet", "burn-rate"),
        ("subnet", "ema-tao-flow"),
        ("subnet", "hyperparams"),
        ("subnet", "identity"),
        ("validator", "dividends-alpha"),
        ("validator", "stake-alpha"),
        ("validator", "commission"),
        ("validator", "weights"),
        ("validator", "child-keys"),
        ("validator", "identity"),
        ("neuron", "incentive"),
        ("neuron", "dividends"),
        ("neuron", "stake-alpha"),
        ("neuron", "last-update"),
        ("neuron", "blocks-until-immunity-expires"),
        ("account", "balance"),
        ("account", "activity"),
        ("network", "tao-price"),
        ("network", "subnet-registration-cost"),
        ("network", "runtime-version"),
        ("network", "subnet-count"),
        ("network", "on-runtime-upgraded"),
        ("event",),
        ("tx",),
    }
)


# ---------------------------------------------------------------------------
# Error envelope helpers
# ---------------------------------------------------------------------------


def _invocation() -> list[str]:
    return [os.path.basename(sys.argv[0]), *sys.argv[1:]]


def _emit_user_error(reason: str, message: str) -> int:
    """Emit a user_error envelope honouring render mode; return exit code 2.

    Thin wrapper around :func:`chainwake.output.render.emit_user_error`
    that also returns the conventional exit code so call sites can
    ``return _emit_user_error(...)`` from dispatch helpers.
    """
    emit_user_error(reason, message)
    return 2


def _user_error(message: str) -> Never:
    """Emit JSON user_error and raise SystemExit(2)."""
    _emit_user_error("invalid_input", message)
    raise SystemExit(2)


def _validate_ss58_address(value: str, label: str) -> str:
    """Validate a Bittensor address before starting any provider work."""
    try:
        return validate_bittensor_ss58(value)
    except ValueError as exc:
        _user_error(f"{label}: {exc}")


# ---------------------------------------------------------------------------
# Condition resolvers — one per shape
# ---------------------------------------------------------------------------


def _resolve_price_condition(
    *,
    below: float | None,
    above: float | None,
    drop_pct: float | None,
    rise_pct: float | None,
    move_pct: float | None,
    window_time: str | None,
    window_blocks: int | None,
    window_epochs: int | None,
) -> BelowCondition | AboveCondition | DropPctCondition | RisePctCondition | MovePctCondition:
    n_threshold = sum(f is not None for f in (below, above))
    n_delta = sum(f is not None for f in (drop_pct, rise_pct, move_pct))
    has_window = any(f is not None for f in (window_time, window_blocks, window_epochs))
    n_window = sum(f is not None for f in (window_time, window_blocks, window_epochs))

    if n_threshold > 0 and n_delta > 0:
        _user_error("--below/--above and --drop-pct/--rise-pct/--move-pct are mutually exclusive")
    if n_threshold == 0 and n_delta == 0:
        _user_error("one of --below, --above, --drop-pct, --rise-pct, or --move-pct is required")
    if n_threshold > 1:
        _user_error("--below and --above are mutually exclusive")
    if n_delta > 1:
        _user_error("only one of --drop-pct, --rise-pct, --move-pct is allowed")
    if n_threshold > 0 and has_window:
        _user_error("--window-* flags are only valid with delta flags (--drop-pct etc.)")
    if n_window > 1:
        _user_error("--window-time, --window-blocks, --window-epochs are mutually exclusive")

    if below is not None:
        return BelowCondition(value=below)
    if above is not None:
        return AboveCondition(value=above)
    if drop_pct is not None:
        return DropPctCondition(
            pct=_validate_pct(drop_pct),
            window_time=window_time,
            window_blocks=window_blocks,
            window_epochs=window_epochs,
        )
    if rise_pct is not None:
        return RisePctCondition(
            pct=_validate_pct(rise_pct),
            window_time=window_time,
            window_blocks=window_blocks,
            window_epochs=window_epochs,
        )
    if move_pct is None:
        _user_error("one of --below, --above, --drop-pct, --rise-pct, --move-pct required")
    return MovePctCondition(
        pct=_validate_pct(move_pct),
        window_time=window_time,
        window_blocks=window_blocks,
        window_epochs=window_epochs,
    )


def _resolve_commission_state(
    on_change: bool,
    changes_to: float | None,
    changes_from: float | None,
) -> OnChangeCondition | CommissionChangesToCondition | CommissionChangesFromCondition:
    """Resolve commission operators without comparing provider floats to strings."""
    given = sum([on_change, changes_to is not None, changes_from is not None])
    if given == 0:
        _user_error("one of --on-change, --changes-to, --changes-from required")
    if given > 1:
        _user_error("--on-change, --changes-to, --changes-from are mutually exclusive")
    if on_change:
        return OnChangeCondition()
    target = changes_to if changes_to is not None else changes_from
    if target is None:
        _user_error("one of --changes-to or --changes-from is required")
    if not math.isfinite(target) or not 0 <= target <= 1:
        _user_error("commission target must be a finite fraction from 0 to 1")
    if changes_to is not None:
        return CommissionChangesToCondition(value=target)
    return CommissionChangesFromCondition(value=target)


def _resolve_on_change_only(on_change: bool) -> OnChangeCondition:
    """Require the only meaningful operator for structured records."""
    if not on_change:
        _user_error("--on-change is required")
    return OnChangeCondition()


def _resolve_rpc(rpc_url: str | None) -> str:
    return rpc_url or os.environ.get("CHAINWAKE_BT_RPC_URL") or DEFAULT_RPC_URL


def _validate_mechid(mechid: int) -> int:
    if not 0 <= mechid <= _MAX_MECHANISM_ID:
        _user_error("--mechid must be from 0 through 15")
    return mechid


def _validate_netuid(netuid: int) -> int:
    """Validate Subtensor's public ``NetUid`` (u16) protocol domain."""
    if not 0 <= netuid <= _MAX_NETUID:
        _user_error(f"--netuid must be from 0 through {_MAX_NETUID}")
    return netuid


def _validate_mechanism_netuid(netuid: int) -> int:
    if not 0 <= netuid <= _MAX_MECHANISM_NETUID:
        _user_error("--netuid must be from 0 through 4095")
    return netuid


def _state_target(
    condition: (OnChangeCondition | CommissionChangesToCondition | CommissionChangesFromCondition),
) -> str | float | None:
    """Extract the target value from a state condition, or None for on-change."""
    if isinstance(
        condition,
        CommissionChangesToCondition | CommissionChangesFromCondition,
    ):
        return condition.value
    return None


# ---------------------------------------------------------------------------
# id-first dispatch helper (spec §5.1)
# ---------------------------------------------------------------------------


def _install_id_first_meta(
    app: cyclopts.App,
    id_subcommands: frozenset[str],
    id_label: str,
    resource_label: str,
    id_arity: int = 1,
) -> cyclopts.App:
    """Wrap ``app`` with a meta-app that accepts ``<id...> <sub-command> [flags]``.

    Cyclopts parses command positionals after the sub-command natively. The
    Chainwake surface puts resource ids **first**
    (``bt subnet 19 price --below 0.5`` or
    ``bt neuron 19 5Fxxx incentive --below 0.1``), so we attach a
    meta-default that detects the ``<id...> <sub-command>`` shape and
    rewrites tokens before dispatching to the inner app.

    ``id_subcommands`` lists every sub-command that takes ``<id...>`` as
    positionals. Sub-commands without an id (e.g. ``on-registered``) fall
    through unchanged. ``id_arity`` is the number of id positionals each
    listed sub-command consumes (1 for subnet/validator/account; 2 for
    neuron, which takes ``<netuid> <hotkey>``).

    ``id_label`` is the angle-bracketed placeholder used in error
    messages (``"<netuid>"`` or ``"<netuid> <hotkey>"``).

    Returns the meta-app (caller registers ``meta`` on its parent under the
    desired name).
    """
    meta = app.meta
    meta.help_flags = []  # let --help reach the inner command for context-rich help

    # Cyclopts builds ``Usage: ...`` from the leaf's command_chain, which
    # for id-first leaves would put ``<sub-command>`` before ``<id...>``.
    # Override usage per leaf so the help page advertises the Chainwake
    # ``bt <resource> <id...> <sub-command>`` form.
    for sub in id_subcommands:
        app[sub].usage = f"Usage: chainwake bt {resource_label} {id_label} {sub} [OPTIONS]\n"
    # The resource app's own usage line (shown when the user types
    # ``bt <resource>`` with no further args) defaults to the cyclopts-generated
    # ``Usage: <resource> COMMAND [ARGS...]`` which hides the id positional
    # entirely. Tell the user up-front that <id> goes first.
    app.usage = f"Usage: chainwake bt {resource_label} {id_label} COMMAND [OPTIONS]\n"

    @meta.default
    def _dispatch(
        *tokens: Annotated[str, cyclopts.Parameter(allow_leading_hyphen=True)],
    ) -> object:
        if not tokens:
            return app([])
        first = tokens[0]
        # `<ids>` alone (no sub-command yet) — show the resource's command
        # list rather than crashing with "unknown command <id>". Same path
        # the user gets from `bt <resource>` or `bt <resource> --help`.
        sub_index = id_arity
        if len(tokens) <= sub_index and all(not t.startswith("-") for t in tokens):
            return app([])
        # New form: <id...> <id-bearing sub-command> ...rest. Detect by
        # looking at the (id_arity)-th token; if it names a sub-command
        # that takes ids, rewrite the token order to match what the inner
        # cyclopts app expects (sub-command first, then ids).
        if len(tokens) > sub_index and tokens[sub_index] in id_subcommands:
            sub = tokens[sub_index]
            rest = list(tokens[sub_index + 1 :])
            # Bare leaf or explicit --help → print the leaf's parameter
            # list. Cyclopts' parent traversal would otherwise route help
            # to the resource app, hiding the leaf's required flags.
            if not rest or any(t in ("--help", "-h") for t in rest):
                app[sub].help_print()
                return None
            ids = list(tokens[:sub_index])
            if resource_label in {"subnet", "neuron"}:
                try:
                    netuid = int(first)
                except ValueError:
                    # Let Cyclopts produce its standard typed-argument error.
                    return app([sub, *ids, *rest])
                if resource_label == "neuron" and sub in {"incentive", "last-update"}:
                    _validate_mechanism_netuid(netuid)
                else:
                    _validate_netuid(netuid)
            return app([sub, *ids, *rest])
        if first not in id_subcommands:
            return app(list(tokens))
        raise cyclopts.UnknownCommandError(
            unused_tokens=[first],
            root_input_tokens=list(tokens),
            app=meta,
        )

    return meta


# ---------------------------------------------------------------------------
# subnet resource
# ---------------------------------------------------------------------------


_SUBNET_ID_SUBCOMMANDS = frozenset(
    {
        "price",
        "registration-cost",
        "tao-depth",
        "alpha-depth",
        "alpha-supply",
        "moving-price",
        "volume",
        "emission-share",
        "burn-rate",
        "burnrate",
        "ema-tao-flow",
        "hyperparams",
        "identity",
        "depth-for-trade",
    }
)
"""Subnet sub-commands that take ``<netuid>`` as a positional id.

This is the complete set — every subnet sub-command is id-bearing.
Chain-wide events (``subnet-registered`` etc.) live under ``bt event``
not under any per-resource path, so the resource-app help table stays
homogeneous and cyclopts' default rendering doesn't need filtering.
"""


def _build_subnet_app() -> cyclopts.App:
    app = cyclopts.App(name="subnet", help="Watch a Bittensor subnet.")

    @app.command(name="price")
    def subnet_price(
        netuid: Annotated[int, cyclopts.Parameter(help="Subnet netuid.", show=False)],
        *,
        below: Annotated[
            float | None, cyclopts.Parameter(name="--below", help="Fire when value < threshold.")
        ] = None,
        above: Annotated[
            float | None, cyclopts.Parameter(name="--above", help="Fire when value > threshold.")
        ] = None,
        drop_pct: Annotated[
            float | None,
            cyclopts.Parameter(name="--drop-pct", help="Fire when value drops by N%."),
        ] = None,
        rise_pct: Annotated[
            float | None,
            cyclopts.Parameter(name="--rise-pct", help="Fire when value rises by N%."),
        ] = None,
        move_pct: Annotated[
            float | None,
            cyclopts.Parameter(
                name="--move-pct", help="Fire when value moves by N% (either direction)."
            ),
        ] = None,
        window_time: Annotated[
            str | None,
            cyclopts.Parameter(name="--window-time", help="Window duration (e.g. 1h, 30m)."),
        ] = None,
        window_blocks: Annotated[
            int | None,
            cyclopts.Parameter(name="--window-blocks", help="Window length in blocks."),
        ] = None,
        window_epochs: Annotated[
            int | None,
            cyclopts.Parameter(name="--window-epochs", help="Window length in epochs."),
        ] = None,
        rpc_url: Annotated[
            str | None,
            cyclopts.Parameter(
                name="--rpc-url",
                env_var="CHAINWAKE_BT_RPC_URL",
                help="Override RPC endpoint.",
            ),
        ] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[
            str | None,
            cyclopts.Parameter(name="--name", help="Human-readable watcher label."),
        ] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[
            str | None,
            cyclopts.Parameter(
                name="--max-runtime",
                help="Hard limit (e.g. 10m, 2h, 1d). Default: unbounded.",
            ),
        ] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        """Alpha price in TAO (threshold or delta)."""
        max_secs = _parse_max_runtime(max_runtime)
        is_delta = any(f is not None for f in (drop_pct, rise_pct, move_pct))
        is_threshold = below is not None or above is not None
        has_window = any(f is not None for f in (window_time, window_blocks, window_epochs))

        if not is_threshold and not is_delta:
            _user_error("one of --below, --above, --drop-pct, --rise-pct, --move-pct is required")
        if is_threshold and is_delta:
            _user_error(
                "--below/--above and --drop-pct/--rise-pct/--move-pct are mutually exclusive"
            )
        if is_threshold and has_window:
            _user_error(
                "--window-time/--window-blocks/--window-epochs are only valid"
                " with delta flags (--drop-pct, --rise-pct, --move-pct)"
            )

        if is_delta:
            delta_op, delta_val, window_unit, window_val = _resolve_delta(
                drop_pct=drop_pct,
                rise_pct=rise_pct,
                move_pct=move_pct,
                window_time=window_time,
                window_blocks=window_blocks,
                window_epochs=window_epochs,
            )
            exit_code = asyncio.run(
                dispatch_delta(
                    resource="subnet",
                    path_params={"netuid": str(netuid)},
                    sub_resource="pool.price",
                    entry_path="subnet.{netuid}.pool.price",
                    operator=delta_op,
                    target=delta_val,
                    window_unit=window_unit,
                    window_value=window_val,
                    rpc_url=_resolve_rpc(rpc_url),
                    max_runtime_seconds=max_secs,
                    poll_seconds=_DEFAULT_POLL,
                    invocation=_invocation(),
                    out_uris=out or [],
                    name=name,
                    max_ru=max_ru,
                    api_key=resolve_api_key(api_key, "bt"),
                )
            )
            raise SystemExit(exit_code)

        operator, target = _resolve_threshold(below, above)
        exit_code = asyncio.run(
            dispatch_threshold(
                resource="subnet",
                path_params={"netuid": str(netuid)},
                sub_resource="pool.price",
                entry_path="subnet.{netuid}.pool.price",
                operator=operator,
                target=target,
                rpc_url=_resolve_rpc(rpc_url),
                max_runtime_seconds=max_secs,
                poll_seconds=_DEFAULT_POLL,
                invocation=_invocation(),
                out_uris=out or [],
                name=name,
                max_ru=max_ru,
                api_key=resolve_api_key(api_key, "bt"),
            )
        )
        raise SystemExit(exit_code)

    @app.command(name="registration-cost")
    def subnet_registration_cost(
        netuid: Annotated[int, cyclopts.Parameter(help="Subnet netuid.", show=False)],
        *,
        below: Annotated[
            float | None, cyclopts.Parameter(name="--below", help="Fire when value < threshold.")
        ] = None,
        above: Annotated[
            float | None, cyclopts.Parameter(name="--above", help="Fire when value > threshold.")
        ] = None,
        rpc_url: Annotated[str | None, cyclopts.Parameter(name="--rpc-url")] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        """Subnet registration cost in TAO (threshold)."""
        max_secs = _parse_max_runtime(max_runtime)
        operator, target = _resolve_threshold(below, above)
        exit_code = asyncio.run(
            dispatch_threshold(
                resource="subnet",
                path_params={"netuid": str(netuid)},
                sub_resource="registration-cost",
                entry_path="subnet.{netuid}.registration-cost",
                operator=operator,
                target=target,
                rpc_url=_resolve_rpc(rpc_url),
                max_runtime_seconds=max_secs,
                poll_seconds=_DEFAULT_POLL,
                invocation=_invocation(),
                out_uris=out or [],
                name=name,
                max_ru=max_ru,
                api_key=resolve_api_key(api_key, "bt"),
            )
        )
        raise SystemExit(exit_code)

    _register_subnet_numeric_commands(app)
    _register_subnet_state_commands(app)
    _register_subnet_depth_for_trade(app)

    return _install_id_first_meta(
        app, _SUBNET_ID_SUBCOMMANDS, id_label="<netuid>", resource_label="subnet"
    )


def _register_subnet_numeric_commands(app: cyclopts.App) -> None:
    """Register threshold-or-delta numeric subnet commands."""

    for name_, sub_resource, entry_path, doc in (
        (
            "tao-depth",
            "pool.tao-depth",
            "subnet.{netuid}.pool.tao-depth",
            "TAO reserve depth of the dTAO pool (threshold or delta).",
        ),
        (
            "alpha-depth",
            "pool.alpha-depth",
            "subnet.{netuid}.pool.alpha-depth",
            "Alpha reserve depth of the dTAO pool (threshold or delta).",
        ),
        (
            "emission-share",
            "emission-share",
            "subnet.{netuid}.emission-share",
            "Fraction of TAO emission routed to this subnet (threshold or delta).",
        ),
        (
            "burn-rate",
            "burn-rate",
            "subnet.{netuid}.burn-rate",
            (
                "Last-tempo fraction of miner emission withheld for subnet-owner "
                "hotkeys (threshold or delta)."
            ),
        ),
        (
            "alpha-supply",
            "pool.alpha-supply",
            "subnet.{netuid}.pool.alpha-supply",
            "Alpha token supply outside the dTAO pool (threshold or delta).",
        ),
        (
            "moving-price",
            "pool.moving-price",
            "subnet.{netuid}.pool.moving-price",
            "EMA price of the dTAO pool (threshold or delta).",
        ),
        (
            "volume",
            "pool.volume",
            "subnet.{netuid}.pool.volume",
            "Cumulative swap volume (TAO) for this subnet's dTAO pool (threshold or delta).",
        ),
        (
            "ema-tao-flow",
            "ema-tao-flow",
            "subnet.{netuid}.ema-tao-flow",
            "EMA of TAO inflow/outflow in TAO; signed (threshold or delta).",
        ),
    ):
        _bind_subnet_numeric(app, name_, sub_resource, entry_path, doc)


def _bind_subnet_numeric(
    app: cyclopts.App,
    command_name: str,
    sub_resource: str,
    entry_path: str,
    docstring: str,
) -> None:
    command_alias = "burnrate" if command_name == "burn-rate" else None

    @app.command(name=command_name, alias=command_alias)
    def cmd(
        netuid: Annotated[int, cyclopts.Parameter(help="Subnet netuid.", show=False)],
        *,
        below: Annotated[float | None, cyclopts.Parameter(name="--below")] = None,
        above: Annotated[float | None, cyclopts.Parameter(name="--above")] = None,
        drop_pct: Annotated[float | None, cyclopts.Parameter(name="--drop-pct")] = None,
        rise_pct: Annotated[float | None, cyclopts.Parameter(name="--rise-pct")] = None,
        move_pct: Annotated[float | None, cyclopts.Parameter(name="--move-pct")] = None,
        window_time: Annotated[
            str | None,
            cyclopts.Parameter(
                name="--window-time",
                help="Optional rolling duration; omit all windows for watcher-start baseline.",
            ),
        ] = None,
        window_blocks: Annotated[
            int | None,
            cyclopts.Parameter(
                name="--window-blocks",
                help="Optional rolling block count; omit all windows for watcher-start baseline.",
            ),
        ] = None,
        window_epochs: Annotated[
            int | None,
            cyclopts.Parameter(
                name="--window-epochs",
                help="Optional rolling epoch count; omit all windows for watcher-start baseline.",
            ),
        ] = None,
        rpc_url: Annotated[str | None, cyclopts.Parameter(name="--rpc-url")] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        max_secs = _parse_max_runtime(max_runtime)
        exit_code = asyncio.run(
            _dispatch_numeric(
                resource="subnet",
                path_params={"netuid": str(netuid)},
                sub_resource=sub_resource,
                entry_path=entry_path,
                drop_pct=drop_pct,
                rise_pct=rise_pct,
                move_pct=move_pct,
                window_time=window_time,
                window_blocks=window_blocks,
                window_epochs=window_epochs,
                below=below,
                above=above,
                rpc_url=_resolve_rpc(rpc_url),
                max_runtime_seconds=max_secs,
                poll_seconds=_DEFAULT_POLL,
                invocation=_invocation(),
                out_uris=out or [],
                name=name,
                max_ru=max_ru,
                api_key=resolve_api_key(api_key, "bt"),
            )
        )
        raise SystemExit(exit_code)

    cmd.__doc__ = docstring


def _register_subnet_state_commands(app: cyclopts.App) -> None:
    @app.command(name="hyperparams")
    def subnet_hyperparams(
        netuid: Annotated[int, cyclopts.Parameter(help="Subnet netuid.", show=False)],
        *,
        on_change: Annotated[
            bool, cyclopts.Parameter(name="--on-change", negative="", help="Fire on any change.")
        ] = False,
        rpc_url: Annotated[str | None, cyclopts.Parameter(name="--rpc-url")] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        """Fire when any subnet hyperparameter changes (--on-change required)."""
        if not on_change:
            _user_error("--on-change required for hyperparams watcher")
        max_secs = _parse_max_runtime(max_runtime)
        exit_code = asyncio.run(
            dispatch_state(
                resource="subnet",
                path_params={"netuid": str(netuid)},
                sub_resource="hyperparams",
                entry_path="subnet.{netuid}.hyperparams",
                operator="on-change",
                target=None,
                rpc_url=_resolve_rpc(rpc_url),
                max_runtime_seconds=max_secs,
                poll_seconds=_DEFAULT_POLL,
                invocation=_invocation(),
                out_uris=out or [],
                name=name,
                max_ru=max_ru,
                api_key=resolve_api_key(api_key, "bt"),
            )
        )
        raise SystemExit(exit_code)

    @app.command(name="identity")
    def subnet_identity(
        netuid: Annotated[int, cyclopts.Parameter(help="Subnet netuid.", show=False)],
        *,
        on_change: Annotated[
            bool, cyclopts.Parameter(name="--on-change", negative="", help="Fire on any change.")
        ] = False,
        rpc_url: Annotated[str | None, cyclopts.Parameter(name="--rpc-url")] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        """Fire when subnet on-chain identity changes."""
        max_secs = _parse_max_runtime(max_runtime)
        condition = _resolve_on_change_only(on_change)
        exit_code = asyncio.run(
            dispatch_state(
                resource="subnet",
                path_params={"netuid": str(netuid)},
                sub_resource="identity",
                entry_path="subnet.{netuid}.identity",
                operator=condition.kind,
                target=_state_target(condition),
                rpc_url=_resolve_rpc(rpc_url),
                max_runtime_seconds=max_secs,
                poll_seconds=_DEFAULT_POLL,
                invocation=_invocation(),
                out_uris=out or [],
                name=name,
                max_ru=max_ru,
                api_key=resolve_api_key(api_key, "bt"),
            )
        )
        raise SystemExit(exit_code)


def _register_subnet_depth_for_trade(app: cyclopts.App) -> None:
    @app.command(name="depth-for-trade")
    def subnet_depth_for_trade(
        netuid: Annotated[int, cyclopts.Parameter(help="Subnet netuid.", show=False)],
        *,
        size: Annotated[
            float | None,
            cyclopts.Parameter(name="--size", help="Trade size in TAO."),
        ] = None,
        max_bps: Annotated[
            float | None,
            cyclopts.Parameter(name="--max-bps", help="Slippage budget in basis points."),
        ] = None,
        below: Annotated[float | None, cyclopts.Parameter(name="--below")] = None,
        above: Annotated[float | None, cyclopts.Parameter(name="--above")] = None,
        rpc_url: Annotated[str | None, cyclopts.Parameter(name="--rpc-url")] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        """Margin (bps) for a trade of --size TAO within --max-bps slippage (threshold)."""
        if size is None:
            _user_error("--size required for depth-for-trade watcher")
        if max_bps is None:
            _user_error("--max-bps required for depth-for-trade watcher")
        if not math.isfinite(size) or size <= 0:
            _user_error("--size must be finite and greater than zero")
        if not math.isfinite(max_bps) or max_bps <= 0:
            _user_error("--max-bps must be finite and greater than zero")
        max_secs = _parse_max_runtime(max_runtime)
        operator, target = _resolve_threshold(below, above)
        exit_code = asyncio.run(
            dispatch_threshold(
                resource="subnet",
                path_params={"netuid": str(netuid)},
                sub_resource="pool.depth-for-trade",
                entry_path="subnet.{netuid}.pool.depth-for-trade",
                operator=operator,
                target=target,
                rpc_url=_resolve_rpc(rpc_url),
                max_runtime_seconds=max_secs,
                poll_seconds=_DEFAULT_POLL,
                invocation=_invocation(),
                out_uris=out or [],
                name=name,
                max_ru=max_ru,
                api_key=resolve_api_key(api_key, "bt"),
                read_args={"size": size, "max_bps": max_bps},
            )
        )
        raise SystemExit(exit_code)


# ---------------------------------------------------------------------------
# validator resource
# ---------------------------------------------------------------------------


_VALIDATOR_ID_SUBCOMMANDS = frozenset(
    {
        "weights",
        "commission",
        "dividends-alpha",
        "stake-alpha",
        "child-keys",
        "identity",
    }
)
"""Validator sub-commands that take ``<hotkey>`` as a positional id."""


def _build_validator_app() -> cyclopts.App:
    app = cyclopts.App(name="validator", help="Watch a Bittensor validator.")

    @app.command(name="weights")
    def validator_weights(
        hotkey: Annotated[str, cyclopts.Parameter(help="Validator hotkey (SS58).", show=False)],
        *,
        netuid: Annotated[
            int,
            cyclopts.Parameter(
                name="--netuid",
                help="Subnet whose weight activity is monitored. Default: 1.",
            ),
        ] = 1,
        mechid: Annotated[int, _MECHID_PARAM] = 0,
        silent_for: Annotated[
            str | None,
            cyclopts.Parameter(
                name="--silent-for",
                help="Fire after no weight-setting for this duration (e.g. '3epochs', '10m').",
            ),
        ] = None,
        rpc_url: Annotated[str | None, cyclopts.Parameter(name="--rpc-url")] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        """Liveness watch on weight-setting activity."""
        hotkey = _validate_ss58_address(hotkey, "<hotkey>")
        if silent_for is None:
            _user_error("--silent-for required for weights watcher")
        max_secs = _parse_max_runtime(max_runtime)
        validated_silent_for = _validate_duration_flag(silent_for, "--silent-for")
        validated_netuid = _validate_mechanism_netuid(netuid)
        validated_mechid = _validate_mechid(mechid)
        exit_code = asyncio.run(
            dispatch_liveness(
                resource="validator",
                path_params={"hotkey": hotkey},
                sub_resource="weights",
                entry_path="validator.{hotkey}.weights",
                silent_for=validated_silent_for,
                rpc_url=_resolve_rpc(rpc_url),
                max_runtime_seconds=max_secs,
                poll_seconds=_DEFAULT_POLL,
                invocation=_invocation(),
                out_uris=out or [],
                name=name,
                max_ru=max_ru,
                api_key=resolve_api_key(api_key, "bt"),
                read_args={"netuid": validated_netuid, "mechid": validated_mechid},
            )
        )
        raise SystemExit(exit_code)

    @app.command(name="commission")
    def validator_commission(
        hotkey: Annotated[str, cyclopts.Parameter(help="Validator hotkey (SS58).", show=False)],
        *,
        on_change: Annotated[
            bool, cyclopts.Parameter(name="--on-change", negative="", help="Fire on any change.")
        ] = False,
        changes_to: Annotated[float | None, cyclopts.Parameter(name="--changes-to")] = None,
        changes_from: Annotated[float | None, cyclopts.Parameter(name="--changes-from")] = None,
        rpc_url: Annotated[str | None, cyclopts.Parameter(name="--rpc-url")] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        """Fire when validator commission changes."""
        hotkey = _validate_ss58_address(hotkey, "<hotkey>")
        max_secs = _parse_max_runtime(max_runtime)
        condition = _resolve_commission_state(on_change, changes_to, changes_from)
        exit_code = asyncio.run(
            dispatch_state(
                resource="validator",
                path_params={"hotkey": hotkey},
                sub_resource="commission",
                entry_path="validator.{hotkey}.commission",
                operator=condition.kind,
                target=_state_target(condition),
                rpc_url=_resolve_rpc(rpc_url),
                max_runtime_seconds=max_secs,
                poll_seconds=_DEFAULT_POLL,
                invocation=_invocation(),
                out_uris=out or [],
                name=name,
                max_ru=max_ru,
                api_key=resolve_api_key(api_key, "bt"),
            )
        )
        raise SystemExit(exit_code)

    @app.command(name="dividends-alpha")
    def validator_dividends(
        hotkey: Annotated[str, cyclopts.Parameter(help="Validator hotkey.", show=False)],
        *,
        netuid: Annotated[
            int,
            cyclopts.Parameter(
                name="--netuid",
                help="Subnet whose alpha-token dividends to watch.",
            ),
        ],
        below: Annotated[float | None, cyclopts.Parameter(name="--below")] = None,
        above: Annotated[float | None, cyclopts.Parameter(name="--above")] = None,
        drop_pct: Annotated[float | None, cyclopts.Parameter(name="--drop-pct")] = None,
        rise_pct: Annotated[float | None, cyclopts.Parameter(name="--rise-pct")] = None,
        move_pct: Annotated[float | None, cyclopts.Parameter(name="--move-pct")] = None,
        window_time: Annotated[str | None, cyclopts.Parameter(name="--window-time")] = None,
        window_blocks: Annotated[int | None, cyclopts.Parameter(name="--window-blocks")] = None,
        window_epochs: Annotated[int | None, cyclopts.Parameter(name="--window-epochs")] = None,
        rpc_url: Annotated[str | None, cyclopts.Parameter(name="--rpc-url")] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        """Watch per-subnet validator dividends denominated in alpha."""
        hotkey = _validate_ss58_address(hotkey, "<hotkey>")
        max_secs = _parse_max_runtime(max_runtime)
        validated_netuid = _validate_netuid(netuid)
        exit_code = asyncio.run(
            _dispatch_numeric(
                resource="validator",
                path_params={"netuid": str(validated_netuid), "hotkey": hotkey},
                sub_resource="dividends-alpha",
                entry_path="validator.{netuid}.{hotkey}.dividends-alpha",
                drop_pct=drop_pct,
                rise_pct=rise_pct,
                move_pct=move_pct,
                window_time=window_time,
                window_blocks=window_blocks,
                window_epochs=window_epochs,
                below=below,
                above=above,
                rpc_url=_resolve_rpc(rpc_url),
                max_runtime_seconds=max_secs,
                poll_seconds=_DEFAULT_POLL,
                invocation=_invocation(),
                out_uris=out or [],
                name=name,
                max_ru=max_ru,
                api_key=resolve_api_key(api_key, "bt"),
            )
        )
        raise SystemExit(exit_code)

    @app.command(name="stake-alpha")
    def validator_stake(
        hotkey: Annotated[str, cyclopts.Parameter(help="Validator hotkey.", show=False)],
        *,
        netuid: Annotated[
            int,
            cyclopts.Parameter(
                name="--netuid",
                help="Subnet whose alpha-token stake to watch.",
            ),
        ],
        below: Annotated[float | None, cyclopts.Parameter(name="--below")] = None,
        above: Annotated[float | None, cyclopts.Parameter(name="--above")] = None,
        drop_pct: Annotated[float | None, cyclopts.Parameter(name="--drop-pct")] = None,
        rise_pct: Annotated[float | None, cyclopts.Parameter(name="--rise-pct")] = None,
        move_pct: Annotated[float | None, cyclopts.Parameter(name="--move-pct")] = None,
        window_time: Annotated[str | None, cyclopts.Parameter(name="--window-time")] = None,
        window_blocks: Annotated[int | None, cyclopts.Parameter(name="--window-blocks")] = None,
        window_epochs: Annotated[int | None, cyclopts.Parameter(name="--window-epochs")] = None,
        rpc_url: Annotated[str | None, cyclopts.Parameter(name="--rpc-url")] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        """Watch per-subnet validator stake denominated in alpha."""
        hotkey = _validate_ss58_address(hotkey, "<hotkey>")
        max_secs = _parse_max_runtime(max_runtime)
        validated_netuid = _validate_netuid(netuid)
        exit_code = asyncio.run(
            _dispatch_numeric(
                resource="validator",
                path_params={"netuid": str(validated_netuid), "hotkey": hotkey},
                sub_resource="stake-alpha",
                entry_path="validator.{netuid}.{hotkey}.stake-alpha",
                drop_pct=drop_pct,
                rise_pct=rise_pct,
                move_pct=move_pct,
                window_time=window_time,
                window_blocks=window_blocks,
                window_epochs=window_epochs,
                below=below,
                above=above,
                rpc_url=_resolve_rpc(rpc_url),
                max_runtime_seconds=max_secs,
                poll_seconds=_DEFAULT_POLL,
                invocation=_invocation(),
                out_uris=out or [],
                name=name,
                max_ru=max_ru,
                api_key=resolve_api_key(api_key, "bt"),
            )
        )
        raise SystemExit(exit_code)

    @app.command(name="child-keys")
    def validator_child_keys(
        hotkey: Annotated[str, cyclopts.Parameter(help="Validator hotkey.", show=False)],
        *,
        on_change: Annotated[
            bool, cyclopts.Parameter(name="--on-change", negative="", help="Fire on any change.")
        ] = False,
        rpc_url: Annotated[str | None, cyclopts.Parameter(name="--rpc-url")] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        """Fire when child keys change."""
        hotkey = _validate_ss58_address(hotkey, "<hotkey>")
        if not on_change:
            _user_error("--on-change required for child-keys watcher")
        max_secs = _parse_max_runtime(max_runtime)
        exit_code = asyncio.run(
            dispatch_state(
                resource="validator",
                path_params={"hotkey": hotkey},
                sub_resource="child-keys",
                entry_path="validator.{hotkey}.child-keys",
                operator="on-change",
                target=None,
                rpc_url=_resolve_rpc(rpc_url),
                max_runtime_seconds=max_secs,
                poll_seconds=_DEFAULT_POLL,
                invocation=_invocation(),
                out_uris=out or [],
                name=name,
                max_ru=max_ru,
                api_key=resolve_api_key(api_key, "bt"),
            )
        )
        raise SystemExit(exit_code)

    @app.command(name="identity")
    def validator_identity(
        hotkey: Annotated[str, cyclopts.Parameter(help="Validator hotkey (SS58).", show=False)],
        *,
        on_change: Annotated[
            bool, cyclopts.Parameter(name="--on-change", negative="", help="Fire on any change.")
        ] = False,
        rpc_url: Annotated[str | None, cyclopts.Parameter(name="--rpc-url")] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        """Fire when validator on-chain identity changes."""
        hotkey = _validate_ss58_address(hotkey, "<hotkey>")
        max_secs = _parse_max_runtime(max_runtime)
        condition = _resolve_on_change_only(on_change)
        exit_code = asyncio.run(
            dispatch_state(
                resource="validator",
                path_params={"hotkey": hotkey},
                sub_resource="identity",
                entry_path="validator.{hotkey}.identity",
                operator=condition.kind,
                target=_state_target(condition),
                rpc_url=_resolve_rpc(rpc_url),
                max_runtime_seconds=max_secs,
                poll_seconds=_DEFAULT_POLL,
                invocation=_invocation(),
                out_uris=out or [],
                name=name,
                max_ru=max_ru,
                api_key=resolve_api_key(api_key, "bt"),
            )
        )
        raise SystemExit(exit_code)

    return _install_id_first_meta(
        app, _VALIDATOR_ID_SUBCOMMANDS, id_label="<hotkey>", resource_label="validator"
    )


# ---------------------------------------------------------------------------
# neuron resource
# ---------------------------------------------------------------------------


_NEURON_ID_SUBCOMMANDS = frozenset(
    {
        "last-update",
        "incentive",
        "dividends",
        "stake-alpha",
        "blocks-until-immunity-expires",
    }
)
"""Neuron sub-commands; each takes ``<netuid> <hotkey>`` as positionals."""


def _build_neuron_app() -> cyclopts.App:
    app = cyclopts.App(name="neuron", help="Watch a registered Bittensor neuron.")

    @app.command(name="last-update")
    def neuron_last_update(
        netuid: Annotated[int, cyclopts.Parameter(help="Subnet netuid.", show=False)],
        hotkey: Annotated[str, cyclopts.Parameter(help="Neuron hotkey (SS58).", show=False)],
        *,
        mechid: Annotated[int, _MECHID_PARAM] = 0,
        silent_for: Annotated[
            str | None,
            cyclopts.Parameter(
                name="--silent-for",
                help="Fire after no last-update for this duration.",
            ),
        ] = None,
        rpc_url: Annotated[str | None, cyclopts.Parameter(name="--rpc-url")] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        """Liveness watch on neuron last-update."""
        hotkey = _validate_ss58_address(hotkey, "<hotkey>")
        if silent_for is None:
            _user_error("--silent-for required for last-update watcher")
        max_secs = _parse_max_runtime(max_runtime)
        validated_silent_for = _validate_duration_flag(silent_for, "--silent-for")
        validated_mechid = _validate_mechid(mechid)
        exit_code = asyncio.run(
            dispatch_liveness(
                resource="neuron",
                path_params={"netuid": str(netuid), "hotkey": hotkey},
                sub_resource="last-update",
                entry_path="neuron.{netuid}.{hotkey}.last-update",
                silent_for=validated_silent_for,
                rpc_url=_resolve_rpc(rpc_url),
                max_runtime_seconds=max_secs,
                poll_seconds=_DEFAULT_POLL,
                invocation=_invocation(),
                out_uris=out or [],
                name=name,
                max_ru=max_ru,
                api_key=resolve_api_key(api_key, "bt"),
                read_args={"mechid": validated_mechid},
            )
        )
        raise SystemExit(exit_code)

    @app.command(name="incentive")
    def neuron_incentive(
        netuid: Annotated[int, cyclopts.Parameter(help="Subnet netuid.", show=False)],
        hotkey: Annotated[str, cyclopts.Parameter(help="Neuron hotkey (SS58).", show=False)],
        *,
        mechid: Annotated[int, _MECHID_PARAM] = 0,
        below: Annotated[float | None, cyclopts.Parameter(name="--below")] = None,
        above: Annotated[float | None, cyclopts.Parameter(name="--above")] = None,
        drop_pct: Annotated[float | None, cyclopts.Parameter(name="--drop-pct")] = None,
        rise_pct: Annotated[float | None, cyclopts.Parameter(name="--rise-pct")] = None,
        move_pct: Annotated[float | None, cyclopts.Parameter(name="--move-pct")] = None,
        window_time: Annotated[str | None, cyclopts.Parameter(name="--window-time")] = None,
        window_blocks: Annotated[int | None, cyclopts.Parameter(name="--window-blocks")] = None,
        window_epochs: Annotated[int | None, cyclopts.Parameter(name="--window-epochs")] = None,
        rpc_url: Annotated[str | None, cyclopts.Parameter(name="--rpc-url")] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        """Watch neuron incentive (threshold or delta)."""
        hotkey = _validate_ss58_address(hotkey, "<hotkey>")
        max_secs = _parse_max_runtime(max_runtime)
        validated_mechid = _validate_mechid(mechid)
        exit_code = asyncio.run(
            _dispatch_numeric(
                resource="neuron",
                path_params={"netuid": str(netuid), "hotkey": hotkey},
                sub_resource="incentive",
                entry_path="neuron.{netuid}.{hotkey}.incentive",
                drop_pct=drop_pct,
                rise_pct=rise_pct,
                move_pct=move_pct,
                window_time=window_time,
                window_blocks=window_blocks,
                window_epochs=window_epochs,
                below=below,
                above=above,
                rpc_url=_resolve_rpc(rpc_url),
                max_runtime_seconds=max_secs,
                poll_seconds=_DEFAULT_POLL,
                invocation=_invocation(),
                out_uris=out or [],
                name=name,
                max_ru=max_ru,
                api_key=resolve_api_key(api_key, "bt"),
                read_args={"mechid": validated_mechid},
            )
        )
        raise SystemExit(exit_code)

    _register_neuron_numeric_commands(app)
    _register_neuron_immunity_command(app)

    return _install_id_first_meta(
        app,
        _NEURON_ID_SUBCOMMANDS,
        id_label="<netuid> <hotkey>",
        resource_label="neuron",
        id_arity=2,
    )


def _register_neuron_numeric_commands(app: cyclopts.App) -> None:
    """Register threshold-or-delta neuron commands sharing the incentive shape."""

    for command_name, sub_resource, entry_path, doc in (
        (
            "dividends",
            "dividends",
            "neuron.{netuid}.{hotkey}.dividends",
            "Watch neuron dividends (threshold or delta).",
        ),
        (
            "stake-alpha",
            "stake-alpha",
            "neuron.{netuid}.{hotkey}.stake-alpha",
            "Watch neuron stake denominated in subnet alpha (threshold or delta).",
        ),
    ):
        _bind_neuron_numeric(app, command_name, sub_resource, entry_path, doc)


def _bind_neuron_numeric(
    app: cyclopts.App,
    command_name: str,
    sub_resource: str,
    entry_path: str,
    docstring: str,
) -> None:
    @app.command(name=command_name)
    def cmd(
        netuid: Annotated[int, cyclopts.Parameter(help="Subnet netuid.", show=False)],
        hotkey: Annotated[str, cyclopts.Parameter(help="Neuron hotkey (SS58).", show=False)],
        *,
        below: Annotated[float | None, cyclopts.Parameter(name="--below")] = None,
        above: Annotated[float | None, cyclopts.Parameter(name="--above")] = None,
        drop_pct: Annotated[float | None, cyclopts.Parameter(name="--drop-pct")] = None,
        rise_pct: Annotated[float | None, cyclopts.Parameter(name="--rise-pct")] = None,
        move_pct: Annotated[float | None, cyclopts.Parameter(name="--move-pct")] = None,
        window_time: Annotated[str | None, cyclopts.Parameter(name="--window-time")] = None,
        window_blocks: Annotated[int | None, cyclopts.Parameter(name="--window-blocks")] = None,
        window_epochs: Annotated[int | None, cyclopts.Parameter(name="--window-epochs")] = None,
        rpc_url: Annotated[str | None, cyclopts.Parameter(name="--rpc-url")] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        hotkey = _validate_ss58_address(hotkey, "<hotkey>")
        max_secs = _parse_max_runtime(max_runtime)
        exit_code = asyncio.run(
            _dispatch_numeric(
                resource="neuron",
                path_params={"netuid": str(netuid), "hotkey": hotkey},
                sub_resource=sub_resource,
                entry_path=entry_path,
                drop_pct=drop_pct,
                rise_pct=rise_pct,
                move_pct=move_pct,
                window_time=window_time,
                window_blocks=window_blocks,
                window_epochs=window_epochs,
                below=below,
                above=above,
                rpc_url=_resolve_rpc(rpc_url),
                max_runtime_seconds=max_secs,
                poll_seconds=_DEFAULT_POLL,
                invocation=_invocation(),
                out_uris=out or [],
                name=name,
                max_ru=max_ru,
                api_key=resolve_api_key(api_key, "bt"),
            )
        )
        raise SystemExit(exit_code)

    cmd.__doc__ = docstring


def _register_neuron_immunity_command(app: cyclopts.App) -> None:
    @app.command(name="blocks-until-immunity-expires")
    def neuron_immunity(
        netuid: Annotated[int, cyclopts.Parameter(help="Subnet netuid.", show=False)],
        hotkey: Annotated[str, cyclopts.Parameter(help="Neuron hotkey (SS58).", show=False)],
        *,
        below: Annotated[float | None, cyclopts.Parameter(name="--below")] = None,
        above: Annotated[float | None, cyclopts.Parameter(name="--above")] = None,
        rpc_url: Annotated[str | None, cyclopts.Parameter(name="--rpc-url")] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        """Threshold on computed blocks-until-immunity-expires."""
        hotkey = _validate_ss58_address(hotkey, "<hotkey>")
        max_secs = _parse_max_runtime(max_runtime)
        operator, target = _resolve_threshold(below, above)
        exit_code = asyncio.run(
            dispatch_threshold(
                resource="neuron",
                path_params={"netuid": str(netuid), "hotkey": hotkey},
                sub_resource="blocks-until-immunity-expires",
                entry_path="neuron.{netuid}.{hotkey}.blocks-until-immunity-expires",
                operator=operator,
                target=target,
                rpc_url=_resolve_rpc(rpc_url),
                max_runtime_seconds=max_secs,
                poll_seconds=_DEFAULT_POLL,
                invocation=_invocation(),
                out_uris=out or [],
                name=name,
                max_ru=max_ru,
                api_key=resolve_api_key(api_key, "bt"),
            )
        )
        raise SystemExit(exit_code)


# ---------------------------------------------------------------------------
# account resource
# ---------------------------------------------------------------------------


_ACCOUNT_ID_SUBCOMMANDS = frozenset({"balance", "activity"})
"""Account sub-commands that take ``<coldkey>`` SS58 as a positional id."""


def _build_account_app() -> cyclopts.App:
    app = cyclopts.App(name="account", help="Watch a Bittensor coldkey account.")

    @app.command(name="balance")
    def account_balance(
        coldkey: Annotated[str, cyclopts.Parameter(help="Coldkey SS58 address.", show=False)],
        *,
        below: Annotated[float | None, cyclopts.Parameter(name="--below")] = None,
        above: Annotated[float | None, cyclopts.Parameter(name="--above")] = None,
        drop_pct: Annotated[float | None, cyclopts.Parameter(name="--drop-pct")] = None,
        rise_pct: Annotated[float | None, cyclopts.Parameter(name="--rise-pct")] = None,
        move_pct: Annotated[float | None, cyclopts.Parameter(name="--move-pct")] = None,
        window_time: Annotated[str | None, cyclopts.Parameter(name="--window-time")] = None,
        window_blocks: Annotated[int | None, cyclopts.Parameter(name="--window-blocks")] = None,
        window_epochs: Annotated[int | None, cyclopts.Parameter(name="--window-epochs")] = None,
        on_change: Annotated[bool, cyclopts.Parameter(name="--on-change", negative="")] = False,
        rpc_url: Annotated[str | None, cyclopts.Parameter(name="--rpc-url")] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        """Account balance (threshold, delta, or state)."""
        coldkey = _validate_ss58_address(coldkey, "<coldkey>")
        max_secs = _parse_max_runtime(max_runtime)
        is_delta = any(f is not None for f in (drop_pct, rise_pct, move_pct))
        if is_delta:
            delta_op, delta_val, window_unit, window_val = _resolve_delta(
                drop_pct=drop_pct,
                rise_pct=rise_pct,
                move_pct=move_pct,
                window_time=window_time,
                window_blocks=window_blocks,
                window_epochs=window_epochs,
            )
            exit_code = asyncio.run(
                dispatch_delta(
                    resource="account",
                    path_params={"coldkey": coldkey},
                    sub_resource="balance",
                    entry_path="account.{coldkey}.balance",
                    operator=delta_op,
                    target=delta_val,
                    window_unit=window_unit,
                    window_value=window_val,
                    rpc_url=_resolve_rpc(rpc_url),
                    max_runtime_seconds=max_secs,
                    poll_seconds=_DEFAULT_POLL,
                    invocation=_invocation(),
                    out_uris=out or [],
                    name=name,
                    max_ru=max_ru,
                    api_key=resolve_api_key(api_key, "bt"),
                )
            )
        elif on_change:
            exit_code = asyncio.run(
                dispatch_state(
                    resource="account",
                    path_params={"coldkey": coldkey},
                    sub_resource="balance",
                    entry_path="account.{coldkey}.balance",
                    operator="on-change",
                    target=None,
                    rpc_url=_resolve_rpc(rpc_url),
                    max_runtime_seconds=max_secs,
                    poll_seconds=_DEFAULT_POLL,
                    invocation=_invocation(),
                    out_uris=out or [],
                    name=name,
                    max_ru=max_ru,
                    api_key=resolve_api_key(api_key, "bt"),
                )
            )
        else:
            operator, target = _resolve_threshold(below, above)
            exit_code = asyncio.run(
                dispatch_threshold(
                    resource="account",
                    path_params={"coldkey": coldkey},
                    sub_resource="balance",
                    entry_path="account.{coldkey}.balance",
                    operator=operator,
                    target=target,
                    rpc_url=_resolve_rpc(rpc_url),
                    max_runtime_seconds=max_secs,
                    poll_seconds=_DEFAULT_POLL,
                    invocation=_invocation(),
                    out_uris=out or [],
                    name=name,
                    max_ru=max_ru,
                    api_key=resolve_api_key(api_key, "bt"),
                )
            )
        raise SystemExit(exit_code)

    @app.command(name="activity")
    def account_activity(
        coldkey: Annotated[str, cyclopts.Parameter(help="Coldkey SS58 address.", show=False)],
        *,
        silent_for: Annotated[
            str | None,
            cyclopts.Parameter(
                name="--silent-for", help="Fire after no activity for this duration."
            ),
        ] = None,
        rpc_url: Annotated[str | None, cyclopts.Parameter(name="--rpc-url")] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        """Liveness watch on account activity."""
        coldkey = _validate_ss58_address(coldkey, "<coldkey>")
        if silent_for is None:
            _user_error("--silent-for required for activity watcher")
        max_secs = _parse_max_runtime(max_runtime)
        validated_silent_for = _validate_duration_flag(silent_for, "--silent-for")
        exit_code = asyncio.run(
            dispatch_liveness(
                resource="account",
                path_params={"coldkey": coldkey},
                sub_resource="activity",
                entry_path="account.{coldkey}.activity",
                silent_for=validated_silent_for,
                rpc_url=_resolve_rpc(rpc_url),
                max_runtime_seconds=max_secs,
                poll_seconds=_DEFAULT_POLL,
                invocation=_invocation(),
                out_uris=out or [],
                name=name,
                max_ru=max_ru,
                api_key=resolve_api_key(api_key, "bt"),
            )
        )
        raise SystemExit(exit_code)

    return _install_id_first_meta(
        app, _ACCOUNT_ID_SUBCOMMANDS, id_label="<coldkey>", resource_label="account"
    )


# ---------------------------------------------------------------------------
# network resource
# ---------------------------------------------------------------------------


def _build_network_app() -> cyclopts.App:
    app = cyclopts.App(name="network", help="Watch chain-wide Bittensor network values.")

    @app.command(name="tao-price")
    def network_tao_price(
        *,
        below: Annotated[
            float | None,
            cyclopts.Parameter(name="--below", help="Wake below this TAO/USD price."),
        ] = None,
        above: Annotated[
            float | None,
            cyclopts.Parameter(name="--above", help="Wake above this TAO/USD price."),
        ] = None,
        drop_pct: Annotated[float | None, cyclopts.Parameter(name="--drop-pct")] = None,
        rise_pct: Annotated[float | None, cyclopts.Parameter(name="--rise-pct")] = None,
        move_pct: Annotated[float | None, cyclopts.Parameter(name="--move-pct")] = None,
        window_time: Annotated[str | None, cyclopts.Parameter(name="--window-time")] = None,
        rpc_url: Annotated[str | None, cyclopts.Parameter(name="--rpc-url")] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        """Watch TAO's aggregate USD price."""
        exit_code = asyncio.run(
            _dispatch_numeric(
                resource="network",
                path_params={},
                sub_resource="tao-price",
                entry_path="network.tao-price",
                drop_pct=drop_pct,
                rise_pct=rise_pct,
                move_pct=move_pct,
                window_time=window_time,
                window_blocks=None,
                window_epochs=None,
                below=below,
                above=above,
                rpc_url=_resolve_rpc(rpc_url),
                max_runtime_seconds=_parse_max_runtime(max_runtime),
                poll_seconds=_DEFAULT_POLL,
                invocation=_invocation(),
                out_uris=out or [],
                name=name,
                max_ru=max_ru,
                api_key=resolve_api_key(api_key, "bt"),
            )
        )
        raise SystemExit(exit_code)

    @app.command(name="subnet-registration-cost")
    def network_subnet_reg_cost(
        *,
        below: Annotated[float | None, cyclopts.Parameter(name="--below")] = None,
        above: Annotated[float | None, cyclopts.Parameter(name="--above")] = None,
        rpc_url: Annotated[str | None, cyclopts.Parameter(name="--rpc-url")] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        """Chain-wide subnet registration cost."""
        max_secs = _parse_max_runtime(max_runtime)
        operator, target = _resolve_threshold(below, above)
        exit_code = asyncio.run(
            dispatch_threshold(
                resource="network",
                path_params={},
                sub_resource="subnet-registration-cost",
                entry_path="network.subnet-registration-cost",
                operator=operator,
                target=target,
                rpc_url=_resolve_rpc(rpc_url),
                max_runtime_seconds=max_secs,
                poll_seconds=_DEFAULT_POLL,
                invocation=_invocation(),
                out_uris=out or [],
                name=name,
                max_ru=max_ru,
                api_key=resolve_api_key(api_key, "bt"),
            )
        )
        raise SystemExit(exit_code)

    @app.command(name="runtime-version")
    def network_runtime_version(
        *,
        on_change: Annotated[
            bool, cyclopts.Parameter(name="--on-change", negative="", help="Fire on any change.")
        ] = False,
        rpc_url: Annotated[str | None, cyclopts.Parameter(name="--rpc-url")] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        """Fire when runtime version changes."""
        if not on_change:
            _user_error("--on-change required for runtime-version watcher")
        max_secs = _parse_max_runtime(max_runtime)
        exit_code = asyncio.run(
            dispatch_state(
                resource="network",
                path_params={},
                sub_resource="runtime-version",
                entry_path="network.runtime-version",
                operator="on-change",
                target=None,
                rpc_url=_resolve_rpc(rpc_url),
                max_runtime_seconds=max_secs,
                poll_seconds=_DEFAULT_POLL,
                invocation=_invocation(),
                out_uris=out or [],
                name=name,
                max_ru=max_ru,
                api_key=resolve_api_key(api_key, "bt"),
            )
        )
        raise SystemExit(exit_code)

    @app.command(name="subnet-count")
    def network_subnet_count(
        *,
        below: Annotated[float | None, cyclopts.Parameter(name="--below")] = None,
        above: Annotated[float | None, cyclopts.Parameter(name="--above")] = None,
        drop_pct: Annotated[float | None, cyclopts.Parameter(name="--drop-pct")] = None,
        rise_pct: Annotated[float | None, cyclopts.Parameter(name="--rise-pct")] = None,
        move_pct: Annotated[float | None, cyclopts.Parameter(name="--move-pct")] = None,
        window_time: Annotated[str | None, cyclopts.Parameter(name="--window-time")] = None,
        window_blocks: Annotated[int | None, cyclopts.Parameter(name="--window-blocks")] = None,
        window_epochs: Annotated[int | None, cyclopts.Parameter(name="--window-epochs")] = None,
        rpc_url: Annotated[str | None, cyclopts.Parameter(name="--rpc-url")] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        """Watch total registered subnet count (threshold or delta)."""
        max_secs = _parse_max_runtime(max_runtime)
        exit_code = asyncio.run(
            _dispatch_numeric(
                resource="network",
                path_params={},
                sub_resource="subnet-count",
                entry_path="network.subnet-count",
                drop_pct=drop_pct,
                rise_pct=rise_pct,
                move_pct=move_pct,
                window_time=window_time,
                window_blocks=window_blocks,
                window_epochs=window_epochs,
                below=below,
                above=above,
                rpc_url=_resolve_rpc(rpc_url),
                max_runtime_seconds=max_secs,
                poll_seconds=_DEFAULT_POLL,
                invocation=_invocation(),
                out_uris=out or [],
                name=name,
                max_ru=max_ru,
                api_key=resolve_api_key(api_key, "bt"),
            )
        )
        raise SystemExit(exit_code)

    @app.command(name="on-runtime-upgraded")
    def network_on_runtime_upgraded(
        *,
        rpc_url: Annotated[
            str | None,
            cyclopts.Parameter(name="--rpc-url", env_var="CHAINWAKE_BT_RPC_URL"),
        ] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        """Fire on System.CodeUpdated (runtime upgrade)."""
        max_secs = _parse_max_runtime(max_runtime)
        exit_code = asyncio.run(
            dispatch_event(
                event_type="System.CodeUpdated",
                args_match=None,
                entry_path="network.--on-runtime-upgraded",
                rpc_url=rpc_url or DEFAULT_RPC_URL,
                max_runtime_seconds=max_secs,
                invocation=_invocation(),
                out_uris=list(out or []),
                name=name,
                max_ru=max_ru,
                api_key=resolve_api_key(api_key, "bt"),
                resource="network",
                sub_resource="on-runtime-upgraded",
            )
        )
        raise SystemExit(exit_code)

    return app


# ---------------------------------------------------------------------------
# event resource
# ---------------------------------------------------------------------------


def _build_event_app() -> cyclopts.App:
    app = cyclopts.App(name="event", help="Subscribe to chain-wide events by type.")

    @app.default
    def event_default(
        *,
        event_type: Annotated[
            str | None,
            cyclopts.Parameter(
                name="--type",
                help=(
                    "Verified friendly event name (e.g. 'transfer', 'swap'). "
                    "Use --type-raw for other Module.Event names."
                ),
            ),
        ] = None,
        type_raw: Annotated[
            str | None,
            cyclopts.Parameter(name="--type-raw", help="Raw Substrate event (Module.Event)."),
        ] = None,
        from_addr: Annotated[
            str | None,
            cyclopts.Parameter(
                name="--from",
                help="Filter to events whose decoded 'from' field equals this SS58 address.",
            ),
        ] = None,
        to_addr: Annotated[
            str | None,
            cyclopts.Parameter(
                name="--to",
                help="Filter to events whose decoded 'to' field equals this SS58 address.",
            ),
        ] = None,
        amount_min: Annotated[
            int | None,
            cyclopts.Parameter(
                name="--amount-min",
                help=(
                    "Filter to events whose decoded 'amount' (or 'value') is >= this value (rao)."
                ),
            ),
        ] = None,
        direction: Annotated[
            Literal["in", "out", "both"] | None,
            cyclopts.Parameter(
                name="--direction",
                help=(
                    "Filter by direction relative to --address: 'in' (received) or "
                    "'out' (sent); 'both' is a no-op kept for symmetry. Requires "
                    "--address."
                ),
            ),
        ] = None,
        address: Annotated[
            str | None,
            cyclopts.Parameter(
                name="--address",
                help=("SS58 address used by --direction. Required when --direction is set."),
            ),
        ] = None,
        rpc_url: Annotated[
            str | None,
            cyclopts.Parameter(name="--rpc-url", env_var="CHAINWAKE_BT_RPC_URL"),
        ] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        """Subscribe to chain-wide events by type."""
        if from_addr is not None:
            from_addr = _validate_ss58_address(from_addr, "--from")
        if to_addr is not None:
            to_addr = _validate_ss58_address(to_addr, "--to")
        if address is not None:
            address = _validate_ss58_address(address, "--address")
        if event_type is None and type_raw is None:
            _user_error("--type or --type-raw required for event watcher")
        if event_type is not None and type_raw is not None:
            _user_error("--type and --type-raw are mutually exclusive")
        if type_raw is not None and not is_raw_event_type(type_raw):
            _user_error("--type-raw must use Module.Event syntax")
        if event_type is not None and event_type not in FRIENDLY_EVENT_MAP:
            supported = ", ".join(sorted(FRIENDLY_EVENT_MAP))
            _user_error(
                f"unsupported friendly event {event_type!r}; supported names: {supported}. "
                "Use --type-raw <Module.Event> for other runtime events."
            )
        if amount_min is not None and amount_min < 0:
            _user_error(f"--amount-min must be non-negative, got {amount_min}")
        if direction is not None and address is None:
            _user_error("--direction requires --address (SS58)")
        max_secs = _parse_max_runtime(max_runtime)
        resolved_type = event_type or type_raw
        if resolved_type is None:
            _user_error("--type or --type-raw required for event watcher")
        entry_path = f"event.{event_type}" if event_type is not None else "event.--type-raw"
        args_match = _resolve_event_args_match(from_addr=from_addr, to_addr=to_addr)
        exit_code = asyncio.run(
            dispatch_event(
                event_type=resolved_type,
                args_match=args_match,
                entry_path=entry_path,
                rpc_url=rpc_url or DEFAULT_RPC_URL,
                max_runtime_seconds=max_secs,
                invocation=_invocation(),
                out_uris=list(out or []),
                name=name,
                max_ru=max_ru,
                api_key=resolve_api_key(api_key, "bt"),
                amount_min=amount_min,
                direction=direction,
                direction_address=address,
            )
        )
        raise SystemExit(exit_code)

    return app


def _resolve_event_args_match(
    *,
    from_addr: str | None,
    to_addr: str | None,
) -> dict[str, object] | None:
    """Build the args_match dict for the provider's EventFilter.

    Decoded Substrate Balances.Transfer (and similar) events expose 'from'
    and 'to' SS58 addresses as decoded args. Returns None when no filter
    is requested so the dispatcher and provider stay on the existing
    no-filter path.
    """
    args_match: dict[str, object] = {}
    if from_addr is not None:
        args_match["from"] = from_addr
    if to_addr is not None:
        args_match["to"] = to_addr
    return args_match or None


# ---------------------------------------------------------------------------
# tx command (registered directly on bt app)
# ---------------------------------------------------------------------------


def _register_tx_command(app: cyclopts.App) -> None:
    @app.command(name="tx")
    def tx_command(
        tx_hash: Annotated[str, cyclopts.Parameter(help="Transaction hash (0x...).")],
        *,
        finality: Annotated[
            Literal["included", "finalized"] | None,
            cyclopts.Parameter(
                name="--finality",
                help="Required finality level: 'included' or 'finalized'.",
            ),
        ] = None,
        rpc_url: Annotated[
            str | None,
            cyclopts.Parameter(name="--rpc-url", env_var="CHAINWAKE_BT_RPC_URL"),
        ] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        """Wait for a transaction to reach a finality level."""
        if finality not in ("included", "finalized"):
            _user_error(f"--finality must be 'included' or 'finalized', got {finality!r}")
        if finality is None:
            _user_error("--finality must be 'included' or 'finalized'")
        try:
            validated_tx_hash = validate_tx_hash(tx_hash)
        except ValueError as exc:
            _user_error(str(exc))
        max_secs = _parse_max_runtime(max_runtime)
        exit_code = asyncio.run(
            dispatch_tx(
                tx_hash=validated_tx_hash,
                finality=finality,
                entry_path="tx.{tx_hash}",
                rpc_url=rpc_url or DEFAULT_RPC_URL,
                max_runtime_seconds=max_secs,
                invocation=_invocation(),
                out_uris=list(out or []),
                name=name,
                max_ru=max_ru,
                api_key=resolve_api_key(api_key, "bt"),
            )
        )
        raise SystemExit(exit_code)


# ---------------------------------------------------------------------------
# bt top-level app
# ---------------------------------------------------------------------------


def build_bt_app() -> cyclopts.App:
    """Construct and return the ``bt`` cyclopts sub-app."""
    app = cyclopts.App(name="bt", help="Bittensor chain watchers.")

    app.command(_build_subnet_app(), name="subnet")
    app.command(_build_validator_app(), name="validator")
    app.command(_build_account_app(), name="account")
    app.command(_build_neuron_app(), name="neuron")
    app.command(_build_network_app())
    app.command(_build_event_app())
    _register_tx_command(app)

    return app


__all__ = ["BT_WIRED_WAKE_COMMANDS", "build_bt_app"]
