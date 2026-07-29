"""CoinGecko token resolution and USD price-source contracts."""

from __future__ import annotations

import json
from typing import TypedDict

import httpx
import pytest

from chainwake.core.errors import RateLimitError, UserError
from chainwake.providers.coingecko import CoinGeckoClient

pytestmark = pytest.mark.unit


class _RawToken(TypedDict):
    chainId: int
    address: str
    name: str
    symbol: str
    decimals: int


_DAI: _RawToken = {
    "chainId": 1,
    "address": "0x6b175474e89094c44da98b954eedeac495271d0f",
    "name": "Dai",
    "symbol": "DAI",
    "decimals": 18,
}


@pytest.mark.asyncio
async def test_resolves_symbol_once_and_reuses_cached_chain_token_list() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"tokens": [_DAI]})

    client = CoinGeckoClient(transport=httpx.MockTransport(handler))
    try:
        first = await client.resolve_token(
            platform="ethereum",
            chain_id=1,
            identifier="dai",
        )
        second = await client.resolve_token(
            platform="ethereum",
            chain_id=1,
            identifier="DAI",
        )
    finally:
        await client.close()

    assert first == second
    assert first.symbol == "DAI"
    assert first.address == _DAI["address"]
    assert len(requests) == 1
    assert requests[0].url == "https://tokens.coingecko.com/ethereum/all.json"


@pytest.mark.asyncio
async def test_symbol_collision_requires_an_explicit_contract_address() -> None:
    duplicate = {**_DAI, "address": f"0x{'12' * 20}", "name": "Another Dai"}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tokens": [_DAI, duplicate]})

    client = CoinGeckoClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UserError, match=r"ambiguous.*contract address"):
            await client.resolve_token(
                platform="ethereum",
                chain_id=1,
                identifier="DAI",
            )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_reads_usd_price_with_source_timestamp_and_optional_demo_key() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                _DAI["address"]: {
                    "usd": 0.9998,
                    "last_updated_at": 1_722_000_000,
                }
            },
        )

    client = CoinGeckoClient(
        api_key="demo-key",
        transport=httpx.MockTransport(handler),
    )
    try:
        quote = await client.price_usd(platform="ethereum", token_address=_DAI["address"])
    finally:
        await client.close()

    assert quote.value == pytest.approx(0.9998)
    assert quote.last_updated_at == 1_722_000_000
    assert requests[0].headers["x-cg-demo-api-key"] == "demo-key"
    assert requests[0].url.params["include_last_updated_at"] == "true"


@pytest.mark.asyncio
async def test_reads_native_coin_usd_price_by_stable_coin_id() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "bittensor": {
                    "usd": 190.71,
                    "last_updated_at": 1_722_000_000,
                }
            },
        )

    client = CoinGeckoClient(transport=httpx.MockTransport(handler))
    try:
        quote = await client.coin_price_usd(coin_id="bittensor")
    finally:
        await client.close()

    assert quote.value == pytest.approx(190.71)
    assert quote.last_updated_at == 1_722_000_000
    assert requests[0].url.params["ids"] == "bittensor"
    assert requests[0].url.params["vs_currencies"] == "usd"
    assert requests[0].url.path == "/api/v3/simple/price"


@pytest.mark.asyncio
async def test_anonymous_rate_limit_recommends_free_demo_key() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=json.dumps({"status": "rate limited"}))

    client = CoinGeckoClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(
            RateLimitError,
            match="CHAINWAKE_COINGECKO_API_KEY",
        ):
            await client.price_usd(platform="ethereum", token_address=_DAI["address"])
    finally:
        await client.close()
