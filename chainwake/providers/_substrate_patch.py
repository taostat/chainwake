"""Vendored fix for a swallowed-error bug in async-substrate-interface.

async-substrate-interface is archived and unmaintained: its README states
plainly that development moved to a transport layer bundled inside the
`bittensor` SDK, which chainwake must never depend on in production (that
package's transport, wallet, keyfile, and signing code are inseparable at
both the packaging and import level — see CLAUDE.md). Filing upstream is a
dead end, so this project pins the last released version and vendors one
targeted fix here instead of forking the whole package.

Bug: ``AsyncSubstrateInterface._process_response`` (async_substrate.py)
checks ``response.get("result")`` but never ``response.get("error")``. When
the node returns a JSON-RPC error (e.g. -32029 rate limit exceeded) instead
of a result — reproduced under contention against Blockmachine's anonymous
RPC tier by firing dozens of concurrent watchers — the method falls through
to the storage item's declared default value exactly as if the queried key
were legitimately absent, silently turning "the request failed" into "here
is a normal read of nothing." Chained through
``chainwake.providers.bittensor.BittensorProvider._require_subnet``, this
misreported subnet 19 (registered since genesis) as nonexistent; the same
swallowing can corrupt any other single-shot ``query()`` call in this
provider, not just that one.

Fix: raise the same ``SubstrateRequestException`` this SDK already raises
for its other error paths (see ``_make_rpc_request``'s subscription-failure
branch) the instant a response carries an "error" key *and* the call is
about to fall through to the storage item's default — i.e. only inside the
``value_scale_type and isinstance(storage_item, ScaleType)`` branch that
actually has the swallowing bug.

The raise is deliberately scoped that narrowly rather than firing for every
"error" key unconditionally: ``rpc_request`` (async_substrate.py) calls
``_make_rpc_request``/``_process_response`` without a ``value_scale_type``,
and already has its own correct, more specific handling of error responses
— a self-healing retry for "Failed to get runtime version" and a typed
``StateDiscardedError`` for a pruned block. An unconditional raise here
would pre-empt both, turning a recoverable retry into a hard failure. Those
callers never reach the buggy default-fallback branch in the first place,
so they don't need this patch and must not be touched by it.
"""

from __future__ import annotations

from typing import Any

from async_substrate_interface.async_substrate import AsyncSubstrateInterface
from async_substrate_interface.errors import SubstrateRequestException
from scalecodec.base import ScaleType

_PATCHED_ATTR = "_chainwake_error_patch_applied"


def apply() -> None:
    """Idempotently patch ``AsyncSubstrateInterface._process_response``.

    Safe to call more than once (e.g. if this module is imported from
    several places, or reloaded in tests) — only the first call patches.
    """
    if getattr(AsyncSubstrateInterface, _PATCHED_ATTR, False):
        return
    original = AsyncSubstrateInterface._process_response

    async def _process_response_raising_on_error(
        self: AsyncSubstrateInterface,
        response: dict[str, Any],
        subscription_id: int | str,
        value_scale_type: str | None = None,
        storage_item: ScaleType | None = None,
        result_handler: Any = None,
        runtime: Any = None,
    ) -> tuple[Any, bool]:
        if "error" in response and value_scale_type and isinstance(storage_item, ScaleType):
            raise SubstrateRequestException(str(response))
        return await original(
            self,
            response,
            subscription_id,
            value_scale_type,
            storage_item,
            result_handler,
            runtime=runtime,
        )

    AsyncSubstrateInterface._process_response = _process_response_raising_on_error
    setattr(AsyncSubstrateInterface, _PATCHED_ATTR, True)


__all__ = ["apply"]
