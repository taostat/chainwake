"""Bittensor chain provider.

Implements the complete `ChainProvider` Protocol for Bittensor.  Uses
`async-substrate-interface` directly.  Never imports
`bittensor.core.async_subtensor` (CI-enforced; see CLAUDE.md).

Supported:
- All seven Appendix-A resources: subnet, validator, neuron, account,
  network, event, tx.
- Two computed observables: depth-for-trade and blocks-until-immunity-expires.
- Eleven friendly event types verified against Subtensor metadata.
- subscribe_heads via chainHead_v1_follow with direct best-block hashes and
  chain_subscribeNewHeads fallback.
- subscribe_events from the same direct best-head BlockRef stream.
- subscribe_storage via raw state_subscribeStorage notifications so each
  update keeps its originating block hash.
- get_block_finality for tx-finality waits.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import blake2b
from typing import Any, Final, NoReturn, cast

from async_substrate_interface import AsyncSubstrateInterface
from async_substrate_interface.errors import SubstrateRequestException
from async_substrate_interface.utils.storage import StorageKey

from chainwake.core.errors import (
    BudgetExhaustedError,
    DecodeError,
    ProviderError,
    RateLimitError,
    RPCUnreachableError,
    SubscriptionFailedError,
    TxNotFoundInHorizonError,
    UserError,
)
from chainwake.core.registry import FRIENDLY_EVENT_MAP, lookup_rendered
from chainwake.core.retry import RateLimitGuard
from chainwake.core.ss58 import BITTENSOR_SS58_FORMAT, validate_bittensor_ss58
from chainwake.core.tx_hash import validate_tx_hash
from chainwake.providers.base import (
    BlockRef,
    Cadence,
    EpochState,
    Event,
    EventFilter,
    ObservableValue,
    ProviderConfig,
    StorageUpdate,
    TxFinalityStatus,
)
from chainwake.providers.market import MarketPriceFeed, NativeAsset

DEFAULT_RPC_URL: Final[str] = "wss://rpc.blockmachine.io"
SS58_FORMAT: Final[int] = BITTENSOR_SS58_FORMAT
RAO_PER_TAO: Final[int] = 1_000_000_000
RAO_PER_ALPHA: Final[int] = 1_000_000_000
# Commission is stored as a u16 take-rate (0-65535 where 65535 == 100%)
TAKE_NORMALISER: Final[int] = 65_535
# Neuron values (incentive, dividends) are stored as u16 fractions
U16_NORMALISER: Final[int] = 65_535
# MinerBurned is stored as a U96F32 fixed-point fraction.
U96F32_FRACTIONAL_BITS: Final[int] = 32
# SS58 addresses start with "5" and are 48 chars on Substrate; 47 is the min
SS58_MIN_LEN: Final[int] = 47
# Tx hashes are 0x-prefixed 32-byte hex strings (66 chars)
TX_HASH_MIN_LEN: Final[int] = 32
# How far back to search when resolving tx finality (blocks).
# 7200 blocks ≈ 24h on FAST_BLOCKS (~12s effective) or mainnet (12s/block).
# The bounded historical scan runs only once per transaction hash. Subsequent
# polls inspect newly produced blocks, so a wake can remain pending without
# repeatedly paying the bootstrap cost.
_TX_SEARCH_HORIZON_BLOCKS: Final[int] = 7200
_TAO_MARKET_ASSET: Final[NativeAsset] = NativeAsset(
    coin_id="bittensor",
    name="Bittensor",
    symbol="TAO",
)
# Index of hotkey segment in a neuron path (neuron.{netuid}.{hotkey}.*)
NEURON_HOTKEY_INDEX: Final[int] = 2
# Subtensor spec 440 reserves 4096 storage indexes per mechanism. Mechanism
# zero therefore retains the legacy ``storage_index == netuid`` layout.
_MECHANISM_STORAGE_STRIDE: Final[int] = 4_096
_MAX_MECHANISM_ID: Final[int] = 15
_MAX_NETUID: Final[int] = 65_535
# Root network. Present in NetworksAdded on every Subtensor chain from
# genesis, which makes it a canary: a True read for root proves the
# endpoint is decoding storage honestly right now.
_ROOT_NETUID: Final[int] = 0
_CHAINHEAD_UNPIN_BATCH: Final[int] = 32
_STORAGE_CHANGE_FIELDS: Final[int] = 2

# ---------------------------------------------------------------------------
# Friendly-name to Substrate event mapping (Appendix B)
# ---------------------------------------------------------------------------
#
# Verified against subtensor-localnet by driving each action and capturing the
# actual Module.Event strings emitted.
#
# Verification legend per entry:
#   CONFIRMED  — action driven on localnet; event observed in block
#   IN_METADATA — event name present in runtime metadata; emission unverified
#                 (staking blocked by localnet TxRateLimit / delegate setup)
#   FIXED      — original name was wrong; corrected per metadata + localnet run
#   UNVERIFIED — no clear runtime event; entry removed or kept with note


_FRIENDLY_TO_SUBSTRATE: Final[dict[str, list[str]]] = FRIENDLY_EVENT_MAP

# Current Subtensor tuple events can expose unnamed metadata fields. Curated
# names keep verified friendly events useful; unknown tuples retain every value
# under deterministic ``arg_N`` keys.
_POSITIONAL_EVENT_ARGUMENT_NAMES: Final[dict[str, tuple[str, ...]]] = {
    "SubtensorModule.NetworkAdded": ("netuid",),
    "SubtensorModule.WeightsSet": ("netuid", "uid"),
}

# Type alias for zero-argument async factory used in dispatch tables.
_AsyncFactory = Callable[[], Coroutine[Any, Any, object]]


@dataclass(slots=True)
class _TxScanState:
    """Incremental scan cursor and optional cached inclusion metadata."""

    last_head_num: int
    last_head_hash: str
    scan_next_block: int | None = None
    scan_oldest_block: int | None = None
    scan_head_num: int | None = None
    scan_head_hash: str | None = None
    match_block_num: int | None = None
    match_block_hash: str | None = None
    included: TxFinalityStatus | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Substrate text patterns we map to provider-error reasons. The library
# funnels every RPC failure through `SubstrateRequestException` with no
# structured detail beyond the message, so text-matching is what we have.
# Rate-limit hits (-32029 in spec) appear as plain "rate limit exceeded".
_RATE_LIMIT_MARKERS: Final[tuple[str, ...]] = (
    "rate limit",
    "too many requests",
    "429",
    "no free-tier-eligible backend",
)
_SUBSCRIPTION_MARKERS: Final[tuple[str, ...]] = ("subscribe", "subscription")


def _wrap_substrate_exception(exc: SubstrateRequestException) -> ProviderError:
    """Convert a substrate-library exception into the matching ProviderError.

    Keeps classification at the provider boundary so the runtime's
    `_handle_provider_exception` only needs to know about ProviderError —
    not every substrate library exception class. Substrate RPC failures
    are upstream by definition, never internal.
    """
    msg = str(exc)
    msg_lc = msg.lower()
    if any(marker in msg_lc for marker in _RATE_LIMIT_MARKERS):
        return RateLimitError(f"SubstrateRequestException: {msg}")
    if any(marker in msg_lc for marker in _SUBSCRIPTION_MARKERS):
        return SubscriptionFailedError(f"SubstrateRequestException: {msg}")
    return RPCUnreachableError(f"SubstrateRequestException: {msg}")


def _scale_to_int(value: object) -> int:
    """Coerce a substrate query result (often a `ScaleType`) to int."""
    if value is None:
        return 0
    underlying = getattr(value, "value", value)
    if underlying is None or isinstance(underlying, dict):
        return 0
    return int(str(underlying))


def _decode_identity_bytes(raw: bytes) -> str:
    """Decode text when possible and preserve arbitrary bytes as canonical hex."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"0x{raw.hex()}"


def _decode_identity_value(value: object) -> object:
    """Decode IdentitiesV2 values without assuming bytes are valid UTF-8.

    SCALE decoders commonly expose a ``Data::Raw`` variant as a one-key
    mapping. Recursing also tolerates an extra wrapper introduced by a
    metadata/decoder version without discarding container structure. Valid
    UTF-8 is returned as text; arbitrary bytes keep a canonical ``0x`` hex
    representation so state comparisons remain lossless and deterministic.
    """

    if isinstance(value, dict):
        first_key = next(iter(value), None)
        if len(value) == 1 and isinstance(first_key, str) and first_key.lower() == "raw":
            return _decode_identity_value(next(iter(value.values())))
        return {str(key): _decode_identity_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_identity_value(item) for item in value]

    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str) and value.startswith("0x"):
        try:
            raw = bytes.fromhex(value.removeprefix("0x"))
        except ValueError:
            return value
    else:
        return value
    return _decode_identity_bytes(raw)


def _rao_to_tao(rao: int) -> float:
    return rao / RAO_PER_TAO


def _rao_to_alpha(rao: int) -> float:
    """Convert an AlphaBalance base-unit value to subnet alpha tokens."""
    return rao / RAO_PER_ALPHA


def _path_template(path: str) -> str:
    """Convert a concrete path like 'subnet.19.pool.price' to its template.

    Replaces numeric segments and SS58/tx-hash-like segments with
    {placeholders} so they match the _PATH_CADENCE keys.

    Resource semantics:
      - account.*  : SS58 segment is a coldkey ({coldkey})
      - validator.* / neuron.*: SS58 segment is a hotkey ({hotkey})
    """
    parts = path.split(".")
    resource = parts[0] if parts else ""
    if (
        resource in {"validator", "neuron"}
        and len(parts) > NEURON_HOTKEY_INDEX + 1
        and parts[1].isdigit()
    ):
        return ".".join([parts[0], "{netuid}", "{hotkey}", *parts[3:]])
    is_account = resource == "account"
    out: list[str] = []
    netuid_used = False
    hotkey_used = False
    tx_used = False
    for part in parts:
        if part.isdigit() and not netuid_used:
            out.append("{netuid}")
            netuid_used = True
        elif part.startswith("5") and len(part) >= SS58_MIN_LEN and not hotkey_used:
            out.append("{coldkey}" if is_account else "{hotkey}")
            hotkey_used = True
        elif part.startswith("5") and len(part) >= SS58_MIN_LEN:
            out.append("{coldkey}")
        elif part.startswith("0x") and len(part) >= TX_HASH_MIN_LEN and not tx_used:
            out.append("{tx_hash}")
            tx_used = True
        else:
            out.append(part)
    return ".".join(out)


def _require_bittensor_ss58(value: object, label: str) -> str:
    """Raise a typed provider-boundary error for a malformed address."""
    try:
        if not isinstance(value, str):
            raise ValueError
        return validate_bittensor_ss58(value)
    except ValueError as exc:
        raise UserError(
            f"{label}: expected a canonical Bittensor SS58 address "
            "(format 42, 32-byte account id, valid checksum)",
            reason="invalid_path_params",
        ) from exc


def _validate_observable_path_addresses(path: str) -> None:
    """Validate address-bearing concrete paths before resolving a chain head."""
    parts = path.split(".")
    resource = parts[0] if parts else ""
    if resource == "account" and len(parts) > 1:
        _require_bittensor_ss58(parts[1], "coldkey")
    elif resource == "validator" and len(parts) > 1:
        hotkey_index = 2 if parts[1].isdigit() else 1
        if len(parts) > hotkey_index:
            _require_bittensor_ss58(parts[hotkey_index], "hotkey")
    elif resource == "neuron" and len(parts) > NEURON_HOTKEY_INDEX:
        _require_bittensor_ss58(parts[NEURON_HOTKEY_INDEX], "hotkey")


def _validate_event_filter_addresses(event_filter: EventFilter) -> None:
    """Validate every Bittensor-address event predicate before subscribing."""
    for key in ("from", "to", "address"):
        if key in event_filter.args_match:
            _require_bittensor_ss58(event_filter.args_match[key], f"event {key!r} filter")
    if event_filter.direction_address is not None:
        _require_bittensor_ss58(event_filter.direction_address, "event direction address")


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class BittensorProvider:
    """Full Bittensor `ChainProvider` implementation.

    Supported observables: all entries from spec Appendix A.
    RPC via `async-substrate-interface` only.
    """

    name: str = "bittensor"
    short_alias: str = "bt"

    def __init__(self, *, market_prices: MarketPriceFeed | None = None) -> None:
        self._substrate: AsyncSubstrateInterface | None = None
        self._market_prices = market_prices or MarketPriceFeed()
        # One observation can validate a neuron and then read a vector keyed by
        # the same uid. Reuse that pinned-block lookup so honesty checks do not
        # silently add a duplicate RPC.
        self._uid_cache: dict[tuple[str, int, str], int | None] = {}
        self._tx_scan_state: dict[str, _TxScanState] = {}

    @property
    def _connected(self) -> AsyncSubstrateInterface:
        if self._substrate is None:
            raise RuntimeError("BittensorProvider.connect() not called")
        return self._substrate

    async def connect(self, config: ProviderConfig) -> None:
        """Open the websocket connection.

        If ``config.api_key`` is set, it is forwarded as an
        ``Authorization: Bearer <key>`` header on the websocket handshake.
        ``async-substrate-interface`` does not expose its own auth surface,
        so we set the header on the underlying ``Websocket._options`` dict
        which is passed through to ``websockets.asyncio.client.connect``.
        """
        if not config.rpc_url:
            raise ValueError("rpc_url required")
        self._substrate = AsyncSubstrateInterface(config.rpc_url, ss58_format=SS58_FORMAT)
        if config.api_key is not None:
            # _options is forwarded as **kwargs to websockets.connect; the
            # `additional_headers` kwarg is stable as of websockets 13+.
            self._substrate.ws._options["additional_headers"] = {
                "Authorization": f"Bearer {config.api_key}",
            }
        try:
            await self._substrate.initialize()
        except SubstrateRequestException as exc:
            raise _wrap_substrate_exception(exc) from exc

    async def disconnect(self) -> None:
        if self._substrate is not None:
            await self._substrate.close()
            self._substrate = None
        await self._market_prices.close()
        self._tx_scan_state.clear()

    def epoch_netuid_for(
        self,
        observable_path: str,
        args: dict[str, object] | None = None,
    ) -> int | None:
        """Resolve the subnet whose stateful epoch governs a path.

        Scoped validator paths carry their netuid in the path. Validator
        weight liveness keeps its stable public path and carries the selected
        subnet in read arguments. Network/account and unscoped validator state
        paths have no single epoch.
        """
        parts = observable_path.split(".")
        if len(parts) > 1 and parts[0] in {"subnet", "neuron", "validator"} and parts[1].isdigit():
            return int(parts[1])
        if parts and parts[0] == "validator" and parts[-1:] == ["weights"]:
            return self._read_netuid_arg(args or {}, default=1)
        return None

    async def get_epoch_state(
        self,
        netuid: int,
        at_block: BlockRef | None = None,
    ) -> EpochState:
        """Read the chain-owned epoch schedule for ``netuid`` at one block."""
        try:
            block_number, block_hash = await self._resolve_block(at_block)
            substrate = self._connected
            tempo = _scale_to_int(
                await substrate.query("SubtensorModule", "Tempo", [netuid], block_hash=block_hash)
            )
            last_epoch_block = _scale_to_int(
                await substrate.query(
                    "SubtensorModule", "LastEpochBlock", [netuid], block_hash=block_hash
                )
            )
            epoch_index = _scale_to_int(
                await substrate.query(
                    "SubtensorModule", "SubnetEpochIndex", [netuid], block_hash=block_hash
                )
            )
            next_start_raw = await substrate.runtime_call(
                "SubnetInfoRuntimeApi",
                "get_next_epoch_start_block",
                [netuid],
                block_hash=block_hash,
            )
        except SubstrateRequestException as exc:
            raise _wrap_substrate_exception(exc) from exc
        next_start = None if next_start_raw is None else _scale_to_int(next_start_raw)
        return EpochState(
            netuid=netuid,
            block=block_number,
            block_hash=block_hash,
            tempo=tempo,
            epoch_index=epoch_index,
            last_epoch_block=last_epoch_block,
            next_epoch_start_block=next_start,
        )

    # ------------------------------------------------------------------
    # Block resolution
    # ------------------------------------------------------------------

    async def _resolve_block(self, at_block: BlockRef | None) -> tuple[int, str]:
        """Pin all reads in a tick to a single block hash."""
        substrate = self._connected
        if at_block is not None and at_block.hash is not None:
            block_hash = at_block.hash
            block_number = (
                at_block.number
                if at_block.number is not None
                else await substrate.get_block_number(block_hash)
            )
        else:
            block_hash = await substrate.get_chain_head()
            block_number = await substrate.get_block_number(block_hash)
        if block_number is None:
            raise RuntimeError("could not resolve block number")
        return block_number, block_hash

    async def _block_timestamp(self, block_hash: str) -> datetime:
        """Read the authoritative chain timestamp for ``block_hash``.

        Queries ``Timestamp.Now`` on the pinned block.  Exceptions propagate
        — silently falling back to wallclock would emit payloads that look
        like chain timestamps but aren't, breaking the spec §7.1 contract.
        Callers run within ``read_observable`` so the runtime maps provider
        exceptions onto a ``provider_error`` payload.

        Raises:
            DecodeError: when ``Timestamp.Now`` returns ``None`` on a block
                that should always carry a timestamp (genesis edge case).
            Any substrate-level exception (network, decode, RPC) is allowed
                to propagate; the runtime classifies it.
        """
        substrate = self._connected
        ts = await substrate.query("Timestamp", "Now", block_hash=block_hash)
        if ts is None:
            raise DecodeError(
                f"Timestamp.Now returned None for block {block_hash}; "
                "chain block lacks an authoritative timestamp"
            )
        return datetime.fromtimestamp(_scale_to_int(ts) / 1000.0, tz=UTC)

    async def _query_multi_int(
        self,
        queries: list[tuple[str, str, list[object]]],
        block_hash: str,
    ) -> list[int]:
        """Batch a list of (module, storage_fn, params) reads into one RPC.

        Returns int-coerced values in the same order as input. A failed
        decode yields 0; the caller decides whether 0 is meaningful.
        Reduces N round-trips to 1 — significant on the per-tick hot path.
        """
        substrate = self._connected
        storage_keys = [
            await substrate.create_storage_key(module, storage_fn, params)
            for module, storage_fn, params in queries
        ]
        pairs = await substrate.query_multi(storage_keys, block_hash=block_hash)
        # query_multi returns (StorageKey, scale_obj) pairs in input order.
        return [_scale_to_int(value) for _, value in pairs]

    async def _read_subnet_dynamic_info(self, netuid: int, block_hash: str) -> dict[str, object]:
        """Fetch the full dynamic-info struct for a subnet in one runtime call.

        Issues a single ``state_call`` to
        ``SubnetInfoRuntimeApi.get_dynamic_info``.  All pool-based observables
        (price, tao-depth, alpha-depth, alpha-supply, moving-price, volume,
        depth-for-trade) extract their field from the returned dict, keeping
        the per-tick cost to one RPC regardless of how many fields are read.

        Returns an empty dict when the subnet does not exist (runtime call
        returns a non-dict).
        """
        substrate = self._connected
        info = await substrate.runtime_call(
            "SubnetInfoRuntimeApi",
            "get_dynamic_info",
            [netuid],
            block_hash=block_hash,
        )
        if not isinstance(info, dict):
            return {}
        return info

    # ------------------------------------------------------------------
    # read_observable — route to per-resource handlers
    # ------------------------------------------------------------------

    async def read_observable(
        self,
        path: str,
        args: dict[str, object],
        at_block: BlockRef | None = None,
    ) -> ObservableValue:
        """Route to the appropriate handler based on path template.

        Converts ``SubstrateRequestException`` (the library's catch-all RPC
        error) into the appropriate ``ProviderError`` subclass at the
        boundary so the runtime's exception ladder routes it as an
        upstream failure rather than dropping through to ``internal_error``.
        """
        _validate_observable_path_addresses(path)
        if _path_template(path) == "tx.{tx_hash}":
            return await self._read_tx_observable(path)
        if path == "network.tao-price":
            return await self._read_tao_price(at_block)
        try:
            self._uid_cache.clear()
            block_number, block_hash = await self._resolve_block(at_block)
            timestamp = await self._block_timestamp(block_hash)
            value = await self._dispatch(path, args, block_number, block_hash)
            meta = await self._activity_context(path, args, value)
        except SubstrateRequestException as exc:
            raise _wrap_substrate_exception(exc) from exc
        return ObservableValue(
            path=path,
            value=value,
            block=block_number,
            block_hash=block_hash,
            timestamp=timestamp,
            meta=meta,
        )

    async def _read_tao_price(
        self,
        at_block: BlockRef | None,
    ) -> ObservableValue:
        """Read CoinGecko's TAO/USD aggregate and attach current chain context."""

        market = await self._market_prices.native_usd(_TAO_MARKET_ASSET)
        block_number, block_hash = await self._resolve_block(at_block)
        timestamp = await self._block_timestamp(block_hash)
        return ObservableValue(
            path="network.tao-price",
            value=market.value,
            block=block_number,
            block_hash=block_hash,
            timestamp=timestamp,
            meta=market.meta,
        )

    async def _read_tx_observable(self, path: str) -> ObservableValue:
        """Read tx finality without redundantly pinning the head first.

        ``get_block_finality`` already resolves and persists the authoritative
        head.  Repeating ``_resolve_block`` before that scan wastes two RPCs
        per tick and can attach a different head to the returned tx status.
        Included statuses already carry their inclusion context; pending
        statuses use the scan cursor's completed head and read its timestamp.
        """
        tx_hash = path.split(".", maxsplit=1)[1] if "." in path else ""
        try:
            status = await self.get_block_finality(tx_hash)
            state = self._tx_scan_state.get(tx_hash.lower())
            if (
                status.block is not None
                and status.block_hash is not None
                and status.timestamp is not None
            ):
                block_number = status.block
                block_hash = status.block_hash
                timestamp = status.timestamp
            elif state is not None:
                block_number = state.last_head_num
                block_hash = state.last_head_hash
                timestamp = await self._block_timestamp(block_hash)
            else:
                # Defensive fallback for a provider that returned no block
                # number for the head while resolving finality.
                block_number, block_hash = await self._resolve_block(None)
                timestamp = await self._block_timestamp(block_hash)
        except SubstrateRequestException as exc:
            raise _wrap_substrate_exception(exc) from exc
        return ObservableValue(
            path=path,
            value=status,
            block=block_number,
            block_hash=block_hash,
            timestamp=timestamp,
        )

    async def _activity_context(
        self,
        path: str,
        args: dict[str, object],
        value: object,
    ) -> dict[str, object]:
        """Resolve an absolute liveness marker to its historical chain context.

        ``LastUpdate`` and ``LastTxBlock`` store a block number.  Using the
        current observation timestamp/epoch for that older marker makes a
        watcher that was already stale start a fresh silence window.  For the
        three absolute-marker observables, pin the marker's authoritative
        timestamp and (where subnet-scoped) epoch index into observation
        metadata.  The liveness primitive can then evaluate truthfully on its
        first read.

        A zero marker means no on-chain activity has ever been recorded.  It
        has no historical block context and intentionally keeps the existing
        watcher-arrival semantics.
        """
        template = _path_template(path)
        if template not in {
            "validator.{hotkey}.weights",
            "neuron.{netuid}.{hotkey}.last-update",
            "account.{coldkey}.activity",
        }:
            return {}
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            return {}

        unit = args.get("_liveness_unit")
        if unit == "blocks":
            # The marker itself is sufficient.  Avoid historical state reads;
            # the liveness payload reports a null timestamp rather than
            # fabricating watcher-arrival time for the older block.
            return {}

        substrate = self._connected
        activity_hash = await substrate.get_block_hash(value)
        if activity_hash is None:
            raise DecodeError(
                f"could not resolve activity marker block {value} for {path!r}; "
                "use a block-based liveness window or a historical RPC endpoint"
            )
        activity_timestamp = await self._block_timestamp(activity_hash)
        meta: dict[str, object] = {
            "activity_block_hash": activity_hash,
            "activity_timestamp": activity_timestamp,
        }

        netuid = self.epoch_netuid_for(path, args)
        if netuid is not None and unit != "time":
            epoch_index = await substrate.query(
                "SubtensorModule",
                "SubnetEpochIndex",
                [netuid],
                block_hash=activity_hash,
            )
            meta["activity_epoch_index"] = _scale_to_int(epoch_index)
        return meta

    async def _dispatch(
        self,
        path: str,
        args: dict[str, object],
        block_number: int,
        block_hash: str,
    ) -> object:
        """Dispatch a path to the correct resource handler."""
        template = _path_template(path)
        parts = path.split(".")

        resource = parts[0] if parts else ""
        if resource == "subnet":
            return await self._dispatch_subnet(template, parts, args, block_number, block_hash)
        if resource == "validator":
            return await self._dispatch_validator(template, parts, args, block_hash)
        if resource == "neuron":
            return await self._dispatch_neuron(template, parts, args, block_number, block_hash)
        if resource == "account":
            return await self._dispatch_account(template, parts, block_hash)
        if resource == "network":
            return await self._dispatch_network(template, block_hash)
        if resource == "tx" and template == "tx.{tx_hash}":
            # Tx primitives consume an ObservableValue whose .value is a
            # TxFinalityStatus. Bridge through get_block_finality so the
            # primitive sees TxFinalityStatus on every poll.
            tx_hash = parts[1] if len(parts) > 1 else ""
            return await self.get_block_finality(tx_hash)
        raise NotImplementedError(
            f"BittensorProvider: no handler for path {path!r} (template={template!r})"
        )

    async def _dispatch_subnet(
        self,
        template: str,
        parts: list[str],
        args: dict[str, object],
        block_number: int,  # noqa: ARG002
        block_hash: str,
    ) -> object:
        netuid = self._path_netuid(parts)
        trade_size = (
            self._read_positive_float_arg(args, "size")
            if template == "subnet.{netuid}.pool.depth-for-trade"
            else 1.0
        )
        trade_max_bps = (
            self._read_positive_float_arg(args, "max_bps")
            if template == "subnet.{netuid}.pool.depth-for-trade"
            else 1.0
        )
        handlers: dict[str, _AsyncFactory] = {
            "subnet.{netuid}.pool.price": lambda: self._read_subnet_price(netuid, block_hash),
            "subnet.{netuid}.pool.tao-depth": lambda: self._read_subnet_tao_depth(
                netuid, block_hash
            ),
            "subnet.{netuid}.pool.alpha-depth": lambda: self._read_subnet_alpha_depth(
                netuid, block_hash
            ),
            "subnet.{netuid}.pool.depth-for-trade": lambda: self._read_depth_for_trade(
                netuid,
                trade_size,
                trade_max_bps,
                block_hash,
            ),
            "subnet.{netuid}.pool.alpha-supply": lambda: self._read_subnet_alpha_supply(
                netuid, block_hash
            ),
            "subnet.{netuid}.pool.moving-price": lambda: self._read_subnet_moving_price(
                netuid, block_hash
            ),
            "subnet.{netuid}.pool.volume": lambda: self._read_subnet_volume(netuid, block_hash),
            "subnet.{netuid}.registration-cost": lambda: self._read_subnet_registration_cost(
                netuid, block_hash
            ),
            "subnet.{netuid}.emission-share": lambda: self._read_subnet_emission_share(
                netuid, block_hash
            ),
            "subnet.{netuid}.burn-rate": lambda: self._read_subnet_burn_rate(netuid, block_hash),
            "subnet.{netuid}.ema-tao-flow": lambda: self._read_subnet_ema_tao_flow(
                netuid, block_hash
            ),
            "subnet.{netuid}.hyperparams": lambda: self._read_subnet_hyperparams(
                netuid, block_hash
            ),
            "subnet.{netuid}.identity": lambda: self._read_subnet_identity(netuid, block_hash),
        }
        coro_factory = handlers.get(template)
        if coro_factory is None:
            raise NotImplementedError(f"BittensorProvider: no subnet handler for {template!r}")
        await self._require_subnet(netuid, block_hash)
        return await coro_factory()

    async def _dispatch_validator(
        self,
        template: str,
        parts: list[str],
        args: dict[str, object],
        block_hash: str,
    ) -> object:
        scoped = template.startswith("validator.{netuid}.{hotkey}.")
        netuid = self._path_netuid(parts) if scoped else 0
        hotkey_index = 2 if scoped else 1
        hotkey = parts[hotkey_index] if len(parts) > hotkey_index else ""
        mechid = self._read_mechid_arg(args)
        weights_netuid = self._read_netuid_arg(args, default=1)
        handlers: dict[str, _AsyncFactory] = {
            "validator.{netuid}.{hotkey}.dividends-alpha": (
                lambda: self._read_validator_dividends(netuid, hotkey, block_hash)
            ),
            "validator.{netuid}.{hotkey}.stake-alpha": (
                lambda: self._read_validator_stake(netuid, hotkey, block_hash)
            ),
            "validator.{hotkey}.commission": lambda: self._read_validator_commission(
                hotkey, block_hash
            ),
            "validator.{hotkey}.weights": lambda: self._read_validator_last_weight_block(
                hotkey,
                block_hash,
                netuid=weights_netuid,
                mechid=mechid,
            ),
            "validator.{hotkey}.child-keys": lambda: self._read_validator_child_keys(
                hotkey, block_hash
            ),
            "validator.{hotkey}.identity": lambda: self._read_validator_identity(
                hotkey, block_hash
            ),
        }
        coro_factory = handlers.get(template)
        if coro_factory is None:
            raise NotImplementedError(f"BittensorProvider: no validator handler for {template!r}")
        if template == "validator.{netuid}.{hotkey}.dividends-alpha":
            await self._require_neuron(netuid, hotkey, block_hash)
        elif template == "validator.{hotkey}.weights":
            await self._require_neuron(weights_netuid, hotkey, block_hash)
        return await coro_factory()

    async def _dispatch_neuron(
        self,
        template: str,
        parts: list[str],
        args: dict[str, object],
        block_number: int,
        block_hash: str,
    ) -> object:
        netuid = self._path_netuid(parts)
        hotkey = parts[NEURON_HOTKEY_INDEX] if len(parts) > NEURON_HOTKEY_INDEX else ""
        mechid = self._read_mechid_arg(args)
        handlers: dict[str, _AsyncFactory] = {
            "neuron.{netuid}.{hotkey}.incentive": lambda: self._read_neuron_incentive(
                netuid, hotkey, block_hash, mechid=mechid
            ),
            "neuron.{netuid}.{hotkey}.dividends": lambda: self._read_neuron_dividends(
                netuid, hotkey, block_hash
            ),
            "neuron.{netuid}.{hotkey}.stake-alpha": lambda: self._read_neuron_stake(
                netuid, hotkey, block_hash
            ),
            "neuron.{netuid}.{hotkey}.last-update": lambda: self._read_neuron_last_update(
                netuid, hotkey, block_hash, mechid=mechid
            ),
            "neuron.{netuid}.{hotkey}.blocks-until-immunity-expires": (
                lambda: self._read_blocks_until_immunity(netuid, hotkey, block_number, block_hash)
            ),
        }
        coro_factory = handlers.get(template)
        if coro_factory is None:
            raise NotImplementedError(f"BittensorProvider: no neuron handler for {template!r}")
        await self._require_neuron(netuid, hotkey, block_hash)
        return await coro_factory()

    async def _dispatch_account(self, template: str, parts: list[str], block_hash: str) -> object:
        coldkey = parts[1] if len(parts) > 1 else ""
        handlers: dict[str, _AsyncFactory] = {
            "account.{coldkey}.balance": lambda: self._read_account_balance(coldkey, block_hash),
            "account.{coldkey}.activity": lambda: self._read_account_activity(coldkey, block_hash),
        }
        coro_factory = handlers.get(template)
        if coro_factory is None:
            raise NotImplementedError(f"BittensorProvider: no account handler for {template!r}")
        return await coro_factory()

    async def _dispatch_network(self, template: str, block_hash: str) -> object:
        handlers: dict[str, _AsyncFactory] = {
            "network.subnet-registration-cost": (
                lambda: self._read_network_subnet_registration_cost(block_hash)
            ),
            "network.runtime-version": lambda: self._read_network_runtime_version(block_hash),
            "network.subnet-count": lambda: self._read_network_subnet_count(block_hash),
        }
        coro_factory = handlers.get(template)
        if coro_factory is None:
            raise NotImplementedError(f"BittensorProvider: no network handler for {template!r}")
        return await coro_factory()

    # ------------------------------------------------------------------
    # Subnet handlers
    # ------------------------------------------------------------------

    @staticmethod
    def _dynamic_int(info: dict[str, object], key: str) -> int:
        """Extract an int field from a dynamic-info dict; 0 when missing or not numeric."""
        raw = info.get(key)
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str):
            return int(raw)
        return 0

    @staticmethod
    def _read_mechid_arg(args: dict[str, object]) -> int:
        """Read and validate the optional spec-440 mechanism identifier."""
        raw = args.get("mechid", 0)
        if isinstance(raw, bool):
            raise DecodeError("mechid must be an integer from 0 through 15")
        try:
            mechid = int(str(raw))
        except (TypeError, ValueError) as exc:
            raise DecodeError("mechid must be an integer from 0 through 15") from exc
        if not 0 <= mechid <= _MAX_MECHANISM_ID:
            raise DecodeError(f"mechid must be from 0 through {_MAX_MECHANISM_ID}, got {mechid}")
        return mechid

    @staticmethod
    def _path_netuid(parts: list[str]) -> int:
        """Parse and validate a path netuid without aliasing bad input to root."""
        raw = parts[1] if len(parts) > 1 else ""
        try:
            netuid = int(raw)
        except ValueError as exc:
            raise UserError(
                f"netuid {raw!r} is not an integer from 0 through {_MAX_NETUID}",
                reason="invalid_path_params",
            ) from exc
        if not 0 <= netuid <= _MAX_NETUID:
            raise UserError(
                f"netuid {netuid} is outside the Subtensor u16 range 0..{_MAX_NETUID}",
                reason="invalid_path_params",
            )
        return netuid

    async def _read_networks_added(self, netuid: int, block_hash: str) -> object:
        """Raw ``NetworksAdded`` read: ``True``/``False`` when decoded, ``None`` when dropped."""
        substrate = self._connected
        result = await substrate.query(
            "SubtensorModule",
            "NetworksAdded",
            [netuid],
            block_hash=block_hash,
        )
        return getattr(result, "value", result)

    async def _require_subnet(self, netuid: int, block_hash: str) -> None:
        """Fail honestly when a ValueQuery-backed read targets no subnet.

        Most Subtensor maps return their type's default for a missing key.
        Without checking ``NetworksAdded`` first, a nonexistent subnet is
        indistinguishable from a real subnet whose metric is zero and can
        immediately satisfy a threshold wake.

        A single False/None read is not trustworthy under contention: a
        rate-limited sibling query elsewhere in the same batch can leave
        this query with no completed response (None) or a silently
        substituted decoded default (False) rather than a genuine answer —
        reproduced by firing dozens of concurrent watchers against a real
        anonymous endpoint, where the condition persisted well past a single
        retry. Root (netuid 0) exists on every Subtensor chain, so a canary
        read against it disambiguates: a True canary means the endpoint is
        answering honestly right now, so the non-True target read is
        authoritative and fails immediately. A non-True canary is the real
        contention signal — reconfirm the target with the same bounded
        backoff used for genuine rate-limit errors elsewhere (250ms doubling
        to 32s, 8 attempts, ~64s total) before committing to an answer.
        When the target IS root there is no more-trustworthy key to check
        against, so it goes straight to the backoff loop.

        With --max-runtime set, WatcherRunner._call_with_deadline bounds
        this loop (and the outer transient retries) to the remaining runtime
        budget. Without it there is no deadline: the None-path
        RPCUnreachableError is transient-classified, so the outer
        with_transient_retry re-enters this ~64s loop without limit —
        consistent with the documented retry-indefinitely policy for
        transient errors.
        """
        raw = await self._read_networks_added(netuid, block_hash)
        if raw is True:
            return
        if netuid != _ROOT_NETUID:
            canary = await self._read_networks_added(_ROOT_NETUID, block_hash)
            if canary is True:
                self._raise_subnet_verdict(netuid, block_hash, raw)
        guard = RateLimitGuard()
        while True:
            reason = "no result" if raw is None else "an unconfirmed False"
            should_retry = await guard.handle(
                RateLimitError(
                    f"NetworksAdded read for subnet {netuid} could not be confirmed "
                    f"(returned {reason}); reconfirming before treating it as authoritative"
                )
            )
            if not should_retry:
                break
            raw = await self._read_networks_added(netuid, block_hash)
            if raw is True:
                return
        self._raise_subnet_verdict(netuid, block_hash, raw)

    def _raise_subnet_verdict(self, netuid: int, block_hash: str, raw: object) -> NoReturn:
        """Commit to a trusted non-True ``NetworksAdded`` read.

        ``None`` means the query never completed upstream; ``False`` (or any
        other decoded value) means the subnet is genuinely absent.
        """
        if raw is None:
            raise RPCUnreachableError(
                f"NetworksAdded query for subnet {netuid} returned no result at block {block_hash}"
            )
        raise UserError(
            f"subnet {netuid} does not exist at block {block_hash}",
            reason="invalid_path_params",
        )

    async def _require_neuron(self, netuid: int, hotkey: str, block_hash: str) -> int:
        """Return the registered uid or raise a typed invalid-entity error."""
        await self._require_subnet(netuid, block_hash)
        uid = await self._hotkey_uid(netuid, hotkey, block_hash)
        if uid is None:
            raise UserError(
                f"hotkey {hotkey} is not registered on subnet {netuid} at block {block_hash}",
                reason="invalid_path_params",
            )
        return uid

    @staticmethod
    def _read_netuid_arg(args: dict[str, object], *, default: int) -> int:
        """Read a non-negative netuid from provider arguments."""
        raw = args.get("netuid", default)
        if isinstance(raw, bool):
            raise DecodeError("netuid must be an integer from 0 through 4095")
        try:
            netuid = int(str(raw))
        except (TypeError, ValueError) as exc:
            raise DecodeError("netuid must be an integer from 0 through 4095") from exc
        if not 0 <= netuid < _MECHANISM_STORAGE_STRIDE:
            raise DecodeError(
                f"netuid must be from 0 through {_MECHANISM_STORAGE_STRIDE - 1}, got {netuid}"
            )
        return netuid

    @staticmethod
    def _read_positive_float_arg(args: dict[str, object], name: str) -> float:
        """Read a finite, positive computed-observable argument."""
        raw = args.get(name)
        if isinstance(raw, bool):
            raise UserError(
                f"{name} must be finite and greater than zero",
                reason="invalid_path_params",
            )
        try:
            value = float(str(raw))
        except (TypeError, ValueError) as exc:
            raise UserError(
                f"{name} must be finite and greater than zero",
                reason="invalid_path_params",
            ) from exc
        if not math.isfinite(value) or value <= 0:
            raise UserError(
                f"{name} must be finite and greater than zero",
                reason="invalid_path_params",
            )
        return value

    async def _mechanism_storage_index(
        self,
        netuid: int,
        mechid: int,
        block_hash: str,
    ) -> int:
        """Resolve ``(netuid, mechid)`` to Subtensor's mechanism storage index.

        Spec 440 stores mechanism-indexed vectors at
        ``mechid * 4096 + netuid``. Mechanism zero is layout-compatible with
        legacy runtimes and costs no additional existence read. Non-zero
        mechanisms are checked against ``MechanismCountCurrent`` at the same
        pinned block before any vector is read, avoiding a misleading zero
        for a mechanism that does not exist.
        """
        if not 0 <= netuid < _MECHANISM_STORAGE_STRIDE:
            raise DecodeError(
                f"subnet {netuid} is outside the mechanism storage range "
                f"0..{_MECHANISM_STORAGE_STRIDE - 1}"
            )
        if not 0 <= mechid <= _MAX_MECHANISM_ID:
            raise DecodeError(f"mechanism id must be 0..{_MAX_MECHANISM_ID}, got {mechid}")
        if mechid > 0:
            substrate = self._connected
            mechanism_count = _scale_to_int(
                await substrate.query(
                    "SubtensorModule",
                    "MechanismCountCurrent",
                    [netuid],
                    block_hash=block_hash,
                )
            )
            if mechid >= mechanism_count:
                raise DecodeError(
                    f"mechanism {mechid} does not exist on subnet {netuid}; "
                    f"current mechanism count is {mechanism_count}"
                )
        return mechid * _MECHANISM_STORAGE_STRIDE + netuid

    async def _read_subnet_alpha_supply(self, netuid: int, block_hash: str) -> float:
        info = await self._read_subnet_dynamic_info(netuid, block_hash)
        return _rao_to_tao(self._dynamic_int(info, "alpha_out"))

    @staticmethod
    def _fixed_point_bits(raw: object) -> int | None:
        """Extract the integer ``bits`` field from a fixed-point object.

        Async substrate decodes the current fixed-point types as
        ``{"bits": <int>}``. Returns ``None`` for a missing or malformed value.
        """
        if not isinstance(raw, dict):
            return None
        for k, v in raw.items():
            if k == "bits" and isinstance(v, int):
                return v
        return None

    # SubnetInfoRuntimeApi.get_dynamic_info encodes moving_price as a
    # substrate-fixed U32F32 (or equivalent — 32 fractional bits). The
    # divisor was originally written as ``1 << 64`` from a misread of the
    # type definition; verified against finney by comparing the converted
    # float to the spot price (tao_in / alpha_in) — the U32F32 reading
    # tracks spot to within EMA lag, the U64F64 reading is ~10**12 too
    # small.
    _MOVING_PRICE_FRACTIONAL_BITS: Final[int] = 32

    async def _read_subnet_moving_price(self, netuid: int, block_hash: str) -> float:
        """Moving price as a float, decoded from the U32F32 ``moving_price`` field."""
        info = await self._read_subnet_dynamic_info(netuid, block_hash)
        bits = self._fixed_point_bits(info.get("moving_price"))
        if bits is None:
            return 0.0
        return bits / (1 << self._MOVING_PRICE_FRACTIONAL_BITS)

    async def _read_subnet_volume(self, netuid: int, block_hash: str) -> float:
        info = await self._read_subnet_dynamic_info(netuid, block_hash)
        return _rao_to_tao(self._dynamic_int(info, "subnet_volume"))

    async def _read_subnet_price(self, netuid: int, block_hash: str) -> float:
        info = await self._read_subnet_dynamic_info(netuid, block_hash)
        tao = self._dynamic_int(info, "tao_in")
        alpha = self._dynamic_int(info, "alpha_in")
        return tao / alpha if alpha else 0.0

    async def _read_subnet_tao_depth(self, netuid: int, block_hash: str) -> float:
        info = await self._read_subnet_dynamic_info(netuid, block_hash)
        return _rao_to_tao(self._dynamic_int(info, "tao_in"))

    async def _read_subnet_alpha_depth(self, netuid: int, block_hash: str) -> float:
        info = await self._read_subnet_dynamic_info(netuid, block_hash)
        return _rao_to_tao(self._dynamic_int(info, "alpha_in"))

    async def _read_depth_for_trade(
        self, netuid: int, size_tao: float, max_bps: float, block_hash: str
    ) -> float:
        """Compute slippage margin for a trade of size_tao TAO.

        Returns a positive margin (in bps) when the pool can absorb the trade
        within max_bps slippage; non-positive otherwise.  Uses the constant-
        product AMM formula: actual_slippage = (price_after/price_before - 1).
        """
        if (
            not math.isfinite(size_tao)
            or size_tao <= 0
            or not math.isfinite(max_bps)
            or max_bps <= 0
        ):
            raise UserError(
                "size and max_bps must be finite and greater than zero",
                reason="invalid_path_params",
            )
        info = await self._read_subnet_dynamic_info(netuid, block_hash)
        tao_raw = self._dynamic_int(info, "tao_in")
        alpha_raw = self._dynamic_int(info, "alpha_in")
        if alpha_raw == 0 or tao_raw == 0:
            return -max_bps
        tao = tao_raw / RAO_PER_TAO
        alpha = alpha_raw / RAO_PER_TAO
        new_tao = tao + size_tao
        delta_alpha = alpha * size_tao / new_tao
        new_alpha = alpha - delta_alpha
        if new_alpha <= 0:
            return -max_bps
        actual_bps = (new_tao / new_alpha) / (tao / alpha) * 10_000.0 - 10_000.0
        return max_bps - actual_bps

    async def _read_subnet_registration_cost(self, netuid: int, block_hash: str) -> float:
        """Burn cost (TAO) to register a new neuron on this subnet."""
        substrate = self._connected
        burn = _scale_to_int(
            await substrate.query("SubtensorModule", "Burn", [netuid], block_hash=block_hash)
        )
        return _rao_to_tao(burn)

    async def _read_subnet_burn_rate(self, netuid: int, block_hash: str) -> float:
        """Last tempo's withheld miner-emission proportion (0-1).

        ``MinerBurned`` is a U96F32 value updated when this subnet's epoch
        settles. It counts miner incentive routed to subnet-owner hotkeys,
        regardless of whether the subnet recycles or burns the withheld alpha.
        """
        substrate = self._connected
        result = await substrate.query(
            "SubtensorModule", "MinerBurned", [netuid], block_hash=block_hash
        )
        raw = getattr(result, "value", result) if result is not None else None
        bits = self._fixed_point_bits(raw)
        if bits is None or bits <= 0:
            return 0.0
        return min(bits / (1 << U96F32_FRACTIONAL_BITS), 1.0)

    # SubnetEmaTaoFlow is stored as (u64, I64F64) — (last_update_block, ema_value).
    # The I64F64 fixed-point type uses 64 fractional bits; dividing the raw bits
    # integer by 2^64 yields the value in RAO. Verified against finney block ~8133183
    # where bits=-62366886535159848297984346 → -3381133 RAO → -0.003381 TAO (SN1).
    _EMA_TAO_FLOW_FRACTIONAL_BITS: Final[int] = 64

    async def _read_subnet_ema_tao_flow(self, netuid: int, block_hash: str) -> float:
        """EMA of TAO inflow/outflow for this subnet, in TAO.

        ``SubnetEmaTaoFlow`` stores ``(u64_block, I64F64_ema)`` on-chain.
        Positive = net TAO entering the subnet; negative = net TAO leaving.
        The I64F64 value is in RAO; we divide by 2^64 then by RAO_PER_TAO.
        """
        substrate = self._connected
        result = await substrate.query(
            "SubtensorModule", "SubnetEmaTaoFlow", [netuid], block_hash=block_hash
        )
        if result is None:
            return 0.0
        raw = getattr(result, "value", result)
        if raw is None:
            return 0.0
        if not isinstance(raw, tuple):
            return 0.0
        _block_num, *rest = raw
        bits = self._fixed_point_bits(rest[0] if rest else None)
        if bits is None:
            return 0.0
        return bits / (1 << self._EMA_TAO_FLOW_FRACTIONAL_BITS) / RAO_PER_TAO

    async def _read_subnet_emission_share(self, netuid: int, block_hash: str) -> float:
        """This subnet's share of the current block's TAO emission (0-1).

        Current runtimes split each subnet's routed TAO between liquidity-pool
        injection (``SubnetTaoInEmission``) and chain buybacks
        (``SubnetExcessTao``). Both storage values describe the last block and
        must be compared with that same block's ``get_block_emission`` result.
        """
        substrate = self._connected
        tao_in = _scale_to_int(
            await substrate.query(
                "SubtensorModule", "SubnetTaoInEmission", [netuid], block_hash=block_hash
            )
        )
        excess_tao = _scale_to_int(
            await substrate.query(
                "SubtensorModule", "SubnetExcessTao", [netuid], block_hash=block_hash
            )
        )
        block_emission = _scale_to_int(
            await substrate.runtime_call(
                "SubnetInfoRuntimeApi",
                "get_block_emission",
                [],
                block_hash=block_hash,
            )
        )
        if block_emission <= 0:
            return 0.0
        routed_emission = max(tao_in, 0) + max(excess_tao, 0)
        return min(routed_emission / block_emission, 1.0)

    async def _read_subnet_hyperparams(self, netuid: int, block_hash: str) -> dict[str, object]:
        """Snapshot of key hyperparameters for this subnet — one pinned batch.

        Spec 440 replaced the deprecated absolute ``ActivityCutoff`` with
        ``ActivityCutoffFactorMilli``. The effective cutoff remains useful to
        watcher consumers, so this returns both the raw factor and the current
        block count computed as ``max(1, factor * tempo // 1000)``.
        """
        param_defs: list[tuple[str, str]] = [
            ("tempo", "Tempo"),
            ("immunity_period", "ImmunityPeriod"),
            ("min_allowed_weights", "MinAllowedWeights"),
            ("max_weights_limit", "MaxWeightsLimit"),
            ("max_allowed_validators", "MaxAllowedValidators"),
            ("max_allowed_uids", "MaxAllowedUids"),
            ("activity_cutoff_factor_milli", "ActivityCutoffFactorMilli"),
            ("adjustment_interval", "AdjustmentInterval"),
            ("weights_version_key", "WeightsVersionKey"),
            ("weights_set_rate_limit", "WeightsSetRateLimit"),
            ("kappa", "Kappa"),
            ("rho", "Rho"),
        ]
        values = await self._query_multi_int(
            [("SubtensorModule", fn, [netuid]) for _, fn in param_defs],
            block_hash=block_hash,
        )
        result: dict[str, object] = {
            friendly: value for (friendly, _), value in zip(param_defs, values, strict=True)
        }
        tempo = _scale_to_int(result["tempo"])
        factor_milli = _scale_to_int(result["activity_cutoff_factor_milli"])
        result["activity_cutoff"] = max(1, factor_milli * tempo // 1_000)
        return result

    async def _read_subnet_identity(self, netuid: int, block_hash: str) -> dict[str, object]:
        """Return the full spec-440 subnet identity and owner records.

        ``SubnetInfoRuntimeApi.get_dynamic_info`` is the canonical runtime
        surface for ``SubnetIdentitiesV3``. It returns the complete identity
        (name, repository, contact, URL, Discord, description, logo, and
        additional data) plus both owner keys in one block-pinned read.
        """
        info = await self._read_subnet_dynamic_info(netuid, block_hash)
        if not info:
            return {}
        owner_hotkey = str(info.get("owner_hotkey", ""))
        owner_coldkey = str(info.get("owner_coldkey", ""))
        identity = info.get("subnet_identity")
        return {
            "netuid": self._dynamic_int(info, "netuid") or netuid,
            "owner_hotkey": owner_hotkey,
            "owner_coldkey": owner_coldkey,
            "subnet_identity": dict(identity) if isinstance(identity, dict) else {},
        }

    # ------------------------------------------------------------------
    # Validator handlers
    # ------------------------------------------------------------------

    async def _hotkey_uid(self, netuid: int, hotkey: str, block_hash: str) -> int | None:
        """Resolve hotkey to uid within a subnet. Returns None if not registered."""
        cache_key = (block_hash, netuid, hotkey)
        if cache_key in self._uid_cache:
            return self._uid_cache[cache_key]
        substrate = self._connected
        uid = await substrate.query(
            "SubtensorModule", "Uids", [netuid, hotkey], block_hash=block_hash
        )
        # The query returns an Option type; a None .value means "not registered".
        if uid is None or getattr(uid, "value", uid) is None:
            self._uid_cache[cache_key] = None
            return None
        decoded_uid = _scale_to_int(uid)
        self._uid_cache[cache_key] = decoded_uid
        return decoded_uid

    async def _query_list_at_uid(
        self, storage_fn: str, netuid: int, uid: int, block_hash: str
    ) -> int:
        """Query a per-subnet list storage and return the value at index uid."""
        substrate = self._connected
        raw = await substrate.query("SubtensorModule", storage_fn, [netuid], block_hash=block_hash)
        items = getattr(raw, "value", raw)
        if not isinstance(items, list) or uid >= len(items):
            return 0
        return _scale_to_int(items[uid])

    async def _read_validator_dividends(self, netuid: int, hotkey: str, block_hash: str) -> float:
        """Last-epoch dividends in the requested subnet's alpha token."""
        substrate = self._connected
        dividends = _scale_to_int(
            await substrate.query(
                "SubtensorModule",
                "AlphaDividendsPerSubnet",
                [netuid, hotkey],
                block_hash=block_hash,
            )
        )
        return _rao_to_alpha(dividends)

    async def _read_validator_stake(self, netuid: int, hotkey: str, block_hash: str) -> float:
        """Total stake for a hotkey in the requested subnet's alpha token."""
        substrate = self._connected
        total = _scale_to_int(
            await substrate.query(
                "SubtensorModule",
                "TotalHotkeyAlpha",
                [hotkey, netuid],
                block_hash=block_hash,
            )
        )
        return _rao_to_alpha(total)

    async def _read_validator_commission(self, hotkey: str, block_hash: str) -> float:
        """Commission (take) as a fraction 0-1."""
        substrate = self._connected
        take_raw = _scale_to_int(
            await substrate.query("SubtensorModule", "Delegates", [hotkey], block_hash=block_hash)
        )
        return take_raw / TAKE_NORMALISER

    async def _read_validator_last_weight_block(
        self,
        hotkey: str,
        block_hash: str,
        *,
        netuid: int = 1,
        mechid: int = 0,
    ) -> int:
        """Block where this hotkey last set weights on a subnet mechanism.

        Used as a liveness anchor: the liveness primitive fires when this
        value has not changed for N blocks/epochs.
        """
        storage_index = await self._mechanism_storage_index(netuid, mechid, block_hash)
        uid = await self._hotkey_uid(netuid, hotkey, block_hash)
        if uid is None:
            return 0
        return await self._query_list_at_uid("LastUpdate", storage_index, uid, block_hash)

    async def _read_validator_child_keys(
        self, hotkey: str, block_hash: str
    ) -> list[dict[str, object]]:
        """Child keys set on this hotkey across all subnets — one batched RPC."""
        substrate = self._connected
        total_networks = _scale_to_int(
            await substrate.query("SubtensorModule", "TotalNetworks", block_hash=block_hash)
        )
        if total_networks <= 0:
            return []
        result: list[dict[str, object]] = []
        netuids = list(range(1, total_networks + 1))
        storage_keys = [
            await substrate.create_storage_key("SubtensorModule", "ChildKeys", [hotkey, netuid])
            for netuid in netuids
        ]
        pairs = await substrate.query_multi(storage_keys, block_hash=block_hash)
        for netuid, (_, value) in zip(netuids, pairs, strict=True):
            items = getattr(value, "value", value)
            if isinstance(items, list) and items:
                for entry in items:
                    result.append({"netuid": netuid, "child": str(entry)})
        return result

    async def _read_validator_identity(
        self,
        hotkey: str,
        block_hash: str,
    ) -> dict[str, object]:
        """Return on-chain identity for ``hotkey`` from ``IdentitiesV2``.

        Substrate stores identities as ``BoundedVec<u8>`` fields (``name``,
        ``url``, ``github_repo``, ``image``, ``discord``, ``description``,
        ``additional``). Chainwake decodes valid UTF-8 as text and preserves
        arbitrary bytes as canonical hex. It owns this narrow decode boundary
        so its read-only provider does not depend on the transaction-oriented
        Bittensor SDK.

        Returns ``{}`` when no identity has been set for this hotkey, which
        keeps the value stable across ticks so ``--on-change`` does not fire.
        """
        substrate = self._connected
        identity_info = await substrate.query(
            "SubtensorModule", "IdentitiesV2", [hotkey], block_hash=block_hash
        )
        identity_data = getattr(identity_info, "value", identity_info)
        if not isinstance(identity_data, dict):
            return {}
        decoded = {key: _decode_identity_value(value) for key, value in identity_data.items()}
        return {
            "name": decoded["name"],
            "url": decoded["url"],
            "github": decoded["github_repo"],
            "image": decoded["image"],
            "discord": decoded["discord"],
            "description": decoded["description"],
            "additional": decoded["additional"],
        }

    # ------------------------------------------------------------------
    # Neuron handlers
    # ------------------------------------------------------------------

    async def _read_neuron_incentive(
        self,
        netuid: int,
        hotkey: str,
        block_hash: str,
        *,
        mechid: int = 0,
    ) -> float:
        storage_index = await self._mechanism_storage_index(netuid, mechid, block_hash)
        uid = await self._hotkey_uid(netuid, hotkey, block_hash)
        if uid is None:
            return 0.0
        return (
            await self._query_list_at_uid("Incentive", storage_index, uid, block_hash)
            / U16_NORMALISER
        )

    async def _read_neuron_dividends(self, netuid: int, hotkey: str, block_hash: str) -> float:
        uid = await self._hotkey_uid(netuid, hotkey, block_hash)
        if uid is None:
            return 0.0
        return await self._query_list_at_uid("Dividends", netuid, uid, block_hash) / U16_NORMALISER

    async def _read_neuron_stake(self, netuid: int, hotkey: str, block_hash: str) -> float:
        substrate = self._connected
        total = _scale_to_int(
            await substrate.query(
                "SubtensorModule", "TotalHotkeyAlpha", [hotkey, netuid], block_hash=block_hash
            )
        )
        return _rao_to_alpha(total)

    async def _read_neuron_last_update(
        self,
        netuid: int,
        hotkey: str,
        block_hash: str,
        *,
        mechid: int = 0,
    ) -> int:
        """Block number of the mechanism's last weight-set (liveness anchor)."""
        storage_index = await self._mechanism_storage_index(netuid, mechid, block_hash)
        uid = await self._hotkey_uid(netuid, hotkey, block_hash)
        if uid is None:
            return 0
        return await self._query_list_at_uid("LastUpdate", storage_index, uid, block_hash)

    async def _read_blocks_until_immunity(
        self, netuid: int, hotkey: str, block_number: int, block_hash: str
    ) -> int:
        """Blocks remaining in the runtime's strictly block-based immunity."""
        substrate = self._connected
        uid = await self._hotkey_uid(netuid, hotkey, block_hash)
        if uid is None:
            return 0
        immunity_period = _scale_to_int(
            await substrate.query(
                "SubtensorModule", "ImmunityPeriod", [netuid], block_hash=block_hash
            )
        )
        block_at_reg = _scale_to_int(
            await substrate.query(
                "SubtensorModule", "BlockAtRegistration", [netuid, uid], block_hash=block_hash
            )
        )
        expiry_block = block_at_reg + immunity_period
        return max(expiry_block - block_number, 0)

    # ------------------------------------------------------------------
    # Account handlers
    # ------------------------------------------------------------------

    async def _read_account_balance(self, coldkey: str, block_hash: str) -> float:
        """Free balance in TAO."""
        substrate = self._connected
        account = await substrate.query("System", "Account", [coldkey], block_hash=block_hash)
        if account is None:
            return 0.0
        data = getattr(account, "value", account)
        if isinstance(data, dict) and "data" in data:
            free_rao = _scale_to_int(data["data"].get("free", 0))
        else:
            free_rao = 0
        return _rao_to_tao(free_rao)

    async def _read_account_activity(self, coldkey: str, block_hash: str) -> int:
        """Block number of the last transaction from this coldkey (liveness anchor)."""
        substrate = self._connected
        return _scale_to_int(
            await substrate.query(
                "SubtensorModule", "LastTxBlock", [coldkey], block_hash=block_hash
            )
        )

    # ------------------------------------------------------------------
    # Network handlers
    # ------------------------------------------------------------------

    async def _read_network_subnet_registration_cost(self, block_hash: str) -> float:
        """Network-wide lock cost to register a new subnet (TAO)."""
        substrate = self._connected
        cost = _scale_to_int(
            await substrate.query("SubtensorModule", "NetworkLastLockCost", block_hash=block_hash)
        )
        return _rao_to_tao(cost)

    async def _read_network_runtime_version(self, block_hash: str) -> dict[str, object]:
        """Current runtime spec version."""
        substrate = self._connected
        last_upgrade = await substrate.query("System", "LastRuntimeUpgrade", block_hash=block_hash)
        data = getattr(last_upgrade, "value", last_upgrade)
        if isinstance(data, dict):
            as_str_dict: dict[str, object] = {str(k): v for k, v in data.items()}
            return {
                "spec_version": _scale_to_int(as_str_dict.get("spec_version")),
                "spec_name": str(as_str_dict.get("spec_name") or ""),
            }
        return {"spec_version": 0, "spec_name": ""}

    async def _read_network_subnet_count(self, block_hash: str) -> int:
        """Total number of registered subnets."""
        substrate = self._connected
        return _scale_to_int(
            await substrate.query("SubtensorModule", "TotalNetworks", block_hash=block_hash)
        )

    # ------------------------------------------------------------------
    # Head subscription
    # ------------------------------------------------------------------

    def subscribe_heads(
        self,
        *,
        charge_rpc: Callable[[int], None] | None = None,
    ) -> AsyncIterator[BlockRef]:
        """Async iterator over best-chain block references."""
        return _HeadSubscription(self._connected, charge_rpc=charge_rpc)

    # ------------------------------------------------------------------
    # Event subscription
    # ------------------------------------------------------------------

    def subscribe_events(
        self,
        event_filter: EventFilter,
        *,
        charge_rpc: Callable[[int], None] | None = None,
    ) -> AsyncIterator[Event]:
        """Async iterator over filtered chain events.

        Follows direct best-head block references and decodes events at each
        pinned hash. Friendly event types are resolved via
        ``_FRIENDLY_TO_SUBSTRATE``; raw Substrate names pass through unchanged.
        """
        _validate_event_filter_addresses(event_filter)
        return _EventSubscription(
            self._connected,
            event_filter,
            charge_rpc=charge_rpc,
        )

    # ------------------------------------------------------------------
    # Storage subscription
    # ------------------------------------------------------------------

    def subscribe_storage(
        self,
        path: str,
        *,
        charge_rpc: Callable[[int], None] | None = None,
    ) -> AsyncIterator[StorageUpdate]:
        """Async iterator over storage updates for a path.

        Registry policy maps the public path to one or more exact storage
        keys. Raw ``state_subscribeStorage`` notifications retain their block
        hash and are wrapped into a queue.
        """
        _validate_observable_path_addresses(path)
        return _StorageSubscription(self._connected, path, charge_rpc=charge_rpc)

    # ------------------------------------------------------------------
    # Block finality
    # ------------------------------------------------------------------

    async def get_block_finality(self, tx_hash: str) -> TxFinalityStatus:
        """Return finality status for a transaction hash.

        The first call performs one bounded historical scan of at most
        ``_TX_SEARCH_HORIZON_BLOCKS``. A miss remains ``pending`` because the
        transaction may still land later. Subsequent calls scan only blocks
        after the previous head. Once included, its block metadata is cached
        and later calls only inspect the finalized head.

        A backwards or same-height changed head invalidates the cursor and
        triggers a bounded rescan. Substrate RPC failures are classified and
        propagated so the runtime retry/rate-limit policy remains in control.
        """
        try:
            validate_tx_hash(tx_hash)
        except ValueError as exc:
            raise UserError(str(exc), reason="invalid_path_params") from exc
        substrate = self._connected
        cache_key = tx_hash.lower()
        state = self._tx_scan_state.get(cache_key)
        try:
            if state is not None and state.included is not None:
                refreshed = await self._refresh_cached_tx_finality(state.included)
                if refreshed is not None:
                    return refreshed
                # The included block is no longer canonical. Rebuild the
                # cursor below with one bounded scan.
                self._tx_scan_state.pop(cache_key, None)
                state = None

            head_hash = await substrate.get_chain_head()
            head_num = await substrate.get_block_number(head_hash)
            if head_num is None:
                return TxFinalityStatus(tx_hash=tx_hash, level="pending")

            if state is None:
                oldest_block = max(1, head_num - _TX_SEARCH_HORIZON_BLOCKS + 1)
                state = _TxScanState(head_num, head_hash)
                self._tx_scan_state[cache_key] = state
                self._begin_tx_scan(state, head_num, head_hash, oldest_block)
            elif state.scan_next_block is not None:
                # A prior call failed mid-range. Keep its exact cursor so a
                # retry resumes at the failed height rather than rereading
                # successfully inspected blocks.
                pass
            elif head_num > state.last_head_num:
                previous_head_hash = await substrate.get_block_hash(state.last_head_num)
                oldest_block = (
                    state.last_head_num + 1
                    if previous_head_hash == state.last_head_hash
                    else max(1, head_num - _TX_SEARCH_HORIZON_BLOCKS + 1)
                )
                self._begin_tx_scan(state, head_num, head_hash, oldest_block)
            elif head_num == state.last_head_num and head_hash == state.last_head_hash:
                return TxFinalityStatus(tx_hash=tx_hash, level="pending")
            else:
                # Practical reorg recovery: a backwards head, or a changed hash
                # at the same height, invalidates the incremental cursor.
                oldest_block = max(1, head_num - _TX_SEARCH_HORIZON_BLOCKS + 1)
                self._begin_tx_scan(state, head_num, head_hash, oldest_block)

            match_location = (
                (state.match_block_num, state.match_block_hash)
                if state.match_block_num is not None and state.match_block_hash is not None
                else await self._scan_for_tx(tx_hash, state)
            )
            if match_location is None:
                self._finish_tx_scan(state)
                return TxFinalityStatus(tx_hash=tx_hash, level="pending")

            block_num, block_hash = match_location
            status = await self._build_finality_status(tx_hash, block_num, block_hash)
            if status is None:
                # The matched block was reorged between scanning its body and
                # finality evaluation. Discard the cursor so the next poll
                # performs one bounded canonical rescan.
                self._tx_scan_state.pop(cache_key, None)
                return TxFinalityStatus(tx_hash=tx_hash, level="pending")
            self._finish_tx_scan(state)
            state.included = status
            return status
        except SubstrateRequestException as exc:
            raise _wrap_substrate_exception(exc) from exc

    @staticmethod
    def _begin_tx_scan(
        state: _TxScanState,
        head_num: int,
        head_hash: str,
        oldest_block: int,
    ) -> None:
        """Persist a scan range before issuing its first block RPC."""
        state.scan_next_block = head_num
        state.scan_oldest_block = oldest_block
        state.scan_head_num = head_num
        state.scan_head_hash = head_hash

    @staticmethod
    def _finish_tx_scan(state: _TxScanState) -> None:
        """Promote a completed range to the incremental head cursor."""
        if state.scan_head_num is not None and state.scan_head_hash is not None:
            state.last_head_num = state.scan_head_num
            state.last_head_hash = state.scan_head_hash
        state.scan_next_block = None
        state.scan_oldest_block = None
        state.scan_head_num = None
        state.scan_head_hash = None
        state.match_block_num = None
        state.match_block_hash = None

    async def _refresh_cached_tx_finality(
        self,
        included: TxFinalityStatus,
    ) -> TxFinalityStatus | None:
        """Refresh cached inclusion, or return None when it was reorged out."""
        if included.level == "finalized":
            return included
        substrate = self._connected
        # Snapshot finality before checking the cached block's canonical hash.
        # Checking in the opposite order leaves a TOCTOU window where a reorg
        # can occur during the finality lookup and promote a stale inclusion.
        finalized_hash = await substrate.get_chain_finalised_head()
        finalized_num = await substrate.get_block_number(finalized_hash)
        if included.block is not None and included.block_hash is not None:
            canonical_hash = await substrate.get_block_hash(included.block)
            if canonical_hash != included.block_hash:
                return None
        if (
            finalized_num is not None
            and included.block is not None
            and included.block <= finalized_num
        ):
            return TxFinalityStatus(
                tx_hash=included.tx_hash,
                level="finalized",
                block=included.block,
                block_hash=included.block_hash,
                timestamp=included.timestamp,
            )
        return included

    async def _scan_for_tx(
        self,
        tx_hash: str,
        state: _TxScanState,
    ) -> tuple[int, str] | None:
        """Scan the persisted inclusive range, advancing after each success."""
        substrate = self._connected
        while (
            state.scan_next_block is not None
            and state.scan_oldest_block is not None
            and state.scan_next_block >= state.scan_oldest_block
        ):
            block_num = state.scan_next_block
            ext_hashes = await self._block_extrinsic_hashes(substrate, block_num)
            # Advance only after both block RPCs succeeded. On any exception,
            # the retry resumes at this exact height.
            state.scan_next_block = block_num - 1
            if ext_hashes is None:
                continue
            for block_hash, ext_hash in ext_hashes:
                if ext_hash.lower() == tx_hash.lower():
                    state.match_block_num = block_num
                    state.match_block_hash = block_hash
                    return block_num, block_hash
        return None

    @staticmethod
    async def _block_extrinsic_hashes(
        substrate: AsyncSubstrateInterface, block_num: int
    ) -> list[tuple[str, str]] | None:
        """Hash raw block extrinsics without loading historical runtime state."""
        block_hash = await substrate.get_block_hash(block_num)
        if block_hash is None:
            return None

        response = await substrate.rpc_request("chain_getBlock", [block_hash])
        result = response.get("result")
        if result is None:
            raise TxNotFoundInHorizonError(
                f"block body {block_hash} at height {block_num} is unavailable; "
                "transaction history lookup requires an archive node"
            )
        if not isinstance(result, dict):
            raise DecodeError("chain_getBlock result must be an object or null")
        block = result.get("block")
        if not isinstance(block, dict):
            raise DecodeError("chain_getBlock result.block must be an object")
        extrinsics = block.get("extrinsics")
        if not isinstance(extrinsics, list):
            raise DecodeError("chain_getBlock result.block.extrinsics must be a list")
        if not extrinsics:
            return None

        results: list[tuple[str, str]] = []
        for index, encoded in enumerate(extrinsics):
            if (
                not isinstance(encoded, str)
                or not encoded.startswith("0x")
                or len(encoded) % 2 != 0
            ):
                raise DecodeError(f"chain_getBlock extrinsic {index} must be even-length 0x hex")
            try:
                raw = bytes.fromhex(encoded[2:])
            except ValueError as exc:
                raise DecodeError(
                    f"chain_getBlock extrinsic {index} is not valid hexadecimal"
                ) from exc
            ext_hash = "0x" + blake2b(raw, digest_size=32).hexdigest()
            results.append((block_hash, ext_hash))
        return results

    async def _build_finality_status(
        self,
        tx_hash: str,
        block_num: int,
        block_hash: str,
    ) -> TxFinalityStatus | None:
        """Build and cache authoritative inclusion metadata for a transaction."""
        substrate = self._connected
        finalized_hash = await substrate.get_chain_finalised_head()
        finalized_num = await substrate.get_block_number(finalized_hash)
        canonical_hash = await substrate.get_block_hash(block_num)
        if canonical_hash != block_hash:
            return None
        timestamp = await self._block_timestamp(block_hash)
        is_final = finalized_num is not None and block_num <= finalized_num
        level = "finalized" if is_final else "included"
        return TxFinalityStatus(
            tx_hash=tx_hash,
            level=level,  # type: ignore[arg-type]
            block=block_num,
            block_hash=block_hash,
            timestamp=timestamp,
        )

    # ------------------------------------------------------------------
    # Natural cadence
    # ------------------------------------------------------------------

    def natural_cadence_for(self, observable_path: str) -> Cadence:
        try:
            return lookup_rendered(observable_path).natural_cadence
        except KeyError:
            return Cadence.PER_BLOCK


# ---------------------------------------------------------------------------
# Internal async iterators
# ---------------------------------------------------------------------------


class _HeadSubscription:
    """Yield best heads via ``chainHead_v1_follow``, with legacy fallback."""

    def __init__(
        self,
        substrate: AsyncSubstrateInterface,
        *,
        charge_rpc: Callable[[int], None] | None = None,
    ) -> None:
        self._substrate = substrate
        self._charge_rpc = charge_rpc if charge_rpc is not None else lambda _cost: None
        self._queue: asyncio.Queue[BlockRef | BaseException | None] = asyncio.Queue(maxsize=64)
        self._task: asyncio.Task[None] | None = None
        self._follow_subscription: str | None = None
        self._follow_stopped = False
        self._pinned: set[str] = set()
        self._pending_unpins: set[str] = set()
        self._block_numbers: dict[str, int] = {}
        self._finalized_hash: str | None = None

    def __aiter__(self) -> _HeadSubscription:
        return self

    async def __anext__(self) -> BlockRef:
        if self._task is None:
            self._task = asyncio.create_task(self._subscribe())
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        return item

    async def _subscribe(self) -> None:
        try:
            try:
                await self._subscribe_chainhead()
            except SubstrateRequestException as exc:
                if not self._chainhead_unavailable(exc):
                    raise
                self._follow_subscription = None
                await self._subscribe_legacy()
        except asyncio.CancelledError:
            raise
        except (BudgetExhaustedError, ProviderError) as exc:
            await self._queue.put(exc)
        except SubstrateRequestException as exc:
            await self._queue.put(_wrap_substrate_exception(exc))
        except Exception as exc:
            await self._queue.put(
                SubscriptionFailedError(
                    f"new-head subscription failed: {type(exc).__name__}: {exc}"
                )
            )
        finally:
            await self._queue.put(None)

    async def _subscribe_chainhead(self) -> None:
        """Follow the best chain with direct hashes and inferred heights."""

        async def result_handler(
            message: dict[str, object],
            subscription_id: str,
        ) -> tuple[dict[str, object], bool]:
            self._follow_subscription = str(subscription_id)
            return {}, await self._handle_chainhead_message(message)

        self._charge_rpc(1)
        await self._substrate.rpc_request(
            "chainHead_v1_follow",
            [False],
            result_handler=result_handler,
        )
        if not self._follow_stopped:
            raise SubscriptionFailedError("chainHead follow ended unexpectedly")

    async def _handle_chainhead_message(self, message: dict[str, object]) -> bool:
        params_raw = message.get("params")
        if not isinstance(params_raw, dict):
            # Initial JSON-RPC response containing the opaque subscription id.
            return False
        params = cast("dict[str, object]", params_raw)
        result_raw = params.get("result")
        if not isinstance(result_raw, dict):
            raise DecodeError("chainHead follow returned an invalid notification")
        result = cast("dict[str, object]", result_raw)
        event = result.get("event")
        if event == "initialized":
            await self._initialize_chainhead(result)
        elif event == "newBlock":
            self._record_chainhead_block(result)
        elif event == "bestBlockChanged":
            await self._emit_chainhead_best(result)
        elif event == "finalized":
            await self._record_chainhead_finality(result)
        elif event == "stop":
            self._follow_stopped = True
            await self._queue.put(
                SubscriptionFailedError("chainHead follow stopped; subscription must reconnect")
            )
            return True
        # Operation notifications cannot occur because Chainwake starts no
        # chainHead body/call/storage operation on this subscription.
        return False

    async def _initialize_chainhead(self, result: dict[str, object]) -> None:
        hashes = self._chainhead_hashes(result.get("finalizedBlockHashes"), "initialized")
        if not hashes:
            raise DecodeError("chainHead initialized without a finalized block")
        self._pinned.update(hashes)
        finalized_hash = hashes[-1]

        # Only this one setup read is needed. Every later newBlock height is
        # parent height + 1, and the protocol guarantees ordered parents.
        self._charge_rpc(1)
        header = await self._substrate.get_block_header(finalized_hash)
        if not isinstance(header, dict):
            raise DecodeError("chainHead finalized block header was unavailable")
        header_raw = cast("dict[str, object]", header).get("header")
        header_dict = cast("dict[str, object]", header_raw) if isinstance(header_raw, dict) else {}
        self._block_numbers[finalized_hash] = self._block_number(
            header_dict.get("number"),
            source="chainHead finalized header",
        )
        self._finalized_hash = finalized_hash
        if len(hashes) > 1:
            await self._unpin(hashes[:-1])

    def _record_chainhead_block(self, result: dict[str, object]) -> None:
        block_hash = self._chainhead_hash(result.get("blockHash"), "newBlock.blockHash")
        parent_hash = self._chainhead_hash(
            result.get("parentBlockHash"),
            "newBlock.parentBlockHash",
        )
        parent_number = self._block_numbers.get(parent_hash)
        if parent_number is None:
            raise DecodeError(
                f"chainHead newBlock parent {parent_hash!r} has no known block number"
            )
        self._pinned.add(block_hash)
        self._block_numbers[block_hash] = parent_number + 1

    async def _emit_chainhead_best(self, result: dict[str, object]) -> None:
        block_hash = self._chainhead_hash(
            result.get("bestBlockHash"),
            "bestBlockChanged.bestBlockHash",
        )
        number = self._block_numbers.get(block_hash)
        if number is None:
            raise DecodeError(
                f"chainHead bestBlockChanged hash {block_hash!r} has no known block number"
            )
        await self._queue.put(BlockRef(number=number, hash=block_hash))

    async def _record_chainhead_finality(self, result: dict[str, object]) -> None:
        finalized = self._chainhead_hashes(result.get("finalizedBlockHashes"), "finalized")
        pruned = self._chainhead_hashes(result.get("prunedBlockHashes"), "pruned")
        latest = finalized[-1] if finalized else self._finalized_hash
        obsolete = set(pruned)
        if self._finalized_hash is not None and self._finalized_hash != latest:
            obsolete.add(self._finalized_hash)
        obsolete.update(block_hash for block_hash in finalized if block_hash != latest)
        if latest is not None:
            obsolete.discard(latest)
            self._finalized_hash = latest
        self._pending_unpins.update(
            block_hash for block_hash in obsolete if block_hash in self._pinned
        )
        if len(self._pending_unpins) >= _CHAINHEAD_UNPIN_BATCH:
            await self._unpin(sorted(self._pending_unpins))

    async def _unpin(self, hashes: list[str]) -> None:
        if not hashes or self._follow_subscription is None:
            return
        unique_hashes = list(dict.fromkeys(hashes))
        self._charge_rpc(1)
        await self._substrate.rpc_request(
            "chainHead_v1_unpin",
            [self._follow_subscription, unique_hashes],
        )
        for block_hash in unique_hashes:
            self._pinned.discard(block_hash)
            self._pending_unpins.discard(block_hash)
            self._block_numbers.pop(block_hash, None)

    async def _subscribe_legacy(self) -> None:
        substrate = self._substrate

        async def handler(obj: object) -> None:
            block_dict = cast("dict[str, object]", obj) if isinstance(obj, dict) else {}
            header_raw = block_dict.get("header")
            header = cast("dict[str, object]", header_raw) if isinstance(header_raw, dict) else {}
            number = self._block_number(
                header.get("number"),
                source="legacy new-head subscription",
            )
            self._charge_rpc(1)
            block_hash = await substrate.get_block_hash(number)
            if block_hash is None:
                raise SubscriptionFailedError(
                    f"new-head subscription could not resolve block {number}"
                )
            await self._queue.put(BlockRef(number=number, hash=block_hash))

        # The SDK resolves the current head once before attaching
        # chain_subscribeNewHeads.
        self._charge_rpc(1)
        await substrate.subscribe_block_headers(handler)
        raise SubscriptionFailedError("legacy new-head subscription ended unexpectedly")

    @staticmethod
    def _block_number(value: object, *, source: str) -> int:
        if isinstance(value, str):
            try:
                return int(value, 0)
            except ValueError as exc:
                raise DecodeError(f"{source} returned invalid block number {value!r}") from exc
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        raise DecodeError(f"{source} returned invalid block number {value!r}")

    @staticmethod
    def _chainhead_hash(value: object, source: str) -> str:
        if isinstance(value, str) and value.startswith("0x") and value != "0x":
            return value
        raise DecodeError(f"chainHead {source} returned invalid block hash {value!r}")

    @classmethod
    def _chainhead_hashes(cls, value: object, source: str) -> list[str]:
        if not isinstance(value, list):
            raise DecodeError(f"chainHead {source} returned invalid block hashes")
        return [cls._chainhead_hash(item, source) for item in value]

    @staticmethod
    def _chainhead_unavailable(exc: SubstrateRequestException) -> bool:
        message = str(exc).lower()
        return any(
            marker in message
            for marker in ("method not found", "unknown method", "not supported", "-32601")
        )

    async def aclose(self) -> None:
        if self._task is not None:
            task = self._task
            task.cancel()
            while not self._queue.empty():
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._queue.get_nowait()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._task = None
        if self._follow_subscription is not None and not self._follow_stopped:
            with contextlib.suppress(Exception):
                await self._substrate.rpc_request(
                    "chainHead_v1_unfollow",
                    [self._follow_subscription],
                )
        self._follow_subscription = None
        self._pinned.clear()
        self._pending_unpins.clear()
        self._block_numbers.clear()
        self._finalized_hash = None


class _EventSubscription:
    """Decode filtered events at each direct best-head block reference."""

    def __init__(
        self,
        substrate: AsyncSubstrateInterface,
        event_filter: EventFilter,
        *,
        charge_rpc: Callable[[int], None] | None = None,
    ) -> None:
        self._substrate = substrate
        self._filter = event_filter
        self._charge_rpc = charge_rpc if charge_rpc is not None else lambda _cost: None
        self._target_types: frozenset[str] = frozenset(event_filter.event_types)
        self._queue: asyncio.Queue[Event | BaseException | None] = asyncio.Queue(maxsize=1024)
        self._task: asyncio.Task[None] | None = None
        self._argument_names: dict[tuple[str, str], tuple[str | None, ...]] = {}

    def __aiter__(self) -> _EventSubscription:
        return self

    async def __anext__(self) -> Event:
        if self._task is None:
            self._task = asyncio.create_task(self._poll_loop())
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        return item

    async def _poll_loop(self) -> None:
        substrate = self._substrate
        heads = _HeadSubscription(substrate, charge_rpc=self._charge_rpc)
        try:
            async for head in heads:
                if head.number is None or head.hash is None:
                    raise SubscriptionFailedError(
                        "event head subscription yielded an unpinned block reference"
                    )
                self._charge_rpc(1)
                events = await substrate.get_events(block_hash=head.hash)
                self._charge_rpc(1)
                timestamp_raw = await substrate.query(
                    "Timestamp",
                    "Now",
                    block_hash=head.hash,
                )
                if timestamp_raw is None:
                    raise DecodeError(
                        f"Timestamp.Now returned None for block {head.hash}; "
                        "chain block lacks an authoritative timestamp"
                    )
                timestamp = datetime.fromtimestamp(
                    _scale_to_int(timestamp_raw) / 1000.0,
                    tz=UTC,
                )
                for raw_event in events:
                    names = await self._metadata_argument_names(raw_event, head.hash)
                    ev = self._decode_event(
                        raw_event,
                        head.number,
                        head.hash,
                        timestamp,
                        argument_names=names,
                    )
                    if ev is not None:
                        await self._queue.put(ev)
            await self._queue.put(SubscriptionFailedError("event subscription ended unexpectedly"))
        except asyncio.CancelledError:
            raise
        except (BudgetExhaustedError, ProviderError) as exc:
            await self._queue.put(exc)
        except SubstrateRequestException as exc:
            await self._queue.put(_wrap_substrate_exception(exc))
        except Exception as exc:
            await self._queue.put(
                SubscriptionFailedError(f"event subscription failed: {type(exc).__name__}: {exc}")
            )
        finally:
            await heads.aclose()
            await self._queue.put(None)

    def _decode_event(
        self,
        raw: dict[str, object],
        block_number: int,
        block_hash: str,
        timestamp: datetime,
        *,
        argument_names: tuple[str | None, ...] | None = None,
    ) -> Event | None:
        module_id = str(raw.get("module_id", ""))
        event_id = str(raw.get("event_id", ""))
        substrate_name = f"{module_id}.{event_id}"
        attrs = raw.get("attributes")
        if isinstance(attrs, dict):
            args = {str(k): v for k, v in attrs.items()}
        elif isinstance(attrs, (tuple, list)):
            args = {}
            for index, value in enumerate(attrs):
                candidate = (
                    argument_names[index]
                    if argument_names is not None and index < len(argument_names)
                    else None
                )
                key = candidate if candidate and candidate not in args else f"arg_{index}"
                args[key] = value
        else:
            args = {}

        matched_friendly = [
            f for f, substrates in _FRIENDLY_TO_SUBSTRATE.items() if substrate_name in substrates
        ]
        is_raw_match = substrate_name in self._target_types
        friendly_matches = [f for f in matched_friendly if f in self._target_types]

        if not is_raw_match and not friendly_matches:
            return None

        if not self._args_match(args):
            return None
        if not self._amount_min_matches(args):
            return None
        if not self._direction_matches(args):
            return None

        friendly_type = friendly_matches[0] if friendly_matches else substrate_name
        return Event(
            event_type=friendly_type,
            raw_event=substrate_name,
            args=args,
            block=block_number,
            block_hash=block_hash,
            timestamp=timestamp,
        )

    async def _metadata_argument_names(
        self,
        raw: dict[str, object],
        block_hash: str,
    ) -> tuple[str | None, ...] | None:
        """Return event field names, retaining ``None`` for unnamed tuple fields.

        Runtime metadata is cached by pallet/event for the lifetime of a
        subscription. If metadata lookup fails, positional ``arg_N`` keys in
        ``_decode_event`` still preserve every decoded value.
        """
        attrs = raw.get("attributes")
        if not isinstance(attrs, (tuple, list)):
            return None
        module_id = str(raw.get("module_id", ""))
        event_id = str(raw.get("event_id", ""))
        cache_key = (module_id, event_id)
        if cache_key in self._argument_names:
            return self._argument_names[cache_key]
        try:
            self._charge_rpc(1)
            metadata = await self._substrate.get_metadata_event(
                module_id,
                event_id,
                block_hash,
            )
            serialized = getattr(metadata, "value_serialized", metadata)
            fields = serialized.get("fields", []) if isinstance(serialized, dict) else []
            names = tuple(
                str(field["name"])
                if isinstance(field, dict) and isinstance(field.get("name"), str)
                else None
                for field in fields
            )
        except (BudgetExhaustedError, ProviderError):
            raise
        except Exception:
            names = ()
        curated = _POSITIONAL_EVENT_ARGUMENT_NAMES.get(f"{module_id}.{event_id}")
        if curated is not None:
            names = tuple(
                name if name is not None else curated[index] if index < len(curated) else None
                for index, name in enumerate(names or (None,) * len(curated))
            )
        self._argument_names[cache_key] = names
        return names

    def _args_match(self, args: dict[str, object]) -> bool:
        """Exact-match AND filter — args[k] == v for each (k, v) declared."""
        return all(args.get(k) == v for k, v in self._filter.args_match.items())

    def _amount_min_matches(self, args: dict[str, object]) -> bool:
        """`amount_min` predicate: args["amount"] (or "value") >= threshold.

        BalanceTransfer-shaped events expose the amount as `amount`;
        `value` is a fallback used by some Substrate variants. Returns
        False (drop) if no amount-shaped field is present, since the
        filter cannot meaningfully apply.
        """
        threshold = self._filter.amount_min
        if threshold is None:
            return True
        amount = args.get("amount", args.get("value"))
        if not isinstance(amount, (int, float)):
            return False
        return amount >= threshold

    def _direction_matches(self, args: dict[str, object]) -> bool:
        """`direction` predicate: args["from"|"to"] == direction_address.

        ``"both"`` is a no-op kept for CLI symmetry. ``"in"`` requires
        ``args["to"] == direction_address``; ``"out"`` requires
        ``args["from"] == direction_address``.
        """
        direction = self._filter.direction
        if direction is None or direction == "both":
            return True
        addr = self._filter.direction_address
        if direction == "in":
            return args.get("to") == addr
        return args.get("from") == addr

    async def aclose(self) -> None:
        if self._task is not None:
            task = self._task
            task.cancel()
            while not self._queue.empty():
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._queue.get_nowait()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._task = None


class _StorageSubscription:
    """Async iterator over block-pinned ``state_subscribeStorage`` updates."""

    def __init__(
        self,
        substrate: AsyncSubstrateInterface,
        path: str,
        *,
        charge_rpc: Callable[[int], None] | None = None,
    ) -> None:
        self._substrate = substrate
        self._path = path
        self._charge_rpc = charge_rpc if charge_rpc is not None else lambda _cost: None
        self._queue: asyncio.Queue[StorageUpdate | BaseException | None] = asyncio.Queue(maxsize=64)
        self._task: asyncio.Task[None] | None = None
        self._previous: object = None

    def __aiter__(self) -> _StorageSubscription:
        return self

    async def __anext__(self) -> StorageUpdate:
        if self._task is None:
            self._task = asyncio.create_task(self._subscribe())
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        return item

    async def _subscribe(self) -> None:
        substrate = self._substrate
        try:
            entry = lookup_rendered(self._path)
            bindings = entry.observation_policy.storage_bindings
            if not bindings:
                raise NotImplementedError(
                    f"subscribe_storage: no exact-key policy for path {self._path!r}"
                )
            bound_params = entry.bind_rendered_path(self._path)
            storage_keys: list[StorageKey] = []
            for binding in bindings:
                params = [bound_params[name] for name in binding.path_params]
                self._charge_rpc(1)
                storage_keys.append(
                    await substrate.create_storage_key(
                        binding.module,
                        binding.storage_function,
                        params,
                    )
                )

            key_hexes = [storage_key.to_hex() for storage_key in storage_keys]

            async def handler(
                message: dict[str, Any],
                _sub_id: str,
            ) -> tuple[dict[str, Any], bool]:
                # The RPC machinery passes the initial subscription
                # acknowledgement through the same handler before change
                # notifications begin.
                if "params" not in message:
                    return message, False
                params = message.get("params")
                result = params.get("result") if isinstance(params, dict) else None
                if not isinstance(result, dict):
                    raise SubscriptionFailedError("storage subscription notification has no result")
                block_hash = result.get("block")
                changes = result.get("changes")
                if not isinstance(block_hash, str) or not isinstance(changes, list):
                    raise SubscriptionFailedError(
                        "storage subscription notification is missing its block or changes"
                    )

                self._charge_rpc(1)
                self._charge_rpc(1)
                block_number = await substrate.get_block_number(block_hash)
                if block_number is None:
                    raise SubscriptionFailedError(
                        f"storage subscription could not resolve block {block_hash}"
                    )
                changed_values = tuple(
                    change[1]
                    for change in changes
                    if isinstance(change, list | tuple) and len(change) == _STORAGE_CHANGE_FIELDS
                )
                value: object = changed_values[0] if len(changed_values) == 1 else changed_values
                update = StorageUpdate(
                    path=self._path,
                    value=value,
                    previous_value=self._previous,
                    block=block_number,
                    block_hash=block_hash,
                    # This timestamp is transport metadata only. The runtime
                    # re-reads the observable and uses its chain timestamp.
                    timestamp=datetime.now(UTC),
                )
                self._previous = value
                await self._queue.put(update)
                return message, False

            self._charge_rpc(1)
            await substrate.rpc_request(
                "state_subscribeStorage",
                [key_hexes],
                result_handler=handler,
            )
            await self._queue.put(
                SubscriptionFailedError("storage subscription ended unexpectedly")
            )
        except asyncio.CancelledError:
            raise
        except (BudgetExhaustedError, ProviderError, NotImplementedError) as exc:
            await self._queue.put(exc)
        except SubstrateRequestException as exc:
            await self._queue.put(_wrap_substrate_exception(exc))
        except Exception as exc:
            await self._queue.put(
                SubscriptionFailedError(f"storage subscription failed: {type(exc).__name__}: {exc}")
            )
        finally:
            await self._queue.put(None)

    async def aclose(self) -> None:
        if self._task is not None:
            task = self._task
            task.cancel()
            while not self._queue.empty():
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._queue.get_nowait()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._task = None


__all__ = ["DEFAULT_RPC_URL", "BittensorProvider"]
