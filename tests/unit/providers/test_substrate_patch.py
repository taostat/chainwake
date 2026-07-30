"""Unit tests for the vendored async-substrate-interface error-handling patch.

See chainwake/providers/_substrate_patch.py for the bug this works around:
AsyncSubstrateInterface._process_response silently decodes a JSON-RPC error
response as the storage item's default value instead of raising — but only
inside its ``value_scale_type and isinstance(storage_item, ScaleType)``
branch. Callers that don't decode a storage value (e.g. ``rpc_request``,
which already has its own correct, more specific error handling — a
self-healing retry for a stale runtime, a typed error for a pruned block)
never reach that branch and must see the original passthrough behavior
unchanged.
"""

from __future__ import annotations

from typing import cast

import pytest
from async_substrate_interface.async_substrate import AsyncSubstrateInterface
from async_substrate_interface.errors import SubstrateRequestException
from scalecodec.base import ScaleType

from chainwake.providers import _substrate_patch

pytestmark = pytest.mark.unit

_ERROR_RESPONSE = {
    "error": {"code": -32029, "message": "rate limit exceeded"},
    "id": "abc123",
    "jsonrpc": "2.0",
}


class _FakeStorageItem(ScaleType):
    """Minimal concrete ScaleType: enough to satisfy isinstance(), never decoded."""

    def process(self) -> None:
        raise AssertionError("must not be reached — the patch raises before decoding")


def test_apply_is_idempotent() -> None:
    _substrate_patch.apply()
    patched_once = AsyncSubstrateInterface._process_response
    _substrate_patch.apply()
    assert AsyncSubstrateInterface._process_response is patched_once


@pytest.mark.asyncio
async def test_error_response_in_storage_decode_path_raises() -> None:
    """Regression: an error must never decode as a storage item's default value.

    This is the actual bug from #7/#8/#9: a query()-style call resolves a
    real value_scale_type and storage_item, so it would otherwise fall
    through to the default-value branch.
    """
    _substrate_patch.apply()

    with pytest.raises(SubstrateRequestException):
        await AsyncSubstrateInterface._process_response(
            cast(AsyncSubstrateInterface, None),  # never dereferenced before the raise
            _ERROR_RESPONSE,
            1,
            "bool",
            _FakeStorageItem(),
        )


@pytest.mark.asyncio
async def test_error_response_outside_storage_decode_path_passes_through() -> None:
    """rpc_request()-style calls (no value_scale_type) must be untouched.

    rpc_request calls _make_rpc_request/_process_response without a
    value_scale_type, then inspects the error itself to retry a stale
    runtime or raise a typed StateDiscardedError. An unconditional raise
    here would pre-empt that self-healing retry — see the regression this
    test guards against.
    """
    _substrate_patch.apply()

    result, complete = await AsyncSubstrateInterface._process_response(
        cast(AsyncSubstrateInterface, None),  # untouched: value_scale_type is falsy
        _ERROR_RESPONSE,
        1,
    )

    assert result == _ERROR_RESPONSE
    assert complete is True


@pytest.mark.asyncio
async def test_normal_response_still_decodes() -> None:
    """The patch must not interfere with a genuine, error-free response."""
    _substrate_patch.apply()
    response = {"result": "0x01", "id": "abc123", "jsonrpc": "2.0"}

    result, complete = await AsyncSubstrateInterface._process_response(
        cast(AsyncSubstrateInterface, None),  # untouched when value_scale_type is falsy
        response,
        1,
    )

    assert result == response
    assert complete is True
