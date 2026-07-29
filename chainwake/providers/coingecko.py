"""CoinGecko-backed token identity and aggregate USD price source."""

from __future__ import annotations

import math
import os
import re
from typing import Final

import httpx

from chainwake.core.errors import (
    AuthError,
    DecodeError,
    RateLimitError,
    RPCUnreachableError,
    UserError,
)
from chainwake.providers.market import TokenInfo, UsdPrice

_TOKEN_CATALOG_BASE_URL: Final[str] = "https://tokens.coingecko.com"  # noqa: S105
_API_BASE_URL: Final[str] = "https://api.coingecko.com/api/v3"
_EVM_ADDRESS_RE: Final[re.Pattern[str]] = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HTTP_BAD_REQUEST = 400
_HTTP_TOO_MANY_REQUESTS = 429


class CoinGeckoClient:
    """Resolve chain tokens and fetch aggregate prices by contract address."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        token_list_base_url: str | None = None,
        api_base_url: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._token_list_base_url = (
            token_list_base_url
            or os.environ.get("CHAINWAKE_COINGECKO_TOKEN_LIST_BASE_URL")
            or _TOKEN_CATALOG_BASE_URL
        ).rstrip("/")
        self._api_base_url = (
            api_base_url or os.environ.get("CHAINWAKE_COINGECKO_API_BASE_URL") or _API_BASE_URL
        ).rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout_seconds, transport=transport)
        self._token_lists: dict[tuple[str, int], tuple[TokenInfo, ...]] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def resolve_token(
        self,
        *,
        platform: str,
        chain_id: int,
        identifier: str,
    ) -> TokenInfo:
        """Resolve a symbol or explicit EVM contract within one chain."""

        candidate = identifier.strip()
        if not candidate:
            raise UserError("token identifier must not be empty", reason="invalid_path_params")
        tokens = await self._tokens(platform=platform, chain_id=chain_id)

        if _EVM_ADDRESS_RE.fullmatch(candidate):
            address = candidate.lower()
            match = next((token for token in tokens if token.address.lower() == address), None)
            return match or TokenInfo(
                chain_id=chain_id,
                address=address,
                name=address,
                symbol=address,
                decimals=None,
            )

        symbol = candidate.upper()
        matches = [token for token in tokens if token.symbol.upper() == symbol]
        if not matches:
            raise UserError(
                f"CoinGecko has no {symbol!r} token on {platform}; pass its contract address",
                reason="invalid_path_params",
            )
        if len(matches) > 1:
            addresses = ", ".join(token.address for token in matches)
            raise UserError(
                f"token symbol {symbol!r} is ambiguous on {platform} ({addresses}); "
                "pass an explicit contract address",
                reason="invalid_path_params",
            )
        return matches[0]

    async def price_usd(
        self,
        *,
        platform: str,
        token_address: str,
    ) -> UsdPrice:
        """Return CoinGecko's aggregate USD price for one contract."""

        headers = {"x-cg-demo-api-key": self._api_key} if self._api_key else None
        payload = await self._get_json(
            f"{self._api_base_url}/simple/token_price/{platform}",
            params={
                "contract_addresses": token_address,
                "vs_currencies": "usd",
                "include_last_updated_at": "true",
                "precision": "full",
            },
            headers=headers,
        )
        return self._decode_usd_price(
            payload,
            key=token_address,
            missing_message=(f"CoinGecko returned no USD price for {token_address} on {platform}"),
        )

    async def coin_price_usd(self, *, coin_id: str) -> UsdPrice:
        """Return an aggregate USD price for one native asset by CoinGecko ID."""

        identifier = coin_id.strip()
        if not identifier:
            raise UserError("CoinGecko coin ID must not be empty", reason="invalid_path_params")
        headers = {"x-cg-demo-api-key": self._api_key} if self._api_key else None
        payload = await self._get_json(
            f"{self._api_base_url}/simple/price",
            params={
                "ids": identifier,
                "vs_currencies": "usd",
                "include_last_updated_at": "true",
                "precision": "full",
            },
            headers=headers,
        )
        return self._decode_usd_price(
            payload,
            key=identifier,
            missing_message=f"CoinGecko returned no USD price for coin ID {identifier!r}",
        )

    @staticmethod
    def _decode_usd_price(
        payload: object,
        *,
        key: str,
        missing_message: str,
    ) -> UsdPrice:
        """Validate one keyed CoinGecko simple-price response."""
        if not isinstance(payload, dict):
            raise DecodeError("CoinGecko token-price response must be an object")
        raw_quote = payload.get(key.lower()) or payload.get(key)
        if not isinstance(raw_quote, dict):
            raise DecodeError(missing_message)
        raw_value = raw_quote.get("usd")
        if isinstance(raw_value, bool) or not isinstance(raw_value, int | float):
            raise DecodeError("CoinGecko token-price response has no numeric usd value")
        value = float(raw_value)
        if not math.isfinite(value) or value < 0:
            raise DecodeError("CoinGecko USD price must be finite and non-negative")
        raw_updated = raw_quote.get("last_updated_at")
        if raw_updated is not None and (
            isinstance(raw_updated, bool) or not isinstance(raw_updated, int | float)
        ):
            raise DecodeError("CoinGecko last_updated_at must be a Unix timestamp")
        return UsdPrice(
            value=value,
            last_updated_at=int(raw_updated) if raw_updated is not None else None,
        )

    async def _tokens(
        self,
        *,
        platform: str,
        chain_id: int,
    ) -> tuple[TokenInfo, ...]:
        cache_key = (platform, chain_id)
        cached = self._token_lists.get(cache_key)
        if cached is not None:
            return cached
        payload = await self._get_json(f"{self._token_list_base_url}/{platform}/all.json")
        if not isinstance(payload, dict):
            raise DecodeError("CoinGecko token list must contain a tokens array")
        raw_tokens = payload.get("tokens")
        if not isinstance(raw_tokens, list):
            raise DecodeError("CoinGecko token list must contain a tokens array")
        tokens: list[TokenInfo] = []
        for raw in raw_tokens:
            if not isinstance(raw, dict) or raw.get("chainId") != chain_id:
                continue
            address = raw.get("address")
            symbol = raw.get("symbol")
            name = raw.get("name")
            decimals = raw.get("decimals")
            if (
                not isinstance(address, str)
                or _EVM_ADDRESS_RE.fullmatch(address) is None
                or not isinstance(symbol, str)
                or not symbol
                or not isinstance(name, str)
                or not name
                or (
                    decimals is not None
                    and (isinstance(decimals, bool) or not isinstance(decimals, int))
                )
            ):
                continue
            tokens.append(
                TokenInfo(
                    chain_id=chain_id,
                    address=address.lower(),
                    name=name,
                    symbol=symbol,
                    decimals=decimals,
                )
            )
        resolved = tuple(tokens)
        self._token_lists[cache_key] = resolved
        return resolved

    async def _get_json(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> object:
        try:
            response = await self._client.get(url, params=params, headers=headers)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise RPCUnreachableError(f"CoinGecko request failed: {exc}") from exc
        if response.status_code in {401, 403}:
            raise AuthError(
                "CoinGecko rejected CHAINWAKE_COINGECKO_API_KEY; "
                "remove it for anonymous access or provide a valid Demo key"
            )
        if response.status_code == _HTTP_TOO_MANY_REQUESTS:
            raise RateLimitError(
                "CoinGecko anonymous rate limit reached; retry later or create a free "
                "Demo API key and set CHAINWAKE_COINGECKO_API_KEY"
            )
        if response.status_code >= _HTTP_BAD_REQUEST:
            raise RPCUnreachableError(f"CoinGecko request failed with HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise DecodeError("CoinGecko returned invalid JSON") from exc


__all__ = ["CoinGeckoClient"]
