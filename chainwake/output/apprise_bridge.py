"""Apprise notification bridge for chainwake.

Wraps any apprise-compatible URI into subject + body and dispatches the
chainwake JSON payload as a notification.  The synchronous `dispatch()`
entry-point wraps `apprise.Apprise.notify()` directly — apprise's sync notify
is thread-safe. Async callers use `async_dispatch()` which runs the sync call
in a thread.

Subject format:
  [chainwake] <status_phrase>
  e.g. "[chainwake] subnet.19.pool.price matched"
       "[chainwake] timeout — max_runtime_reached"
       "[chainwake] budget_exhausted — max_ru_reached"
       "[chainwake] user_error — invalid_observable"
       "[chainwake] provider_error — rpc_unreachable"
       "[chainwake] internal_error — unexpected_exception"

Body: indented JSON of the payload (all fields, two-space indent).  Readable
in a phone notification; complete enough for automated parsing.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlparse

import apprise
import structlog

from chainwake.output.schema import (
    BudgetExhaustedPayload,
    InternalErrorPayload,
    MatchedPayload,
    ObservedEvent,
    ObservedTx,
    Payload,
    ProviderErrorPayload,
    StoppedPayload,
    TimeoutPayload,
    UserErrorPayload,
    serialize,
)

_log = structlog.get_logger(__name__)

SUBJECT_PREFIX = "[chainwake]"


def _safe_uri(uri: str) -> str:
    """Return the URI scheme-only for safe logging (strips credentials and path)."""
    parsed = urlparse(uri)
    if parsed.scheme:
        return f"{parsed.scheme}://<redacted>"
    return "<unknown-scheme>"


_ERROR_TYPES = (UserErrorPayload, ProviderErrorPayload, InternalErrorPayload)


def _build_subject(payload: Payload) -> str:
    """Return a one-line notification subject for the payload."""
    if isinstance(payload, MatchedPayload):
        obs = payload.observed
        # ObservedEvent and ObservedTx use event_type / tx_hash instead of path.
        if isinstance(obs, ObservedEvent):
            label = obs.event_type
        elif isinstance(obs, ObservedTx):
            label = obs.tx_hash
        else:
            label = obs.path
        return f"{SUBJECT_PREFIX} {label} matched"
    if isinstance(payload, TimeoutPayload):
        return f"{SUBJECT_PREFIX} timeout — {payload.reason}"
    if isinstance(payload, StoppedPayload):
        return f"{SUBJECT_PREFIX} stopped — {payload.reason}"
    if isinstance(payload, BudgetExhaustedPayload):
        return f"{SUBJECT_PREFIX} budget_exhausted — {payload.reason}"
    if isinstance(payload, _ERROR_TYPES):
        return f"{SUBJECT_PREFIX} {payload.status} — {payload.reason}"
    return f"{SUBJECT_PREFIX} {payload.status}"  # type: ignore[union-attr]


def _build_body(payload: Payload) -> str:
    """Return the notification body as indented JSON."""
    return json.dumps(serialize(payload), indent=2, sort_keys=True)


def _notify_type_for(payload: Payload) -> str:
    """Map payload status to an apprise NotifyType string."""
    if isinstance(payload, MatchedPayload):
        return apprise.NotifyType.SUCCESS
    if isinstance(payload, TimeoutPayload | StoppedPayload | BudgetExhaustedPayload):
        return apprise.NotifyType.WARNING
    # user_error, provider_error, internal_error
    return apprise.NotifyType.FAILURE


def dispatch(uri: str, payload: Payload) -> bool:
    """Send *payload* to *uri* via apprise.

    Returns True if apprise reported success, False otherwise.  Never raises —
    errors are logged at WARNING level so adapter dispatch stays best-effort.
    """
    safe = _safe_uri(uri)
    try:
        app = apprise.Apprise()
        if not app.add(uri):
            _log.warning("apprise rejected URI", uri=safe)
            return False
        result = app.notify(
            title=_build_subject(payload),
            body=_build_body(payload),
            notify_type=_notify_type_for(payload),
        )
        if not result:
            _log.warning("apprise notification failed", uri=safe)
        return bool(result)
    except Exception:
        _log.warning("apprise dispatch raised an exception", uri=safe, exc_info=True)
        return False


async def async_dispatch(uri: str, payload: Payload) -> bool:
    """Async wrapper: run dispatch() in a thread.

    The runtime is async; apprise's sync notify() is blocking so we offload it
    to avoid stalling the event loop.
    """
    return await asyncio.to_thread(dispatch, uri, payload)


__all__ = [
    "_safe_uri",
    "async_dispatch",
    "dispatch",
]
