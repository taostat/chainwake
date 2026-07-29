"""Profile-driven EVM JSON-RPC provider."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final, Literal, cast

import websockets

from chainwake.core.errors import (
    AuthError,
    CUExhaustedError,
    DecodeError,
    HeadUnavailableError,
    ProviderError,
    RateLimitError,
    RPCUnreachableError,
    SubscriptionFailedError,
)
from chainwake.core.tx_hash import validate_tx_hash
from chainwake.providers.base import (
    BlockRef,
    ObservableValue,
    ProviderConfig,
    TxFinalityStatus,
)
from chainwake.providers.market import MarketPriceFeed

EvmAlias = Literal["eth", "base", "bsc"]
EvmFinalityLevel = Literal["included", "safe", "finalized"]


class EvmFeeModel(StrEnum):
    """Fee signal exposed by a chain profile."""

    EIP1559 = "eip1559"
    OP_STACK = "op_stack"
    GAS_PRICE = "gas_price"


class EvmSubscription(StrEnum):
    """Standard EVM subscription capabilities used by Chainwake."""

    NEW_HEADS = "newHeads"
    LOGS = "logs"


@dataclass(frozen=True, slots=True)
class EvmChainProfile:
    """Static identity and runtime capabilities for one EVM chain."""

    alias: EvmAlias
    name: str
    chain_id: int
    default_rpc: str
    coingecko_platform: str
    block_seconds: float
    supported_finality_levels: tuple[EvmFinalityLevel, ...]
    fee_model: EvmFeeModel
    subscription_capabilities: frozenset[EvmSubscription]

    def __post_init__(self) -> None:
        if self.chain_id <= 0:
            raise ValueError("chain_id must be positive")
        if self.block_seconds <= 0:
            raise ValueError("block_seconds must be positive")
        if not self.default_rpc.startswith(("ws://", "wss://")):
            raise ValueError("default_rpc must be a WebSocket endpoint")
        if not self.coingecko_platform:
            raise ValueError("coingecko_platform must be non-empty")
        if "included" not in self.supported_finality_levels:
            raise ValueError("EVM profiles must support included finality")


ETHEREUM_PROFILE: Final[EvmChainProfile] = EvmChainProfile(
    alias="eth",
    name="Ethereum",
    chain_id=1,
    default_rpc="wss://ethereum-rpc.publicnode.com",
    coingecko_platform="ethereum",
    block_seconds=12.0,
    supported_finality_levels=("included", "safe", "finalized"),
    fee_model=EvmFeeModel.EIP1559,
    subscription_capabilities=frozenset({EvmSubscription.NEW_HEADS, EvmSubscription.LOGS}),
)
BASE_PROFILE: Final[EvmChainProfile] = EvmChainProfile(
    alias="base",
    name="Base",
    chain_id=8453,
    default_rpc="wss://base-rpc.publicnode.com",
    coingecko_platform="base",
    block_seconds=2.0,
    supported_finality_levels=("included", "safe", "finalized"),
    fee_model=EvmFeeModel.OP_STACK,
    subscription_capabilities=frozenset({EvmSubscription.NEW_HEADS, EvmSubscription.LOGS}),
)
BSC_PROFILE: Final[EvmChainProfile] = EvmChainProfile(
    alias="bsc",
    name="BSC",
    chain_id=56,
    default_rpc="wss://bsc-rpc.publicnode.com",
    coingecko_platform="binance-smart-chain",
    block_seconds=0.45,
    supported_finality_levels=("included", "finalized"),
    fee_model=EvmFeeModel.GAS_PRICE,
    subscription_capabilities=frozenset({EvmSubscription.NEW_HEADS, EvmSubscription.LOGS}),
)
EVM_PROFILES: Final[dict[EvmAlias, EvmChainProfile]] = {
    profile.alias: profile for profile in (ETHEREUM_PROFILE, BASE_PROFILE, BSC_PROFILE)
}


def profile_for(alias: EvmAlias) -> EvmChainProfile:
    """Return the immutable profile for one public EVM alias."""

    return EVM_PROFILES[alias]


DEFAULT_RPC_URL: Final[str] = ETHEREUM_PROFILE.default_rpc
WEI_PER_GWEI: Final[int] = 1_000_000_000
_BASE_GAS_PRICE_ORACLE: Final[str] = "0x420000000000000000000000000000000000000F"
_L1_BASE_FEE_SELECTOR: Final[str] = "0x519b4bd3"
_L1_BLOB_BASE_FEE_SELECTOR: Final[str] = "0xf8206140"
_PING_INTERVAL_SECONDS: Final[float] = 20.0
_PING_TIMEOUT_SECONDS: Final[float] = 20.0
_SUBSCRIPTION_READ_COST: Final[int] = 1
_RPC_AUTH_FAILED: Final[int] = -32021
_RPC_RATE_LIMITED: Final[int] = -32029
_RPC_CU_EXHAUSTED: Final[int] = -32030
_RPC_METHOD_NOT_FOUND: Final[int] = -32601
_RPC_METHOD_NOT_SUPPORTED: Final[int] = -32004
_HTTP_UNAUTHORIZED: Final[frozenset[int]] = frozenset({401, 403})
_HTTP_TOO_MANY_REQUESTS: Final[int] = 429
_TOKEN_PRICE_PATH_PARTS: Final[int] = 3


def _provider_error(
    error: object,
    *,
    subscription: bool = False,
) -> ProviderError | NotImplementedError:
    """Map a JSON-RPC error object onto Chainwake's provider taxonomy."""
    if not isinstance(error, dict):
        return DecodeError(f"malformed JSON-RPC error: {error!r}")
    code = error.get("code")
    message = str(error.get("message") or "EVM JSON-RPC request failed")
    if subscription and code in {_RPC_METHOD_NOT_FOUND, _RPC_METHOD_NOT_SUPPORTED}:
        return NotImplementedError(f"eth_subscribe is unavailable: {message}")
    error_type = {
        _RPC_AUTH_FAILED: AuthError,
        _RPC_RATE_LIMITED: RateLimitError,
        _RPC_CU_EXHAUSTED: CUExhaustedError,
    }.get(code)
    if error_type is not None:
        return error_type(message)
    if subscription:
        return SubscriptionFailedError(message)
    return RPCUnreachableError(message)


def _decode_message(raw: object) -> dict[str, object]:
    """Decode one JSON-RPC frame as an object."""
    try:
        value = json.loads(cast(str | bytes | bytearray, raw))
    except (TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DecodeError(f"invalid EVM JSON-RPC response: {exc}") from exc
    if not isinstance(value, dict):
        raise DecodeError("EVM JSON-RPC response must be an object")
    return cast(dict[str, object], value)


def _transport_error(
    exc: BaseException,
    *,
    subscription: bool = False,
) -> ProviderError:
    """Map WebSocket and network failures without leaking transport classes."""
    if isinstance(exc, websockets.exceptions.InvalidStatus):
        status = exc.response.status_code
        if status in _HTTP_UNAUTHORIZED:
            return AuthError(f"EVM RPC rejected authentication (HTTP {status})")
        if status == _HTTP_TOO_MANY_REQUESTS:
            return RateLimitError(f"EVM RPC rate limited the request (HTTP {status})")
    message = f"EVM {'subscription' if subscription else 'RPC connection'} failed: {exc}"
    if subscription:
        return SubscriptionFailedError(message)
    return RPCUnreachableError(message)


def _hex_int(value: object, field: str) -> int:
    """Decode an Ethereum quantity and name malformed fields precisely."""
    if not isinstance(value, str) or not value.startswith("0x"):
        raise DecodeError(f"{field} must be a 0x-prefixed Ethereum quantity")
    try:
        return int(value, 16)
    except ValueError as exc:
        raise DecodeError(f"{field} is not valid hexadecimal") from exc


@dataclass(frozen=True, slots=True)
class _DecodedBlock:
    number: int
    hash: str
    timestamp: datetime


class EvmProvider:
    """Standard WebSocket JSON-RPC backend configured by an EVM profile."""

    def __init__(
        self,
        profile: EvmChainProfile,
        *,
        market_prices: MarketPriceFeed | None = None,
    ) -> None:
        self.profile = profile
        self.name: str = profile.name.lower()
        self.short_alias: str = profile.alias
        self._socket: Any | None = None
        self._config: ProviderConfig | None = None
        self._request_id = 0
        self._request_lock = asyncio.Lock()
        self._market_prices = market_prices or MarketPriceFeed()

    @property
    def _connected(self) -> Any:
        if self._socket is None:
            raise RuntimeError("EvmProvider.connect() not called")
        return self._socket

    def _additional_headers(self) -> dict[str, str] | None:
        config = self._config
        if config is None:
            raise RuntimeError("EvmProvider.connect() not called")
        if config.api_key is None:
            return None
        return {"Authorization": f"Bearer {config.api_key}"}

    async def connect(self, config: ProviderConfig) -> None:
        """Open and validate a primary request/response connection."""
        if not config.rpc_url:
            raise ValueError("rpc_url required")
        self._config = config
        try:
            await self._open_primary()
            raw_chain_id = await self._rpc("eth_chainId", [])
            chain_id = _hex_int(raw_chain_id, "eth_chainId")
            if chain_id != self.profile.chain_id:
                raise RPCUnreachableError(
                    f"{self.profile.name} RPC returned chain ID {chain_id}; "
                    f"expected {self.profile.chain_id}"
                )
        except ProviderError:
            await self.disconnect()
            raise
        except (OSError, TimeoutError, websockets.exceptions.WebSocketException) as exc:
            await self.disconnect()
            raise _transport_error(exc) from exc

    async def disconnect(self) -> None:
        await self._drop_primary()
        await self._market_prices.close()
        self._config = None

    async def _open_primary(self) -> None:
        """Open a fresh serialized request socket from the retained config."""
        config = self._config
        if config is None:
            raise RuntimeError("EvmProvider.connect() not called")
        self._socket = await websockets.connect(
            config.rpc_url,
            open_timeout=config.timeout_seconds,
            ping_interval=_PING_INTERVAL_SECONDS,
            ping_timeout=_PING_TIMEOUT_SECONDS,
            additional_headers=self._additional_headers(),
        )

    async def _drop_primary(self) -> None:
        """Forget and best-effort close a failed request socket."""
        socket = self._socket
        self._socket = None
        if socket is not None:
            with contextlib.suppress(Exception):
                await socket.close()

    async def _rpc(self, method: str, params: list[object]) -> object:
        """Issue one serialized JSON-RPC call on the primary connection."""
        async with self._request_lock:
            if self._socket is None:
                try:
                    await self._open_primary()
                except (OSError, TimeoutError, websockets.exceptions.WebSocketException) as exc:
                    raise _transport_error(exc) from exc
            self._request_id += 1
            request_id = self._request_id
            request = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
            try:
                await self._connected.send(json.dumps(request, separators=(",", ":")))
                config = self._config
                if config is None:
                    raise RuntimeError("EvmProvider.connect() not called")
                async with asyncio.timeout(config.timeout_seconds):
                    response = _decode_message(await self._connected.recv())
            except ProviderError:
                raise
            except (OSError, TimeoutError, websockets.exceptions.WebSocketException) as exc:
                await self._drop_primary()
                raise _transport_error(exc) from exc
            if response.get("id") != request_id:
                raise DecodeError(
                    f"EVM RPC {method} returned response id {response.get('id')!r}, "
                    f"expected {request_id}"
                )
            if "error" in response:
                raise _provider_error(response["error"])
            if "result" not in response:
                raise DecodeError(f"EVM RPC {method} response has no result")
            return response["result"]

    async def read_observable(
        self,
        path: str,
        args: dict[str, object],
        at_block: BlockRef | None = None,
    ) -> ObservableValue:
        """Read a profile-supported EVM observable at latest or an exact block."""
        if path.startswith("tx."):
            return await self._read_transaction(path, args, at_block=at_block)
        if path.startswith("token.") and path.endswith(".price"):
            return await self._read_token_price(path, at_block)
        if path == "network.base-fee" and self.profile.fee_model in {
            EvmFeeModel.EIP1559,
            EvmFeeModel.OP_STACK,
        }:
            return await self._read_base_fee(path, at_block)
        if path == "network.gas-price" and self.profile.fee_model == EvmFeeModel.GAS_PRICE:
            return await self._read_gas_price(path, at_block)
        if path == "network.l1-base-fee" and self.profile.fee_model == EvmFeeModel.OP_STACK:
            return await self._read_l1_oracle_fee(
                path,
                at_block,
                selector=_L1_BASE_FEE_SELECTOR,
                field="l1_base_fee_wei",
                label="GasPriceOracle.l1BaseFee",
            )
        if path == "network.l1-blob-base-fee" and self.profile.fee_model == EvmFeeModel.OP_STACK:
            return await self._read_l1_oracle_fee(
                path,
                at_block,
                selector=_L1_BLOB_BASE_FEE_SELECTOR,
                field="l1_blob_base_fee_wei",
                label="GasPriceOracle.blobBaseFee",
            )
        raise NotImplementedError(f"unknown {self.profile.name} observable {path!r}")

    async def _read_token_price(
        self,
        path: str,
        at_block: BlockRef | None,
    ) -> ObservableValue:
        parts = path.split(".")
        if len(parts) != _TOKEN_PRICE_PATH_PARTS or not parts[1]:
            raise DecodeError(f"invalid token price path {path!r}")
        identifier = parts[1]
        market = await self._market_prices.token_usd(
            platform=self.profile.coingecko_platform,
            chain_id=self.profile.chain_id,
            identifier=identifier,
        )
        head = await self._read_block_ref(at_block)
        return ObservableValue(
            path=path,
            value=market.value,
            block=head.number,
            block_hash=head.hash,
            timestamp=head.timestamp,
            meta=market.meta,
        )

    async def _read_base_fee(
        self,
        path: str,
        at_block: BlockRef | None,
    ) -> ObservableValue:
        if at_block is not None and at_block.hash is not None:
            result = await self._rpc("eth_getBlockByHash", [at_block.hash, False])
        elif at_block is not None and at_block.number is not None:
            result = await self._rpc("eth_getBlockByNumber", [hex(at_block.number), False])
        else:
            result = await self._rpc("eth_getBlockByNumber", ["latest", False])
        if result is None and at_block is not None:
            raise HeadUnavailableError(
                f"notified EVM head {at_block.hash or at_block.number!r} is unavailable"
            )
        return self._decode_base_fee_block(path, result)

    async def _read_gas_price(
        self,
        path: str,
        at_block: BlockRef | None,
    ) -> ObservableValue:
        gas_price_wei = _hex_int(await self._rpc("eth_gasPrice", []), "eth_gasPrice")
        head = await self._read_block_ref(at_block)
        return ObservableValue(
            path=path,
            value=gas_price_wei / WEI_PER_GWEI,
            block=head.number,
            block_hash=head.hash,
            timestamp=head.timestamp,
            meta={"gas_price_wei": gas_price_wei, "unit": "gwei"},
        )

    async def _read_l1_oracle_fee(
        self,
        path: str,
        at_block: BlockRef | None,
        *,
        selector: str,
        field: str,
        label: str,
    ) -> ObservableValue:
        head = await self._read_block_ref(at_block)
        raw_fee = await self._rpc(
            "eth_call",
            [
                {"to": _BASE_GAS_PRICE_ORACLE, "data": selector},
                {"blockHash": head.hash, "requireCanonical": True},
            ],
        )
        fee_wei = _hex_int(raw_fee, label)
        return ObservableValue(
            path=path,
            value=fee_wei / WEI_PER_GWEI,
            block=head.number,
            block_hash=head.hash,
            timestamp=head.timestamp,
            meta={field: fee_wei, "unit": "gwei"},
        )

    async def _read_transaction(
        self,
        path: str,
        args: dict[str, object],
        *,
        at_block: BlockRef | None,
    ) -> ObservableValue:
        """Read a receipt against canonical chain state.

        A missing receipt and a receipt whose inclusion hash no longer matches
        the canonical block are both reported as pending. Ethereum JSON-RPC
        cannot reliably distinguish those states from dropped/replaced
        transactions, so this provider deliberately makes no such claim.
        """
        tx_hash = validate_tx_hash(path.removeprefix("tx."))
        requested_finality = args.get("finality", "included")
        if requested_finality not in self.profile.supported_finality_levels:
            raise NotImplementedError(
                f"{self.profile.name} does not support {requested_finality!r} finality"
            )
        receipt_result = await self._rpc("eth_getTransactionReceipt", [tx_hash])
        if receipt_result is None:
            head = await self._read_block_ref(at_block)
            return self._pending_transaction(path, tx_hash, head)
        if not isinstance(receipt_result, dict):
            raise DecodeError("eth_getTransactionReceipt must return an object or null")
        receipt = cast(dict[str, object], receipt_result)
        returned_hash = receipt.get("transactionHash")
        if not isinstance(returned_hash, str) or returned_hash.lower() != tx_hash.lower():
            raise DecodeError("transactionHash in receipt does not match the requested hash")

        inclusion_number = _hex_int(receipt.get("blockNumber"), "receipt.blockNumber")
        inclusion_hash = receipt.get("blockHash")
        if not isinstance(inclusion_hash, str) or not inclusion_hash.startswith("0x"):
            raise DecodeError("receipt.blockHash must be a 0x-prefixed Ethereum block hash")
        canonical_result = await self._rpc(
            "eth_getBlockByNumber",
            [hex(inclusion_number), False],
        )
        canonical = self._decode_block_ref(canonical_result, "canonical inclusion block")
        if canonical.hash.lower() != inclusion_hash.lower():
            head_number = (
                at_block.number
                if at_block is not None and at_block.number is not None
                else canonical.number
            )
            head = _DecodedBlock(
                number=head_number,
                hash=(
                    at_block.hash
                    if at_block is not None and at_block.hash is not None
                    else canonical.hash
                ),
                timestamp=canonical.timestamp,
            )
            return self._pending_transaction(path, tx_hash, head)

        head = await self._read_block_ref(at_block)
        head_number = head.number
        confirmations = head_number - inclusion_number + 1
        if confirmations < 1:
            return self._pending_transaction(path, tx_hash, canonical)

        status_quantity = _hex_int(receipt.get("status"), "receipt.status")
        if status_quantity not in {0, 1}:
            raise DecodeError("receipt.status must be 0x0 or 0x1")
        level: Literal["included", "safe", "finalized"] = "included"
        if requested_finality in {"safe", "finalized"}:
            finality_result = await self._rpc(
                "eth_getBlockByNumber",
                [requested_finality, False],
            )
            finality_head = self._decode_block_ref(
                finality_result,
                f"{requested_finality} block",
            )
            if finality_head.number >= inclusion_number:
                level = cast("Literal['safe', 'finalized']", requested_finality)

        status = TxFinalityStatus(
            tx_hash=tx_hash,
            level=level,
            block=inclusion_number,
            block_hash=inclusion_hash,
            timestamp=canonical.timestamp,
            confirmations=confirmations,
            execution_status="success" if status_quantity == 1 else "reverted",
            gas_used=_hex_int(receipt.get("gasUsed"), "receipt.gasUsed"),
            effective_gas_price_wei=_hex_int(
                receipt.get("effectiveGasPrice"),
                "receipt.effectiveGasPrice",
            ),
        )
        return ObservableValue(
            path=path,
            value=status,
            block=inclusion_number,
            block_hash=inclusion_hash,
            timestamp=canonical.timestamp,
        )

    async def _read_block_ref(self, at_block: BlockRef | None) -> _DecodedBlock:
        if at_block is not None and at_block.number is not None:
            result = await self._rpc("eth_getBlockByNumber", [hex(at_block.number), False])
        elif at_block is not None and at_block.hash is not None:
            result = await self._rpc("eth_getBlockByHash", [at_block.hash, False])
        else:
            result = await self._rpc("eth_getBlockByNumber", ["latest", False])
        if result is None and at_block is not None:
            raise HeadUnavailableError(
                f"notified EVM head {at_block.hash or at_block.number!r} is unavailable"
            )
        head = self._decode_block_ref(result, "head block")
        if (
            at_block is not None
            and at_block.hash is not None
            and head.hash.lower() != at_block.hash.lower()
        ):
            raise HeadUnavailableError(
                f"notified Ethereum head {at_block.hash!r} is no longer canonical"
            )
        return head

    @staticmethod
    def _decode_block_ref(result: object, label: str) -> _DecodedBlock:
        if not isinstance(result, dict):
            raise DecodeError(f"{label} is unavailable")
        block = cast(dict[str, object], result)
        block_hash = block.get("hash")
        if not isinstance(block_hash, str) or not block_hash.startswith("0x"):
            raise DecodeError(f"{label}.hash must be a 0x-prefixed Ethereum block hash")
        return _DecodedBlock(
            number=_hex_int(block.get("number"), f"{label}.number"),
            hash=block_hash,
            timestamp=datetime.fromtimestamp(
                _hex_int(block.get("timestamp"), f"{label}.timestamp"),
                tz=UTC,
            ),
        )

    @staticmethod
    def _pending_transaction(
        path: str,
        tx_hash: str,
        head: _DecodedBlock,
    ) -> ObservableValue:
        return ObservableValue(
            path=path,
            value=TxFinalityStatus(tx_hash=tx_hash, level="pending"),
            block=head.number,
            block_hash=head.hash,
            timestamp=head.timestamp,
        )

    @staticmethod
    def _decode_base_fee_block(path: str, result: object) -> ObservableValue:
        if not isinstance(result, dict):
            raise DecodeError("eth_getBlock returned no block object")
        block = cast(dict[str, object], result)
        number = _hex_int(block.get("number"), "number")
        timestamp = _hex_int(block.get("timestamp"), "timestamp")
        base_fee_wei = _hex_int(block.get("baseFeePerGas"), "baseFeePerGas")
        block_hash = block.get("hash")
        if not isinstance(block_hash, str) or not block_hash.startswith("0x"):
            raise DecodeError("hash must be a 0x-prefixed Ethereum block hash")
        return ObservableValue(
            path=path,
            value=base_fee_wei / WEI_PER_GWEI,
            block=number,
            block_hash=block_hash,
            timestamp=datetime.fromtimestamp(timestamp, tz=UTC),
            meta={
                "base_fee_wei": base_fee_wei,
                "unit": "gwei",
            },
        )

    def subscribe_heads(
        self,
        *,
        charge_rpc: Callable[[int], None] | None = None,
    ) -> AsyncIterator[BlockRef]:
        """Subscribe to canonical best-head notifications on a dedicated socket."""
        return self._subscribe_heads(charge_rpc=charge_rpc)

    def _require_head_subscription(self) -> None:
        if EvmSubscription.NEW_HEADS not in self.profile.subscription_capabilities:
            raise NotImplementedError(
                f"{self.profile.name} profile does not support newHeads subscriptions"
            )

    async def _subscribe_heads(
        self,
        *,
        charge_rpc: Callable[[int], None] | None = None,
    ) -> AsyncIterator[BlockRef]:
        config = self._config
        if config is None:
            raise RuntimeError("EvmProvider.connect() not called")
        self._require_head_subscription()
        try:
            async with websockets.connect(
                config.rpc_url,
                open_timeout=config.timeout_seconds,
                ping_interval=_PING_INTERVAL_SECONDS,
                ping_timeout=_PING_TIMEOUT_SECONDS,
                additional_headers=self._additional_headers(),
            ) as socket:
                if charge_rpc is not None:
                    charge_rpc(_SUBSCRIPTION_READ_COST)
                self._request_id += 1
                request_id = self._request_id
                async with asyncio.timeout(config.timeout_seconds):
                    await socket.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": request_id,
                                "method": "eth_subscribe",
                                "params": ["newHeads"],
                            },
                            separators=(",", ":"),
                        )
                    )
                    response = _decode_message(await socket.recv())
                if "error" in response:
                    raise _provider_error(response["error"], subscription=True)
                subscription_id = response.get("result")
                if response.get("id") != request_id or not isinstance(subscription_id, str):
                    raise SubscriptionFailedError(
                        "eth_subscribe returned an invalid subscription acknowledgement"
                    )
                while True:
                    notification = _decode_message(await socket.recv())
                    if notification.get("method") != "eth_subscription":
                        continue
                    params = notification.get("params")
                    if not isinstance(params, dict):
                        raise DecodeError("eth_subscription params must be an object")
                    if params.get("subscription") != subscription_id:
                        continue
                    head = params.get("result")
                    if not isinstance(head, dict):
                        raise DecodeError("newHeads notification result must be an object")
                    block_hash = head.get("hash")
                    if not isinstance(block_hash, str) or not block_hash.startswith("0x"):
                        raise DecodeError("newHeads.hash must be a 0x-prefixed block hash")
                    yield BlockRef(
                        number=_hex_int(head.get("number"), "newHeads.number"),
                        hash=block_hash,
                    )
        except ProviderError:
            raise
        except (OSError, TimeoutError, websockets.exceptions.WebSocketException) as exc:
            raise _transport_error(exc, subscription=True) from exc

    async def get_block_finality(self, tx_hash: str) -> TxFinalityStatus:
        """Return current canonical receipt/finality state for one transaction."""
        validated = validate_tx_hash(tx_hash)
        observed = await self.read_observable(
            f"tx.{validated}",
            {"finality": "finalized"},
        )
        return cast(TxFinalityStatus, observed.value)


__all__ = [
    "BASE_PROFILE",
    "BSC_PROFILE",
    "DEFAULT_RPC_URL",
    "ETHEREUM_PROFILE",
    "EVM_PROFILES",
    "WEI_PER_GWEI",
    "EvmChainProfile",
    "EvmFeeModel",
    "EvmProvider",
    "EvmSubscription",
    "profile_for",
]
