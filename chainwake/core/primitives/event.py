"""Event primitive.

Fires on the first chain event whose ``event_type`` matches the configured
filter and whose ``args`` satisfy an optional AND-match on key-value pairs.

Stateless — each ``Event`` is matched independently; no window or prior-
event state is kept. The runtime decides whether to exit after dispatch
(default) or keep the watcher alive (``--out stream``).

``ObservableValue`` inputs are silently ignored (returning ``NoMatch``);
this primitive is designed for the event stream path, not the polling path.

``Match.observed`` conforms to ``ObservedEvent`` in ``output/schema.py``.
"""

from __future__ import annotations

from chainwake.core.primitives.base import Match, PrimitiveOutcome, no_match
from chainwake.providers.base import Event, ObservableValue


def _json_safe(value: object) -> object:
    """Preserve decoded event data in the JSON output contract."""
    if isinstance(value, str | int | float | bool | type(None)):
        return value
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_safe(item) for item in value]
    decoded = getattr(value, "value", None)
    if decoded is not None and decoded is not value:
        return _json_safe(decoded)
    return str(value)


class EventPrimitive:
    """Fires when a chain event matches the configured type and args filter.

    Args:
        event_type: the canonical event type string to match, e.g.
            ``"subnet-registered"``.
        args_match: optional dict of key-value pairs that must all be present
            and equal in the event's ``args``. An empty dict matches any event
            of the given type.
    """

    name: str = "event"

    def __init__(
        self,
        *,
        event_type: str,
        args_match: dict[str, object] | None = None,
    ) -> None:
        self._event_type = event_type
        self._args_match: dict[str, object] = args_match or {}

    def evaluate(self, observation: ObservableValue | Event) -> PrimitiveOutcome:
        """Return ``Match`` when the event matches; ``NoMatch`` otherwise."""
        if not isinstance(observation, Event):
            return no_match()

        if observation.event_type != self._event_type:
            return no_match()

        for key, expected in self._args_match.items():
            if observation.args.get(key) != expected:
                return no_match()

        json_args = {k: _json_safe(v) for k, v in observation.args.items()}
        return Match(
            observed={
                "event_type": observation.event_type,
                "raw_event": observation.raw_event,
                "args": json_args,
                "block": observation.block,
                "block_hash": observation.block_hash,
                "timestamp": observation.timestamp.isoformat(),
                "extrinsic_hash": observation.extrinsic_hash,
            }
        )

    def reset(self) -> None:
        """No-op: event primitive is stateless."""


__all__ = ["EventPrimitive"]
