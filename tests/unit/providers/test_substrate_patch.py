"""Unit tests for the vendored async-substrate-interface error-handling patch.

See chainwake/providers/_substrate_patch.py for the bug this works around:
AsyncSubstrateInterface._process_response silently decodes a JSON-RPC error
response as the storage item's default value instead of raising.
"""

from __future__ import annotations

from typing import cast

import pytest
from async_substrate_interface.async_substrate import AsyncSubstrateInterface
from async_substrate_interface.errors import SubstrateRequestException

from chainwake.providers import _substrate_patch

pytestmark = pytest.mark.unit


def test_apply_is_idempotent() -> None:
    _substrate_patch.apply()
    patched_once = AsyncSubstrateInterface._process_response
    _substrate_patch.apply()
    assert AsyncSubstrateInterface._process_response is patched_once


@pytest.mark.asyncio
async def test_error_response_raises_instead_of_decoding_as_default() -> None:
    """Regression: a JSON-RPC error must never look like a successful read."""
    _substrate_patch.apply()
    response = {
        "error": {"code": -32029, "message": "rate limit exceeded"},
        "id": "abc123",
        "jsonrpc": "2.0",
    }

    with pytest.raises(SubstrateRequestException):
        await AsyncSubstrateInterface._process_response(
            cast(AsyncSubstrateInterface, None),  # never dereferenced before the patch raises
            response,
            subscription_id=1,
        )


@pytest.mark.asyncio
async def test_normal_response_still_decodes() -> None:
    """The patch must not interfere with a genuine, error-free response."""
    _substrate_patch.apply()
    response = {"result": "0x01", "id": "abc123", "jsonrpc": "2.0"}

    result, complete = await AsyncSubstrateInterface._process_response(
        cast(AsyncSubstrateInterface, None),  # untouched when value_scale_type is falsy
        response,
        subscription_id=1,
    )

    assert result == response
    assert complete is True
