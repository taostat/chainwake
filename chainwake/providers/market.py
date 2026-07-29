"""Shared external market-price component for chain providers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class TokenInfo:
    """Chain-scoped token identity resolved by a market-data source."""

    chain_id: int
    address: str
    name: str
    symbol: str
    decimals: int | None


@dataclass(frozen=True, slots=True)
class UsdPrice:
    """One aggregate USD price observation."""

    value: float
    last_updated_at: int | None


@dataclass(frozen=True, slots=True)
class NativeAsset:
    """Stable market identity for one chain-native asset."""

    coin_id: str
    name: str
    symbol: str


@dataclass(frozen=True, slots=True)
class MarketPrice:
    """Normalized price plus JSON-safe source provenance."""

    value: float
    meta: dict[str, object]


class MarketPriceSource(Protocol):
    """Market-data operations consumed by the shared price feed."""

    async def close(self) -> None: ...

    async def resolve_token(
        self,
        *,
        platform: str,
        chain_id: int,
        identifier: str,
    ) -> TokenInfo: ...

    async def price_usd(
        self,
        *,
        platform: str,
        token_address: str,
    ) -> UsdPrice: ...

    async def coin_price_usd(self, *, coin_id: str) -> UsdPrice: ...


def _price_timestamp(timestamp: int | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat().replace("+00:00", "Z")


def _default_source() -> MarketPriceSource:
    # Local import keeps the source implementation dependent on these domain
    # contracts, rather than making the contracts depend on CoinGecko.
    from chainwake.providers.coingecko import CoinGeckoClient  # noqa: PLC0415

    return CoinGeckoClient(api_key=os.environ.get("CHAINWAKE_COINGECKO_API_KEY"))


class MarketPriceFeed:
    """Own one lazy market source and normalize native/token provenance."""

    def __init__(self, source: MarketPriceSource | None = None) -> None:
        self._source = source

    @property
    def _connected(self) -> MarketPriceSource:
        if self._source is None:
            self._source = _default_source()
        return self._source

    async def close(self) -> None:
        source = self._source
        self._source = None
        if source is not None:
            await source.close()

    async def native_usd(self, asset: NativeAsset) -> MarketPrice:
        quote = await self._connected.coin_price_usd(coin_id=asset.coin_id)
        return MarketPrice(
            value=quote.value,
            meta={
                "source": "coingecko",
                "quote_currency": "usd",
                "coin_id": asset.coin_id,
                "coin_name": asset.name,
                "coin_symbol": asset.symbol,
                "price_last_updated_at": _price_timestamp(quote.last_updated_at),
            },
        )

    async def token_usd(
        self,
        *,
        platform: str,
        chain_id: int,
        identifier: str,
    ) -> MarketPrice:
        token = await self._connected.resolve_token(
            platform=platform,
            chain_id=chain_id,
            identifier=identifier,
        )
        quote = await self._connected.price_usd(
            platform=platform,
            token_address=token.address,
        )
        return MarketPrice(
            value=quote.value,
            meta={
                "source": "coingecko",
                "quote_currency": "usd",
                "token_address": token.address,
                "token_name": token.name,
                "token_symbol": token.symbol,
                "token_decimals": token.decimals,
                "price_last_updated_at": _price_timestamp(quote.last_updated_at),
            },
        )


__all__ = [
    "MarketPrice",
    "MarketPriceFeed",
    "MarketPriceSource",
    "NativeAsset",
    "TokenInfo",
    "UsdPrice",
]
