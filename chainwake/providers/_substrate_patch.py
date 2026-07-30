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
branch) the instant a response carries an "error" key, before any
default-value fallback runs.
"""

from __future__ import annotations

from typing import Any

from async_substrate_interface.async_substrate import AsyncSubstrateInterface
from async_substrate_interface.errors import SubstrateRequestException

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
        *args: Any,
        **kwargs: Any,
    ) -> tuple[Any, bool]:
        if "error" in response:
            raise SubstrateRequestException(str(response))
        return await original(self, response, *args, **kwargs)

    AsyncSubstrateInterface._process_response = _process_response_raising_on_error
    setattr(AsyncSubstrateInterface, _PATCHED_ATTR, True)


__all__ = ["apply"]
