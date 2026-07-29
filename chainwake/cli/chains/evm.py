"""Profile-driven EVM command trees."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from typing import Annotated

import cyclopts

from chainwake.cli.chains.common import (
    dispatch_numeric,
    parse_max_runtime,
    resolve_api_key,
    user_error,
)
from chainwake.cli.chains.dispatch import dispatch_tx
from chainwake.core.tx_hash import validate_tx_hash
from chainwake.providers.evm import EvmChainProfile, EvmFeeModel

_OUT_PARAM = cyclopts.Parameter(
    name="--out",
    help="Output adapter URI (repeatable).",
    negative_iterable="",
)
_RPC_URL_PARAM = cyclopts.Parameter(
    name="--rpc-url",
    help="Override this chain's WebSocket RPC endpoint.",
)
_API_KEY_PARAM = cyclopts.Parameter(
    name="--api-key",
    help="Optional provider API key; falls back to the chain-specific then global environment.",
)
_MAX_RU_PARAM = cyclopts.Parameter(
    name="--max-ru",
    help="Registry-estimated observation budget; not a provider billing cap.",
)
_EVM_ADDRESS_LENGTH = 42
_ID_FIRST_MIN_TOKENS = 2


def _invocation() -> list[str]:
    return [os.path.basename(sys.argv[0]), *sys.argv[1:]]


def _resolve_rpc(profile: EvmChainProfile, rpc_url: str | None) -> str:
    return (
        rpc_url
        or os.environ.get(f"CHAINWAKE_{profile.alias.upper()}_RPC_URL")
        or profile.default_rpc
    )


def _resolve_max_ru(profile: EvmChainProfile, max_ru: int | None) -> int | None:
    if max_ru is not None:
        return max_ru
    raw = os.environ.get(f"CHAINWAKE_{profile.alias.upper()}_MAX_RU")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        user_error(f"CHAINWAKE_{profile.alias.upper()}_MAX_RU must be an integer")


def _fee_command(
    profile: EvmChainProfile,
    *,
    command_name: str,
    entry_path: str,
    summary: str,
) -> Callable[..., None]:
    """Build one numeric fee command with profile-owned environment names."""

    def command(
        *,
        below: Annotated[
            float | None,
            cyclopts.Parameter(name="--below", help="Wake below this fee in gwei."),
        ] = None,
        above: Annotated[
            float | None,
            cyclopts.Parameter(name="--above", help="Wake above this fee in gwei."),
        ] = None,
        drop_pct: Annotated[float | None, cyclopts.Parameter(name="--drop-pct")] = None,
        rise_pct: Annotated[float | None, cyclopts.Parameter(name="--rise-pct")] = None,
        move_pct: Annotated[float | None, cyclopts.Parameter(name="--move-pct")] = None,
        window_time: Annotated[str | None, cyclopts.Parameter(name="--window-time")] = None,
        window_blocks: Annotated[int | None, cyclopts.Parameter(name="--window-blocks")] = None,
        rpc_url: Annotated[
            str | None,
            _RPC_URL_PARAM,
        ] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        exit_code = asyncio.run(
            dispatch_numeric(
                chain=profile.alias,
                resource="network",
                path_params={},
                sub_resource=command_name,
                entry_path=entry_path,
                drop_pct=drop_pct,
                rise_pct=rise_pct,
                move_pct=move_pct,
                window_time=window_time,
                window_blocks=window_blocks,
                window_epochs=None,
                below=below,
                above=above,
                rpc_url=_resolve_rpc(profile, rpc_url),
                max_runtime_seconds=parse_max_runtime(max_runtime),
                poll_seconds=None,
                invocation=_invocation(),
                out_uris=out or [],
                name=name,
                max_ru=_resolve_max_ru(profile, max_ru),
                api_key=resolve_api_key(api_key, profile.alias),
            )
        )
        raise SystemExit(exit_code)

    command.__name__ = command_name.replace("-", "_")
    command.__doc__ = summary
    return command


def _build_network_app(profile: EvmChainProfile) -> cyclopts.App:
    app = cyclopts.App(
        name="network",
        help=f"Watch chain-wide {profile.name} network values.",
    )
    if profile.fee_model in {EvmFeeModel.EIP1559, EvmFeeModel.OP_STACK}:
        app.command(
            _fee_command(
                profile,
                command_name="base-fee",
                entry_path="network.base-fee",
                summary=f"Wake when {profile.name}'s EIP-1559 execution base fee matches.",
            ),
            name="base-fee",
        )
    if profile.fee_model == EvmFeeModel.OP_STACK:
        for command_name, summary in (
            (
                "l1-base-fee",
                f"Wake when the Ethereum L1 base fee observed by {profile.name} matches.",
            ),
            (
                "l1-blob-base-fee",
                f"Wake when the Ethereum L1 blob base fee observed by {profile.name} matches.",
            ),
        ):
            app.command(
                _fee_command(
                    profile,
                    command_name=command_name,
                    entry_path=f"network.{command_name}",
                    summary=summary,
                ),
                name=command_name,
            )
    if profile.fee_model == EvmFeeModel.GAS_PRICE:
        app.command(
            _fee_command(
                profile,
                command_name="gas-price",
                entry_path="network.gas-price",
                summary=f"Wake when {profile.name}'s suggested gas price matches.",
            ),
            name="gas-price",
        )
    return app


def _build_token_app(profile: EvmChainProfile) -> cyclopts.App:
    """Build the id-first ``token <symbol-or-address> price`` surface."""

    app = cyclopts.App(
        name="token",
        help=f"Watch aggregate USD prices for tokens on {profile.name}.",
    )

    def price_command(
        token: Annotated[
            str,
            cyclopts.Parameter(help="Token symbol or 20-byte contract address."),
        ],
        *,
        below: Annotated[
            float | None,
            cyclopts.Parameter(name="--below", help="Wake below this USD price."),
        ] = None,
        above: Annotated[
            float | None,
            cyclopts.Parameter(name="--above", help="Wake above this USD price."),
        ] = None,
        drop_pct: Annotated[float | None, cyclopts.Parameter(name="--drop-pct")] = None,
        rise_pct: Annotated[float | None, cyclopts.Parameter(name="--rise-pct")] = None,
        move_pct: Annotated[float | None, cyclopts.Parameter(name="--move-pct")] = None,
        window_time: Annotated[str | None, cyclopts.Parameter(name="--window-time")] = None,
        rpc_url: Annotated[str | None, _RPC_URL_PARAM] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        """Wake when a token's aggregate USD price matches."""
        identifier = token.strip()
        if not identifier or "." in identifier:
            user_error("token must be a symbol or 20-byte contract address")
        if identifier.startswith(("0x", "0X")):
            if len(identifier) != _EVM_ADDRESS_LENGTH:
                user_error("token contract address must contain 20 bytes")
            try:
                int(identifier[2:], 16)
            except ValueError:
                user_error("token contract address must be hexadecimal")
            identifier = identifier.lower()
        else:
            identifier = identifier.upper()

        exit_code = asyncio.run(
            dispatch_numeric(
                chain=profile.alias,
                resource="token",
                path_params={"token": identifier},
                sub_resource="price",
                entry_path="token.{token}.price",
                drop_pct=drop_pct,
                rise_pct=rise_pct,
                move_pct=move_pct,
                window_time=window_time,
                window_blocks=None,
                window_epochs=None,
                below=below,
                above=above,
                rpc_url=_resolve_rpc(profile, rpc_url),
                max_runtime_seconds=parse_max_runtime(max_runtime),
                poll_seconds=None,
                invocation=_invocation(),
                out_uris=out or [],
                name=name,
                max_ru=_resolve_max_ru(profile, max_ru),
                api_key=resolve_api_key(api_key, profile.alias),
            )
        )
        raise SystemExit(exit_code)

    app.command(price_command, name="price")
    app[
        "price"
    ].usage = f"Usage: chainwake {profile.alias} token <symbol-or-address> price [OPTIONS]\n"
    app.usage = f"Usage: chainwake {profile.alias} token <symbol-or-address> COMMAND [OPTIONS]\n"

    meta = app.meta
    meta.help_flags = []

    @meta.default
    def dispatch_id_first(
        *tokens: Annotated[str, cyclopts.Parameter(allow_leading_hyphen=True)],
    ) -> object:
        if not tokens or (len(tokens) == 1 and not tokens[0].startswith("-")):
            return app([])
        if len(tokens) >= _ID_FIRST_MIN_TOKENS and tokens[1] == "price":
            rest = list(tokens[2:])
            if not rest or any(token in {"--help", "-h"} for token in rest):
                app["price"].help_print()
                return None
            return app(["price", tokens[0], *rest])
        if tokens[0] != "price":
            return app(list(tokens))
        raise cyclopts.UnknownCommandError(
            unused_tokens=[tokens[0]],
            root_input_tokens=list(tokens),
            app=meta,
        )

    return meta


def build_evm_app(profile: EvmChainProfile) -> cyclopts.App:
    """Return one EVM command tree generated from a chain profile."""

    app = cyclopts.App(name=profile.alias, help=f"{profile.name} monitoring.")
    app.command(_build_network_app(profile))
    app.command(_build_token_app(profile), name="token")

    def tx_command(
        tx_hash: Annotated[str, cyclopts.Parameter(help="Transaction hash (0x...).")],
        *,
        finality: str = "included",
        confirmations: Annotated[
            int | None,
            cyclopts.Parameter(
                name="--confirmations",
                help="Canonical confirmations required (default: 1 with included).",
            ),
        ] = None,
        rpc_url: Annotated[
            str | None,
            _RPC_URL_PARAM,
        ] = None,
        out: Annotated[list[str] | None, _OUT_PARAM] = None,
        name: Annotated[str | None, cyclopts.Parameter(name="--name")] = None,
        api_key: Annotated[str | None, _API_KEY_PARAM] = None,
        max_runtime: Annotated[str | None, cyclopts.Parameter(name="--max-runtime")] = None,
        max_ru: Annotated[int | None, _MAX_RU_PARAM] = None,
    ) -> None:
        """Wake when a transaction reaches the requested confidence."""
        if finality not in profile.supported_finality_levels:
            supported = ", ".join(profile.supported_finality_levels)
            user_error(f"{profile.name} supports finality levels: {supported}")
        if confirmations is not None and confirmations <= 0:
            user_error("--confirmations must be greater than zero")
        if finality != "included" and confirmations is not None:
            user_error(f"--confirmations cannot be combined with --finality {finality}")
        try:
            validated_tx_hash = validate_tx_hash(tx_hash)
        except ValueError as exc:
            user_error(str(exc))
        effective_confirmations = (
            (confirmations if confirmations is not None else 1) if finality == "included" else None
        )
        exit_code = asyncio.run(
            dispatch_tx(
                chain=profile.alias,
                tx_hash=validated_tx_hash,
                finality=finality,
                confirmations=effective_confirmations,
                entry_path="tx.{tx_hash}",
                rpc_url=_resolve_rpc(profile, rpc_url),
                max_runtime_seconds=parse_max_runtime(max_runtime),
                invocation=_invocation(),
                out_uris=out or [],
                name=name,
                max_ru=_resolve_max_ru(profile, max_ru),
                api_key=resolve_api_key(api_key, profile.alias),
            )
        )
        raise SystemExit(exit_code)

    tx_command.__annotations__["finality"] = Annotated[
        str,
        cyclopts.Parameter(
            name="--finality",
            help=f"Wait for one of {', '.join(profile.supported_finality_levels)}.",
        ),
    ]
    app.command(tx_command, name="tx")
    return app


__all__ = ["build_evm_app"]
