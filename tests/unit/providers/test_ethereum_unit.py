"""Unit contract for the Ethereum JSON-RPC provider."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from chainwake.chains import ChainRuntimeConfig
from chainwake.core.budget import Budget
from chainwake.core.errors import (
    AuthError,
    DecodeError,
    HeadUnavailableError,
    RateLimitError,
    RPCUnreachableError,
    SubscriptionFailedError,
)
from chainwake.core.primitives.threshold import ThresholdPrimitive
from chainwake.core.registry import lookup
from chainwake.core.runtime import WatcherRunner, WatcherSpec
from chainwake.output.adapters import DefaultAdapter
from chainwake.output.schema import ThresholdCondition
from chainwake.providers.base import BlockRef, ProviderConfig, TxFinalityStatus
from chainwake.providers.evm import ETHEREUM_PROFILE, EvmProvider

pytestmark = pytest.mark.unit


_BLOCK = {
    "number": "0x2a",
    "hash": "0xabc123",
    "timestamp": "0x65ec8780",
    "baseFeePerGas": "0x59682f00",  # 1_500_000_000 wei
}
_TX_HASH = f"0x{'12' * 32}"
_INCLUSION_HASH = f"0x{'34' * 32}"
_HEAD_HASH = f"0x{'56' * 32}"
_RECEIPT = {
    "transactionHash": _TX_HASH,
    "blockNumber": "0x64",
    "blockHash": _INCLUSION_HASH,
    "status": "0x1",
    "gasUsed": "0x5208",
    "effectiveGasPrice": "0x59682f00",
}
_INCLUSION_BLOCK = {
    "number": "0x64",
    "hash": _INCLUSION_HASH,
    "timestamp": "0x65ec8780",
    "baseFeePerGas": "0x59682f00",
}
_HEAD_BLOCK = {
    "number": "0x65",
    "hash": _HEAD_HASH,
    "timestamp": "0x65ec878c",
    "baseFeePerGas": "0x59682f00",
}


RpcResult = dict[str, object] | Callable[[dict[str, object]], dict[str, object]]


class _FakeWebSocket:
    """Small websockets-compatible socket with request-aware responses."""

    def __init__(
        self,
        responses: dict[str, RpcResult],
        *,
        notifications: list[dict[str, object]] | None = None,
        recv_error_after: int | None = None,
    ) -> None:
        self.responses = responses
        self.notifications = deque(notifications or [])
        self.recv_error_after = recv_error_after
        self.recv_count = 0
        self.sent: list[dict[str, object]] = []
        self._replies: deque[dict[str, object]] = deque()
        self.closed = False

    def __await__(self) -> Any:
        async def _return_self() -> _FakeWebSocket:
            return self

        return _return_self().__await__()

    async def __aenter__(self) -> _FakeWebSocket:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: object,
    ) -> None:
        await self.close()

    async def send(self, message: str) -> None:
        request = cast(dict[str, object], json.loads(message))
        self.sent.append(request)
        method = cast(str, request["method"])
        scripted = self.responses[method]
        response = (
            cast(dict[str, object], scripted) if isinstance(scripted, dict) else scripted(request)
        )
        reply: dict[str, object] = {"jsonrpc": "2.0", "id": request["id"]}
        reply.update(response)
        self._replies.append(reply)

    async def recv(self) -> str:
        if self.recv_error_after is not None and self.recv_count >= self.recv_error_after:
            self.recv_error_after = None
            raise ConnectionResetError("socket dropped")
        self.recv_count += 1
        if self._replies:
            return json.dumps(self._replies.popleft())
        if self.notifications:
            return json.dumps(self.notifications.popleft())
        raise AssertionError("fake websocket has no queued response")

    async def close(self) -> None:
        self.closed = True


class _FakeConnector:
    def __init__(self, *sockets: _FakeWebSocket) -> None:
        self.sockets = deque(sockets)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, url: str, **kwargs: object) -> _FakeWebSocket:
        self.calls.append((url, kwargs))
        return self.sockets.popleft()


class _StalledWebSocket(_FakeWebSocket):
    """Accept a subscription request but never acknowledge it."""

    async def send(self, message: str) -> None:
        self.sent.append(cast(dict[str, object], json.loads(message)))

    async def recv(self) -> str:
        await asyncio.Future()
        raise AssertionError("unreachable")


def _chain_socket(*, block_response: RpcResult | None = None) -> _FakeWebSocket:
    responses: dict[str, RpcResult] = {"eth_chainId": {"result": "0x1"}}
    if block_response is not None:
        responses["eth_getBlockByNumber"] = block_response
        responses["eth_getBlockByHash"] = block_response
    return _FakeWebSocket(responses)


@pytest.mark.asyncio
async def test_connect_validates_endpoint_with_eth_chain_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket = _chain_socket()
    connector = _FakeConnector(socket)
    monkeypatch.setattr("chainwake.providers.evm.websockets.connect", connector)

    provider = EvmProvider(ETHEREUM_PROFILE)
    await provider.connect(ProviderConfig(rpc_url="ws://ethereum.test"))

    assert provider.name == "ethereum"
    assert provider.short_alias == "eth"
    assert connector.calls[0][0] == "ws://ethereum.test"
    assert [request["method"] for request in socket.sent] == ["eth_chainId"]
    await provider.disconnect()
    assert socket.closed


@pytest.mark.asyncio
async def test_read_base_fee_at_latest_block_converts_wei_to_gwei(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket = _chain_socket(block_response={"result": _BLOCK})
    monkeypatch.setattr(
        "chainwake.providers.evm.websockets.connect",
        _FakeConnector(socket),
    )
    provider = EvmProvider(ETHEREUM_PROFILE)
    await provider.connect(ProviderConfig(rpc_url="ws://ethereum.test"))

    observed = await provider.read_observable("network.base-fee", {})

    request = socket.sent[-1]
    assert request["method"] == "eth_getBlockByNumber"
    assert request["params"] == ["latest", False]
    assert observed.path == "network.base-fee"
    assert observed.value == pytest.approx(1.5)
    assert observed.block == 42
    assert observed.block_hash == "0xabc123"
    assert observed.timestamp == datetime.fromtimestamp(int("65ec8780", 16), tz=UTC)


@pytest.mark.asyncio
async def test_read_base_fee_at_pinned_hash_uses_exact_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket = _chain_socket(block_response={"result": _BLOCK})
    monkeypatch.setattr(
        "chainwake.providers.evm.websockets.connect",
        _FakeConnector(socket),
    )
    provider = EvmProvider(ETHEREUM_PROFILE)
    await provider.connect(ProviderConfig(rpc_url="ws://ethereum.test"))

    observed = await provider.read_observable(
        "network.base-fee",
        {},
        at_block=BlockRef(number=42, hash="0xabc123"),
    )

    request = socket.sent[-1]
    assert request["method"] == "eth_getBlockByHash"
    assert request["params"] == ["0xabc123", False]
    assert observed.block == 42
    assert observed.block_hash == "0xabc123"
    assert observed.value == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_new_heads_subscription_uses_keepalive_and_yields_block_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _chain_socket()
    subscription = _FakeWebSocket(
        {
            "eth_subscribe": {"result": "0xsubscription"},
            "eth_unsubscribe": {"result": True},
        },
        notifications=[
            {
                "jsonrpc": "2.0",
                "method": "eth_subscription",
                "params": {
                    "subscription": "0xsubscription",
                    "result": {"number": "0x2b", "hash": "0xdef456"},
                },
            }
        ],
    )
    connector = _FakeConnector(primary, subscription)
    monkeypatch.setattr("chainwake.providers.evm.websockets.connect", connector)
    provider = EvmProvider(ETHEREUM_PROFILE)
    await provider.connect(ProviderConfig(rpc_url="ws://ethereum.test"))
    charges: list[int] = []

    stream = cast(
        AsyncGenerator[BlockRef],
        provider.subscribe_heads(charge_rpc=charges.append),
    )
    block = await anext(stream)

    assert block == BlockRef(number=43, hash="0xdef456")
    assert subscription.sent[0]["method"] == "eth_subscribe"
    assert subscription.sent[0]["params"] == ["newHeads"]
    assert charges == [1]
    subscription_options = connector.calls[1][1]
    assert float(cast(int | float, subscription_options["ping_interval"])) > 0
    assert float(cast(int | float, subscription_options["ping_timeout"])) > 0

    await stream.aclose()
    assert subscription.closed


@pytest.mark.asyncio
async def test_new_heads_method_not_found_requests_timer_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _chain_socket()
    subscription = _FakeWebSocket(
        {
            "eth_subscribe": {
                "error": {"code": -32601, "message": "Method not found"},
            }
        }
    )
    monkeypatch.setattr(
        "chainwake.providers.evm.websockets.connect",
        _FakeConnector(primary, subscription),
    )
    provider = EvmProvider(ETHEREUM_PROFILE)
    await provider.connect(ProviderConfig(rpc_url="ws://ethereum.test"))

    with pytest.raises(NotImplementedError, match="eth_subscribe"):
        await anext(provider.subscribe_heads())

    assert subscription.closed


@pytest.mark.asyncio
async def test_runtime_polls_after_new_heads_method_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    block_reads = 0

    def next_block(_request: dict[str, object]) -> dict[str, object]:
        nonlocal block_reads
        block_reads += 1
        base_fee = 1_500_000_000 if block_reads == 1 else 3_000_000_000
        return {
            "result": {
                **_BLOCK,
                "number": hex(41 + block_reads),
                "hash": f"0x{block_reads:064x}",
                "baseFeePerGas": hex(base_fee),
            }
        }

    primary = _chain_socket(block_response=next_block)
    subscription = _FakeWebSocket(
        {
            "eth_subscribe": {
                "error": {"code": -32601, "message": "Method not found"},
            }
        }
    )
    monkeypatch.setattr(
        "chainwake.providers.evm.websockets.connect",
        _FakeConnector(primary, subscription),
    )
    provider = EvmProvider(ETHEREUM_PROFILE)
    await provider.connect(ProviderConfig(rpc_url="ws://ethereum.test"))
    spec = WatcherSpec(
        chain="eth",
        resource="network",
        path_params={},
        sub_resource="base-fee",
        primitive_name="threshold",
        condition=ThresholdCondition(operator="above", target=2),
        invocation=["chainwake", "eth", "network", "base-fee", "--above", "2"],
        max_runtime_seconds=1,
    )
    runner = WatcherRunner(
        spec,
        entry=lookup("network.base-fee", chain="eth"),
        provider=provider,
        primitive=ThresholdPrimitive(operator="above", target=2),
        adapters=[DefaultAdapter()],
        budget=Budget(max_runtime_seconds=1),
        runtime=ChainRuntimeConfig(
            block_seconds=0.001,
            epoch_state_read_cost=0,
            event_block_read_cost=1,
            event_legacy_block_read_cost=1,
        ),
    )

    try:
        exit_code = await runner.run()
    finally:
        await provider.disconnect()

    assert exit_code == 0
    assert block_reads == 2


@pytest.mark.asyncio
async def test_runtime_skips_unavailable_reorged_head_and_reads_next_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pinned_reads = 0

    def pinned_block(request: dict[str, object]) -> dict[str, object]:
        nonlocal pinned_reads
        pinned_reads += 1
        if pinned_reads == 1:
            return {"result": None}
        [requested_hash, _full] = cast(list[object], request["params"])
        return {
            "result": {
                **_BLOCK,
                "number": "0x2c",
                "hash": requested_hash,
                "baseFeePerGas": hex(3_000_000_000),
            }
        }

    primary = _FakeWebSocket(
        {
            "eth_chainId": {"result": "0x1"},
            "eth_getBlockByNumber": {"result": _BLOCK},
            "eth_getBlockByHash": pinned_block,
        }
    )
    subscription = _FakeWebSocket(
        {"eth_subscribe": {"result": "0xsubscription"}},
        notifications=[
            {
                "jsonrpc": "2.0",
                "method": "eth_subscription",
                "params": {
                    "subscription": "0xsubscription",
                    "result": {"number": "0x2b", "hash": "0xaaa"},
                },
            },
            {
                "jsonrpc": "2.0",
                "method": "eth_subscription",
                "params": {
                    "subscription": "0xsubscription",
                    "result": {"number": "0x2c", "hash": "0xbbb"},
                },
            },
        ],
    )
    monkeypatch.setattr(
        "chainwake.providers.evm.websockets.connect",
        _FakeConnector(primary, subscription),
    )
    provider = EvmProvider(ETHEREUM_PROFILE)
    await provider.connect(ProviderConfig(rpc_url="ws://ethereum.test"))
    spec = WatcherSpec(
        chain="eth",
        resource="network",
        path_params={},
        sub_resource="base-fee",
        primitive_name="threshold",
        condition=ThresholdCondition(operator="above", target=2),
        invocation=["chainwake", "eth", "network", "base-fee", "--above", "2"],
        max_runtime_seconds=1,
    )
    runner = WatcherRunner(
        spec,
        entry=lookup("network.base-fee", chain="eth"),
        provider=provider,
        primitive=ThresholdPrimitive(operator="above", target=2),
        adapters=[DefaultAdapter()],
        budget=Budget(max_runtime_seconds=1),
    )

    try:
        exit_code = await runner.run()
    finally:
        await provider.disconnect()

    assert exit_code == 0
    assert pinned_reads == 2


@pytest.mark.asyncio
async def test_new_heads_acknowledgement_obeys_rpc_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _chain_socket()
    stalled = _StalledWebSocket({})
    monkeypatch.setattr(
        "chainwake.providers.evm.websockets.connect",
        _FakeConnector(primary, stalled),
    )
    provider = EvmProvider(ETHEREUM_PROFILE)
    await provider.connect(ProviderConfig(rpc_url="ws://ethereum.test", timeout_seconds=0.01))

    with pytest.raises(SubscriptionFailedError, match="failed"):
        await anext(provider.subscribe_heads())

    assert stalled.closed


@pytest.mark.asyncio
async def test_unknown_observable_is_rejected_without_block_rpc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket = _chain_socket()
    monkeypatch.setattr(
        "chainwake.providers.evm.websockets.connect",
        _FakeConnector(socket),
    )
    provider = EvmProvider(ETHEREUM_PROFILE)
    await provider.connect(ProviderConfig(rpc_url="ws://ethereum.test"))

    with pytest.raises(NotImplementedError, match=r"unknown|handler|observable"):
        await provider.read_observable("network.not-an-ethereum-observable", {})

    assert [request["method"] for request in socket.sent] == ["eth_chainId"]


@pytest.mark.asyncio
async def test_json_rpc_auth_error_is_classified_at_provider_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket = _FakeWebSocket(
        {
            "eth_chainId": {
                "error": {"code": -32021, "message": "invalid API key"},
            }
        }
    )
    monkeypatch.setattr(
        "chainwake.providers.evm.websockets.connect",
        _FakeConnector(socket),
    )

    with pytest.raises(AuthError, match="invalid API key"):
        await EvmProvider(ETHEREUM_PROFILE).connect(ProviderConfig(rpc_url="ws://ethereum.test"))


@pytest.mark.asyncio
async def test_json_rpc_rate_limit_is_classified_at_provider_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket = _chain_socket(
        block_response={
            "error": {"code": -32029, "message": "rate limit exceeded"},
        }
    )
    monkeypatch.setattr(
        "chainwake.providers.evm.websockets.connect",
        _FakeConnector(socket),
    )
    provider = EvmProvider(ETHEREUM_PROFILE)
    await provider.connect(ProviderConfig(rpc_url="ws://ethereum.test"))

    with pytest.raises(RateLimitError, match="rate limit exceeded"):
        await provider.read_observable("network.base-fee", {})


@pytest.mark.asyncio
async def test_malformed_base_fee_is_decode_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed_block = {**_BLOCK, "baseFeePerGas": "not-hex"}
    socket = _chain_socket(block_response={"result": malformed_block})
    monkeypatch.setattr(
        "chainwake.providers.evm.websockets.connect",
        _FakeConnector(socket),
    )
    provider = EvmProvider(ETHEREUM_PROFILE)
    await provider.connect(ProviderConfig(rpc_url="ws://ethereum.test"))

    with pytest.raises(DecodeError, match="baseFeePerGas"):
        await provider.read_observable("network.base-fee", {})


@pytest.mark.asyncio
async def test_next_read_reconnects_primary_socket_after_transport_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dropped = _FakeWebSocket(
        {
            "eth_chainId": {"result": "0x1"},
            "eth_getBlockByNumber": {"result": _BLOCK},
        },
        recv_error_after=1,
    )
    recovered = _chain_socket(block_response={"result": _BLOCK})
    connector = _FakeConnector(dropped, recovered)
    monkeypatch.setattr("chainwake.providers.evm.websockets.connect", connector)
    provider = EvmProvider(ETHEREUM_PROFILE)
    await provider.connect(ProviderConfig(rpc_url="ws://ethereum.test"))

    with pytest.raises(RPCUnreachableError, match="socket dropped"):
        await provider.read_observable("network.base-fee", {})

    observed = await provider.read_observable("network.base-fee", {})

    assert observed.value == pytest.approx(1.5)
    assert len(connector.calls) == 2
    assert dropped.closed


@pytest.mark.asyncio
async def test_read_transaction_reports_confirmations_and_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def block_response(request: dict[str, object]) -> dict[str, object]:
        [block, _full] = cast(list[object], request["params"])
        if block == "0x64":
            return {"result": _INCLUSION_BLOCK}
        if block == "finalized":
            return {"result": {**_INCLUSION_BLOCK, "number": "0x63"}}
        return {"result": _HEAD_BLOCK}

    socket = _FakeWebSocket(
        {
            "eth_chainId": {"result": "0x1"},
            "eth_getTransactionReceipt": {"result": _RECEIPT},
            "eth_getBlockByNumber": block_response,
        }
    )
    monkeypatch.setattr(
        "chainwake.providers.evm.websockets.connect",
        _FakeConnector(socket),
    )
    provider = EvmProvider(ETHEREUM_PROFILE)
    await provider.connect(ProviderConfig(rpc_url="ws://ethereum.test"))

    observed = await provider.read_observable(
        f"tx.{_TX_HASH}",
        {"finality": "included", "confirmations": 2},
        at_block=BlockRef(number=101, hash=_HEAD_HASH),
    )

    assert observed.block == 100
    assert observed.block_hash == _INCLUSION_HASH
    status = cast(TxFinalityStatus, observed.value)
    assert status.level == "included"
    assert status.confirmations == 2
    assert status.execution_status == "success"
    assert status.gas_used == 21_000
    assert status.effective_gas_price_wei == 1_500_000_000


@pytest.mark.asyncio
async def test_read_transaction_reports_reverted_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket = _FakeWebSocket(
        {
            "eth_chainId": {"result": "0x1"},
            "eth_getTransactionReceipt": {"result": {**_RECEIPT, "status": "0x0"}},
            "eth_getBlockByNumber": {"result": _INCLUSION_BLOCK},
        }
    )
    monkeypatch.setattr(
        "chainwake.providers.evm.websockets.connect",
        _FakeConnector(socket),
    )
    provider = EvmProvider(ETHEREUM_PROFILE)
    await provider.connect(ProviderConfig(rpc_url="ws://ethereum.test"))

    observed = await provider.read_observable(
        f"tx.{_TX_HASH}",
        {"finality": "included", "confirmations": 1},
        at_block=BlockRef(number=100, hash=_INCLUSION_HASH),
    )

    status = cast(TxFinalityStatus, observed.value)
    assert status.execution_status == "reverted"
    assert status.confirmations == 1


@pytest.mark.asyncio
async def test_missing_transaction_receipt_remains_pending_without_drop_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket = _FakeWebSocket(
        {
            "eth_chainId": {"result": "0x1"},
            "eth_getTransactionReceipt": {"result": None},
            "eth_getBlockByNumber": {"result": _HEAD_BLOCK},
        }
    )
    monkeypatch.setattr(
        "chainwake.providers.evm.websockets.connect",
        _FakeConnector(socket),
    )
    provider = EvmProvider(ETHEREUM_PROFILE)
    await provider.connect(ProviderConfig(rpc_url="ws://ethereum.test"))

    observed = await provider.read_observable(
        f"tx.{_TX_HASH}",
        {"finality": "included", "confirmations": 1},
        at_block=BlockRef(number=101, hash=_HEAD_HASH),
    )

    status = cast(TxFinalityStatus, observed.value)
    assert status.level == "pending"
    assert status.confirmations is None
    assert status.execution_status is None


@pytest.mark.asyncio
async def test_reorged_transaction_receipt_returns_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = {**_INCLUSION_BLOCK, "hash": f"0x{'99' * 32}"}
    socket = _FakeWebSocket(
        {
            "eth_chainId": {"result": "0x1"},
            "eth_getTransactionReceipt": {"result": _RECEIPT},
            "eth_getBlockByNumber": {"result": canonical},
        }
    )
    monkeypatch.setattr(
        "chainwake.providers.evm.websockets.connect",
        _FakeConnector(socket),
    )
    provider = EvmProvider(ETHEREUM_PROFILE)
    await provider.connect(ProviderConfig(rpc_url="ws://ethereum.test"))

    observed = await provider.read_observable(
        f"tx.{_TX_HASH}",
        {"finality": "included", "confirmations": 1},
        at_block=BlockRef(number=101, hash=_HEAD_HASH),
    )

    status = cast(TxFinalityStatus, observed.value)
    assert status.level == "pending"
    assert status.confirmations is None


@pytest.mark.asyncio
async def test_stale_notified_head_cannot_satisfy_confirmation_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacement_head = {
        **_HEAD_BLOCK,
        "number": "0x66",
        "hash": f"0x{'77' * 32}",
    }

    def block_response(request: dict[str, object]) -> dict[str, object]:
        [block, _full] = cast(list[object], request["params"])
        if block == "0x64":
            return {"result": _INCLUSION_BLOCK}
        return {"result": replacement_head}

    socket = _FakeWebSocket(
        {
            "eth_chainId": {"result": "0x1"},
            "eth_getTransactionReceipt": {"result": _RECEIPT},
            "eth_getBlockByNumber": block_response,
        }
    )
    monkeypatch.setattr(
        "chainwake.providers.evm.websockets.connect",
        _FakeConnector(socket),
    )
    provider = EvmProvider(ETHEREUM_PROFILE)
    await provider.connect(ProviderConfig(rpc_url="ws://ethereum.test"))

    with pytest.raises(HeadUnavailableError, match="no longer canonical"):
        await provider.read_observable(
            f"tx.{_TX_HASH}",
            {"finality": "included", "confirmations": 3},
            at_block=BlockRef(number=102, hash=f"0x{'88' * 32}"),
        )


@pytest.mark.asyncio
async def test_finalized_head_promotes_canonical_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def block_response(request: dict[str, object]) -> dict[str, object]:
        [block, _full] = cast(list[object], request["params"])
        if block in {"0x65", "finalized"}:
            return {"result": _HEAD_BLOCK}
        return {"result": _INCLUSION_BLOCK}

    socket = _FakeWebSocket(
        {
            "eth_chainId": {"result": "0x1"},
            "eth_getTransactionReceipt": {"result": _RECEIPT},
            "eth_getBlockByNumber": block_response,
        }
    )
    monkeypatch.setattr(
        "chainwake.providers.evm.websockets.connect",
        _FakeConnector(socket),
    )
    provider = EvmProvider(ETHEREUM_PROFILE)
    await provider.connect(ProviderConfig(rpc_url="ws://ethereum.test"))

    observed = await provider.read_observable(
        f"tx.{_TX_HASH}",
        {"finality": "finalized"},
        at_block=BlockRef(number=101, hash=_HEAD_HASH),
    )

    status = cast(TxFinalityStatus, observed.value)
    assert status.level == "finalized"
