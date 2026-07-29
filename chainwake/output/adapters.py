"""Adapter Protocol and built-in implementations.

Four impls:

- `DefaultAdapter`: writes the payload as JSON to stdout (or any text stream)
  and signals exit. The agent contract.
- `StreamAdapter`: writes NDJSON; never signals exit.
- `FileAdapter`: appends NDJSON to a file. Used with `--out file:///path`.
- `AppriseAdapter`: dispatches via apprise to any supported notification URI.
  Failures are logged but do not crash the watcher.
"""

from __future__ import annotations

import io
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

import structlog

from chainwake.output.apprise_bridge import _safe_uri
from chainwake.output.apprise_bridge import dispatch as apprise_dispatch
from chainwake.output.render import RenderMode, render_human
from chainwake.output.schema import Payload, serialize

_log = structlog.get_logger(__name__)


@runtime_checkable
class Adapter(Protocol):
    """Output sink for chainwake payloads.

    Lifecycle:
        - The runtime calls `dispatch(payload)` once per match.
        - After dispatch, the runtime checks `should_exit_after_dispatch`.
          When True, the watcher process exits with the payload's status code.
          When False (e.g. `stream`), the runtime keeps the watcher alive.
        - `close()` is called once at watcher shutdown for any cleanup.

    All operations are sync. The runtime offloads every ``dispatch`` and
    ``close`` call onto the default executor via ``asyncio.to_thread``, so
    adapters that block on IO (notably AppriseAdapter, which makes HTTP
    requests through apprise) do not stall the watcher's event loop.
    """

    name: str

    @property
    def should_exit_after_dispatch(self) -> bool: ...

    def dispatch(self, payload: Payload) -> None: ...

    def close(self) -> None: ...


def _serialise_oneline(payload: Payload) -> str:
    return json.dumps(
        serialize(payload),
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def _serialise_indented(payload: Payload) -> str:
    return json.dumps(serialize(payload), indent=2, sort_keys=True, allow_nan=False)


@dataclass(slots=True)
class DefaultAdapter:
    """Writes the payload to stdout and signals exit.

    Used when no `--out` flag is specified. Matches the agent contract: one
    invocation, one payload, one exit.

    `render_mode` controls the format on stdout:

    - ``RenderMode.JSON`` writes pretty-printed JSON (the agent contract).
    - ``RenderMode.HUMAN`` writes the prose form from
      :func:`chainwake.output.render.render_human` (TTY-friendly).
    - ``RenderMode.AUTO`` is resolved at construction time via
      :func:`resolve_render_mode`, which picks ``HUMAN`` when stdout is a
      TTY and ``JSON`` otherwise.

    Only this adapter participates in render-mode switching; ``--out
    file://``, ``--out stream``, and apprise URIs always emit JSON because
    their consumers are machines.
    """

    name: str = "default"
    stream: io.TextIOBase | None = None
    render_mode: RenderMode = RenderMode.AUTO

    @property
    def should_exit_after_dispatch(self) -> bool:
        return True

    def _resolved_mode(self, target: io.TextIOBase | object) -> RenderMode:
        """Resolve AUTO against the actual write target (custom stream or stdout)."""
        if self.render_mode is not RenderMode.AUTO:
            return self.render_mode
        is_tty = bool(getattr(target, "isatty", lambda: False)())
        return RenderMode.HUMAN if is_tty else RenderMode.JSON

    def dispatch(self, payload: Payload) -> None:
        target = self.stream if self.stream is not None else sys.stdout
        if self._resolved_mode(target) is RenderMode.HUMAN:
            target.write(render_human(payload) + "\n")
        else:
            target.write(_serialise_indented(payload) + "\n")
        target.flush()

    def close(self) -> None:
        return None


@dataclass(slots=True)
class StreamAdapter:
    """NDJSON keep-alive adapter.

    Used with `--out stream`. Writes one line per match and keeps the watcher
    alive — the runtime composes this with notification adapters for
    "stream + ping me" patterns.
    """

    name: str = "stream"
    stream: io.TextIOBase | None = None

    @property
    def should_exit_after_dispatch(self) -> bool:
        return False

    def dispatch(self, payload: Payload) -> None:
        target = self.stream if self.stream is not None else sys.stdout
        target.write(_serialise_oneline(payload) + "\n")
        target.flush()

    def close(self) -> None:
        return None


class FileAdapter:
    """Appends NDJSON to a file. Used with `--out file:///path`.

    Files are opened on construction and flushed after each dispatch. The
    runtime ensures `close()` is called on shutdown.
    """

    name: str = "file"

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle: io.TextIOWrapper | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def should_exit_after_dispatch(self) -> bool:
        return False

    def _open(self) -> io.TextIOWrapper:
        if self._handle is None or self._handle.closed:
            self._handle = self._path.open("a", encoding="utf-8")
        return self._handle

    def dispatch(self, payload: Payload) -> None:
        handle = self._open()
        handle.write(_serialise_oneline(payload) + "\n")
        handle.flush()

    def close(self) -> None:
        if self._handle is not None and not self._handle.closed:
            self._handle.close()


@dataclass(slots=True)
class AppriseAdapter:
    """Notification dispatch via apprise.

    Any apprise-compatible URI is accepted (tgram://, discord://, slack://,
    mailto://, json://, etc.).  Dispatch is best-effort: failures are logged
    via structlog but never crash the watcher.
    """

    uri: str
    name: str = "apprise"
    _dispatched: int = field(default=0, init=False, repr=False)

    @property
    def should_exit_after_dispatch(self) -> bool:
        return False

    def dispatch(self, payload: Payload) -> None:
        ok = apprise_dispatch(self.uri, payload)
        self._dispatched += 1
        if not ok:
            _log.warning(
                "apprise dispatch failed",
                uri=_safe_uri(self.uri),
                status=payload.status,  # type: ignore[union-attr]
                dispatch_count=self._dispatched,
            )

    def close(self) -> None:
        return None


def parse_adapter_uri(uri: str) -> Adapter:
    """Build an Adapter from a `--out` URI string.

    Recognised schemes:
        - `stream` (no scheme): NDJSON to stdout, keep alive
        - `file:///absolute/path` or `file://./relative`: append NDJSON
        - any apprise-recognised URI: dispatch via apprise
    """
    if uri == "stream":
        return StreamAdapter()
    if uri.startswith("file://"):
        # urlparse misreads `file://./rel` as netloc='.', path='/rel'. Strip the
        # scheme manually so absolute (`file:///abs`), relative-cwd
        # (`file://./rel`), and bare-relative (`file://rel`) forms all map to
        # the path the user typed.
        rest = uri[len("file://") :]
        if not rest:
            raise ValueError(f"file:// URI requires a path, got {uri!r}")
        return FileAdapter(Path(rest))
    return AppriseAdapter(uri=uri)


__all__ = [
    "Adapter",
    "AppriseAdapter",
    "DefaultAdapter",
    "FileAdapter",
    "StreamAdapter",
    "parse_adapter_uri",
]
