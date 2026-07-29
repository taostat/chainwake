"""Smoke tests for pool observables against the live finney chain.

These tests catch decode-path bugs that pure unit tests cannot — they
exercise the real `SubnetInfoRuntimeApi.get_dynamic_info` shape and
verify the values our provider returns are in plausible ranges. The
canonical case they catch is the ``moving_price`` U32F32 vs U64F64
divisor mistake: a unit test that round-trips its own synthetic input
with the wrong divisor still passes; a smoke test that asserts the
result tracks the spot price catches it immediately.

Run on demand:

    pytest -m smoke

These tests hit the public finney endpoint
(`wss://entrypoint-finney.opentensor.ai:443`) so they require network
access. They must NOT run on every push — that would put unnecessary
load on a public RPC and tie CI to external availability.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from chainwake.providers.base import ProviderConfig
from chainwake.providers.bittensor import BittensorProvider

pytestmark = pytest.mark.smoke

FINNEY_RPC = "wss://entrypoint-finney.opentensor.ai:443"
# SN1 has been continuously active since dTAO launch; using a fixed netuid
# keeps the test deterministic. If SN1 ever gets deregistered, swap for any
# other long-lived subnet. The test is robust to liquidity changes but
# assumes the subnet still exists.
SMOKE_NETUID = 1


@pytest.fixture(scope="module")
async def provider() -> AsyncIterator[BittensorProvider]:
    """Module-scoped finney connection. One handshake per module run."""
    p = BittensorProvider()
    await p.connect(ProviderConfig(rpc_url=FINNEY_RPC, api_key=None))
    try:
        yield p
    finally:
        await p.disconnect()


async def test_price_is_in_plausible_range(provider: BittensorProvider) -> None:
    """Alpha price should be a positive float in TAO/alpha — never zero, never absurdly large."""
    val = await provider.read_observable(f"subnet.{SMOKE_NETUID}.pool.price", {})
    assert isinstance(val.value, float)
    # An active subnet's alpha price is bounded by available TAO supply (~21M),
    # but in practice prices sit between 1e-6 and 100 TAO/alpha. A value
    # outside this range almost certainly indicates a decode bug.
    assert 1e-6 < val.value < 100, f"price {val.value} out of plausible range"


async def test_alpha_supply_is_positive(provider: BittensorProvider) -> None:
    """`alpha_out` (circulating supply) must be > 0 for any subnet that has emitted."""
    val = await provider.read_observable(f"subnet.{SMOKE_NETUID}.pool.alpha-supply", {})
    assert isinstance(val.value, float)
    assert val.value > 0, f"alpha-supply {val.value} should be positive"


async def test_volume_is_non_negative(provider: BittensorProvider) -> None:
    """`subnet_volume` is cumulative — non-negative on any subnet, positive on active ones."""
    val = await provider.read_observable(f"subnet.{SMOKE_NETUID}.pool.volume", {})
    assert isinstance(val.value, float)
    assert val.value >= 0, f"volume {val.value} should be non-negative"


async def test_moving_price_tracks_spot(provider: BittensorProvider) -> None:
    """Sanity-cross-check: moving_price should be in the same order of magnitude as spot.

    This is the test that catches the U32F32 vs U64F64 class of bug. The
    EMA lags spot but typically stays within ~50% of it during normal
    market conditions. A converted moving_price that's 10**12 too small
    would fail this assertion immediately.
    """
    spot_val = await provider.read_observable(f"subnet.{SMOKE_NETUID}.pool.price", {})
    ema_val = await provider.read_observable(f"subnet.{SMOKE_NETUID}.pool.moving-price", {})
    assert isinstance(spot_val.value, float)
    assert isinstance(ema_val.value, float)
    spot = spot_val.value
    ema = ema_val.value
    assert spot > 0
    assert ema > 0, f"moving_price {ema} - decode produced ~zero, likely wrong divisor"
    # Both should agree to within an order of magnitude. A tighter bound
    # (e.g. 50%) would flake during market moves; an order-of-magnitude
    # check is enough to catch decode bugs without false positives.
    ratio = max(spot, ema) / min(spot, ema)
    assert ratio < 10, f"spot {spot} and moving_price {ema} disagree by >10x (ratio {ratio})"


async def test_ema_tao_flow_is_finite_signed_float(provider: BittensorProvider) -> None:
    """`SubnetEmaTaoFlow` is signed; both signs are valid. Value can be 0 on idle subnets."""
    val = await provider.read_observable(f"subnet.{SMOKE_NETUID}.ema-tao-flow", {})
    assert isinstance(val.value, float)
    assert val.value == val.value, "ema-tao-flow is NaN"
    # Observed range on finney (subnets 1-29): -0.052 to +0.007 TAO.
    # A bound of 1000 TAO gives generous headroom while still catching a
    # decode regression (e.g. forgetting to divide by RAO_PER_TAO would give
    # values of order 1e6).
    assert abs(val.value) < 1000, f"ema-tao-flow {val.value} is implausibly large"


async def test_get_dynamic_info_keys_match_assumptions(provider: BittensorProvider) -> None:
    """Schema-stability check: the runtime call must still return the keys we read.

    A chain runtime upgrade that renames or removes any of these would
    silently break our pool observables. This test catches that on the
    next smoke run rather than at end-user runtime.
    """
    # Reach into the provider's helper directly — we want to verify the
    # raw shape, not what our handlers extract from it.
    head = await provider._connected.get_chain_head()
    info = await provider._read_subnet_dynamic_info(SMOKE_NETUID, head)
    expected_keys = {
        "tao_in",
        "alpha_in",
        "alpha_out",
        "subnet_volume",
        "moving_price",
    }
    missing = expected_keys - set(info)
    assert not missing, f"get_dynamic_info missing expected keys: {missing}"
    # moving_price must remain a {'bits': N} dict — if the chain switches
    # to a plain int or different shape we want to know before our decoder
    # silently returns 0.0.
    mp = info.get("moving_price")
    assert isinstance(mp, dict), f"moving_price changed shape: {type(mp).__name__}"
    assert "bits" in mp, f"moving_price dict missing 'bits' key: {list(mp)}"
