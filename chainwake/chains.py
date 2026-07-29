"""Chain backend registration and runtime configuration.

This module is the single chain-selection boundary shared by the CLI and
runtime. Importing it does not open a connection or eagerly import a provider.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal

from chainwake.providers.base import ChainProvider

ChainAlias = Literal["bt", "eth", "base", "bsc"]
ProviderFactory = Callable[[], ChainProvider]


@dataclass(frozen=True, slots=True)
class ChainRuntimeConfig:
    """Chain-owned timing and registry-estimate constants."""

    block_seconds: float
    epoch_state_read_cost: int
    event_block_read_cost: int
    event_legacy_block_read_cost: int

    def __post_init__(self) -> None:
        if self.block_seconds <= 0:
            raise ValueError("block_seconds must be positive")
        for field_name in (
            "epoch_state_read_cost",
            "event_block_read_cost",
            "event_legacy_block_read_cost",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must be non-negative")


@dataclass(frozen=True, slots=True)
class ChainBackend:
    """Provider factory and runtime policy for one chain alias."""

    alias: ChainAlias
    provider_factory: ProviderFactory
    runtime: ChainRuntimeConfig

    def create_provider(self) -> ChainProvider:
        provider = self.provider_factory()
        if not isinstance(provider, ChainProvider):
            raise TypeError(f"backend {self.alias!r} factory returned an invalid provider")
        if provider.short_alias != self.alias:
            raise ValueError(
                f"provider alias {provider.short_alias!r} does not match backend {self.alias!r}"
            )
        return provider


class ChainBackendRegistry:
    """Explicit mapping from public chain aliases to backend definitions."""

    def __init__(self, backends: Iterable[ChainBackend] = ()) -> None:
        self._backends: dict[ChainAlias, ChainBackend] = {}
        for backend in backends:
            self.register(backend)

    def register(self, backend: ChainBackend) -> None:
        if backend.alias in self._backends:
            raise ValueError(f"duplicate chain backend {backend.alias!r}")
        self._backends[backend.alias] = backend

    def get(self, alias: ChainAlias) -> ChainBackend:
        try:
            return self._backends[alias]
        except KeyError as exc:
            raise KeyError(f"unknown chain backend {alias!r}") from exc

    def create_provider(self, alias: ChainAlias) -> ChainProvider:
        return self.get(alias).create_provider()


def _bittensor_provider() -> ChainProvider:
    from chainwake.providers.bittensor import BittensorProvider  # noqa: PLC0415

    return BittensorProvider()


def _evm_provider(alias: Literal["eth", "base", "bsc"]) -> ChainProvider:
    from chainwake.providers.evm import EvmProvider, profile_for  # noqa: PLC0415

    return EvmProvider(profile_for(alias))


def _ethereum_provider() -> ChainProvider:
    return _evm_provider("eth")


def _base_provider() -> ChainProvider:
    return _evm_provider("base")


def _bsc_provider() -> ChainProvider:
    return _evm_provider("bsc")


def _evm_runtime(alias: Literal["eth", "base", "bsc"]) -> ChainRuntimeConfig:
    from chainwake.providers.evm import profile_for  # noqa: PLC0415

    return ChainRuntimeConfig(
        block_seconds=profile_for(alias).block_seconds,
        epoch_state_read_cost=0,
        event_block_read_cost=1,
        event_legacy_block_read_cost=1,
    )


_BACKENDS = ChainBackendRegistry(
    [
        ChainBackend(
            alias="bt",
            provider_factory=_bittensor_provider,
            runtime=ChainRuntimeConfig(
                block_seconds=12.0,
                epoch_state_read_cost=4,
                event_block_read_cost=2,
                event_legacy_block_read_cost=4,
            ),
        ),
        ChainBackend(
            alias="eth",
            provider_factory=_ethereum_provider,
            runtime=_evm_runtime("eth"),
        ),
        ChainBackend(
            alias="base",
            provider_factory=_base_provider,
            runtime=_evm_runtime("base"),
        ),
        ChainBackend(
            alias="bsc",
            provider_factory=_bsc_provider,
            runtime=_evm_runtime("bsc"),
        ),
    ]
)


def backend_for(alias: ChainAlias) -> ChainBackend:
    """Return the registered backend for ``alias``."""

    return _BACKENDS.get(alias)


__all__ = [
    "ChainAlias",
    "ChainBackend",
    "ChainBackendRegistry",
    "ChainRuntimeConfig",
    "ProviderFactory",
    "backend_for",
]
