"""Unit tests for chainwake.output.adapters.

Covers AppriseAdapter with mocked apprise_dispatch, FileAdapter NDJSON
guarantees (append-only, multi-write integrity, parent dir creation),
StreamAdapter NDJSON, multi-adapter dispatch independence, and failure
handling (apprise fails → logs, no crash).
"""

from __future__ import annotations

import io
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from chainwake.output.adapters import (
    AppriseAdapter,
    DefaultAdapter,
    FileAdapter,
    StreamAdapter,
    _serialise_oneline,
    parse_adapter_uri,
)
from chainwake.output.apprise_bridge import dispatch as bridge_dispatch
from chainwake.output.render import RenderMode
from chainwake.output.schema import (
    Budget,
    MatchedPayload,
    ObservedThreshold,
    Payload,
    Process,
    ThresholdCondition,
    TimeoutPayload,
    Watcher,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts() -> datetime:
    return datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)


def _budget() -> Budget:
    return Budget(runtime_ms=10, rpc_calls=1, estimated_ru_consumed=1)


def _process() -> Process:
    return Process(pid=999, started_at=_ts())


def _matched() -> Payload:
    return MatchedPayload(
        watcher=Watcher(
            chain="bt",
            resource="subnet",
            resource_id="19",
            sub_resource="pool.price",
            primitive="threshold",
            invocation=["chainwake", "bt", "subnet", "19", "price"],
        ),
        condition=ThresholdCondition(operator="below", target=0.05),
        observed=ObservedThreshold(
            path="subnet.19.pool.price",
            value=0.04,
            block=100,
            block_hash="0xabc",
            timestamp=_ts(),
        ),
        budget=_budget(),
        process=_process(),
    )


def test_json_serialisation_fails_closed_on_non_finite_number() -> None:
    observed = ObservedThreshold.model_construct(
        path="subnet.19.pool.price",
        value=math.inf,
        block=1,
        block_hash="0xabc",
        timestamp=_ts(),
    )
    payload = MatchedPayload.model_construct(
        watcher=Watcher(
            chain="bt",
            resource="subnet",
            resource_id="19",
            sub_resource="pool.price",
            primitive="threshold",
            invocation=["chainwake"],
        ),
        condition=ThresholdCondition(operator="above", target=1.0),
        observed=observed,
        budget=_budget(),
        process=_process(),
    )

    with pytest.raises(ValueError, match="Out of range float"):
        _serialise_oneline(payload)


def _timeout() -> Payload:
    return TimeoutPayload(
        watcher=Watcher(
            chain="bt",
            resource="subnet",
            primitive="threshold",
            invocation=["chainwake"],
        ),
        condition=ThresholdCondition(operator="below", target=0.05),
        reason="max_runtime_reached",
        budget=_budget(),
        process=_process(),
    )


# ---------------------------------------------------------------------------
# AppriseAdapter — real dispatch mocked
# ---------------------------------------------------------------------------


def test_apprise_adapter_calls_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Payload]] = []

    def _fake(uri: str, payload: Payload) -> bool:
        calls.append((uri, payload))
        return True

    monkeypatch.setattr("chainwake.output.adapters.apprise_dispatch", _fake)

    adapter = AppriseAdapter(uri="discord://1234/TOKEN")
    adapter.dispatch(_matched())

    assert len(calls) == 1
    assert calls[0][0] == "discord://1234/TOKEN"
    assert calls[0][1].status == "matched"


def test_apprise_adapter_does_not_raise_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "chainwake.output.adapters.apprise_dispatch",
        lambda _uri, _payload: False,
    )
    adapter = AppriseAdapter(uri="bad://x")
    # Must not raise
    adapter.dispatch(_matched())


def test_apprise_bridge_dispatch_is_exception_safe() -> None:
    """apprise_bridge.dispatch never raises; the adapter relies on that contract."""
    mock_app = MagicMock()
    mock_app.add.return_value = True
    mock_app.notify.side_effect = RuntimeError("network down")

    with patch("chainwake.output.apprise_bridge.apprise.Apprise", return_value=mock_app):
        result = bridge_dispatch("discord://1234/TOKEN", _matched())

    assert result is False


def test_apprise_adapter_should_not_exit() -> None:
    adapter = AppriseAdapter(uri="discord://1234/TOKEN")
    assert adapter.should_exit_after_dispatch is False


def test_apprise_adapter_close_is_noop() -> None:
    adapter = AppriseAdapter(uri="discord://1234/TOKEN")
    adapter.close()  # must not raise


def test_apprise_adapter_multiple_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatch_count = [0]

    def _fake(uri: str, payload: Payload) -> bool:
        dispatch_count[0] += 1
        return True

    monkeypatch.setattr("chainwake.output.adapters.apprise_dispatch", _fake)

    adapter = AppriseAdapter(uri="discord://1234/TOKEN")
    for _ in range(5):
        adapter.dispatch(_matched())

    assert dispatch_count[0] == 5


# ---------------------------------------------------------------------------
# FileAdapter — NDJSON guarantees
# ---------------------------------------------------------------------------


def test_file_adapter_append_only(tmp_path: Path) -> None:
    target = tmp_path / "log.jsonl"
    adapter = FileAdapter(target)
    adapter.dispatch(_matched())
    adapter.dispatch(_timeout())
    adapter.close()

    lines = target.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["status"] == "matched"
    assert json.loads(lines[1])["status"] == "timeout"


def test_file_adapter_each_line_is_compact_json(tmp_path: Path) -> None:
    target = tmp_path / "log.jsonl"
    adapter = FileAdapter(target)
    adapter.dispatch(_matched())
    adapter.close()

    line = target.read_text().strip()
    # No embedded newlines (NDJSON = one object per line)
    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed["status"] == "matched"


def test_file_adapter_parent_dir_created(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c" / "log.jsonl"
    assert not target.parent.exists()
    adapter = FileAdapter(target)
    adapter.dispatch(_matched())
    adapter.close()
    assert target.exists()


def test_file_adapter_survives_reopen(tmp_path: Path) -> None:
    """Closing and reopening appends, doesn't truncate."""
    target = tmp_path / "log.jsonl"
    a = FileAdapter(target)
    a.dispatch(_matched())
    a.close()

    b = FileAdapter(target)
    b.dispatch(_timeout())
    b.close()

    lines = target.read_text().splitlines()
    assert len(lines) == 2


def test_file_adapter_close_idempotent(tmp_path: Path) -> None:
    adapter = FileAdapter(tmp_path / "log.jsonl")
    adapter.dispatch(_matched())
    adapter.close()
    adapter.close()  # Must not raise


def test_file_adapter_close_before_dispatch(tmp_path: Path) -> None:
    """close() before any dispatch does not raise."""
    adapter = FileAdapter(tmp_path / "log.jsonl")
    adapter.close()


def test_file_adapter_multi_write_integrity(tmp_path: Path) -> None:
    """All records are valid JSON and contain expected fields."""
    target = tmp_path / "log.jsonl"
    adapter = FileAdapter(target)
    payloads = [_matched(), _timeout(), _matched(), _timeout()]
    for p in payloads:
        adapter.dispatch(p)
    adapter.close()

    lines = target.read_text().splitlines()
    assert len(lines) == 4
    for i, line in enumerate(lines):
        parsed = json.loads(line)
        assert "status" in parsed
        assert parsed["status"] == payloads[i].status  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# StreamAdapter
# ---------------------------------------------------------------------------


def test_stream_adapter_ndjson_two_payloads() -> None:
    sink = io.StringIO()
    adapter = StreamAdapter(stream=sink)
    adapter.dispatch(_matched())
    adapter.dispatch(_timeout())
    lines = sink.getvalue().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["status"] == "matched"
    assert json.loads(lines[1])["status"] == "timeout"


def test_stream_adapter_no_embedded_newlines() -> None:
    sink = io.StringIO()
    StreamAdapter(stream=sink).dispatch(_matched())
    line = sink.getvalue().strip()
    assert "\n" not in line


def test_stream_adapter_keys_sorted() -> None:
    sink = io.StringIO()
    StreamAdapter(stream=sink).dispatch(_matched())
    raw = sink.getvalue().strip()
    keys = list(json.loads(raw).keys())
    assert keys == sorted(keys)


def test_stream_adapter_should_not_exit() -> None:
    assert StreamAdapter().should_exit_after_dispatch is False


# ---------------------------------------------------------------------------
# DefaultAdapter
# ---------------------------------------------------------------------------


def test_default_adapter_indented_json() -> None:
    sink = io.StringIO()
    DefaultAdapter(stream=sink).dispatch(_matched())
    raw = sink.getvalue()
    assert "\n" in raw
    assert json.loads(raw)["status"] == "matched"


def test_default_adapter_should_exit() -> None:
    assert DefaultAdapter().should_exit_after_dispatch is True


# ---------------------------------------------------------------------------
# DefaultAdapter — render-mode switch
# ---------------------------------------------------------------------------


class _FakeTTYStream(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_default_adapter_human_mode_writes_prose() -> None:
    sink = io.StringIO()
    DefaultAdapter(stream=sink, render_mode=RenderMode.HUMAN).dispatch(_matched())
    raw = sink.getvalue().strip()
    assert raw.startswith("matched: subnet 19 pool.price")
    # Must not be JSON.
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)


def test_default_adapter_json_mode_writes_json_on_tty() -> None:
    sink = _FakeTTYStream()
    DefaultAdapter(stream=sink, render_mode=RenderMode.JSON).dispatch(_matched())
    assert json.loads(sink.getvalue().strip())["status"] == "matched"


def test_default_adapter_auto_with_tty_stream_writes_prose() -> None:
    sink = _FakeTTYStream()
    DefaultAdapter(stream=sink, render_mode=RenderMode.AUTO).dispatch(_matched())
    raw = sink.getvalue().strip()
    assert raw.startswith("matched: ")


def test_default_adapter_auto_with_pipe_stream_writes_json() -> None:
    # io.StringIO.isatty() returns False — AUTO must pick JSON.
    sink = io.StringIO()
    DefaultAdapter(stream=sink, render_mode=RenderMode.AUTO).dispatch(_matched())
    assert json.loads(sink.getvalue().strip())["status"] == "matched"


def test_default_adapter_default_render_mode_is_auto() -> None:
    assert DefaultAdapter().render_mode is RenderMode.AUTO


# ---------------------------------------------------------------------------
# Multi-adapter dispatch — independence
# ---------------------------------------------------------------------------


def test_multi_adapter_each_receives_same_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apprise_calls: list[Payload] = []
    monkeypatch.setattr(
        "chainwake.output.adapters.apprise_dispatch",
        lambda _uri, payload: apprise_calls.append(payload) or True,
    )

    stream_sink = io.StringIO()
    file_target = tmp_path / "log.jsonl"
    adapters = [
        StreamAdapter(stream=stream_sink),
        FileAdapter(file_target),
        AppriseAdapter(uri="discord://1234/TOKEN"),
    ]
    payload = _matched()
    for a in adapters:
        a.dispatch(payload)
    for a in adapters:
        a.close()

    # Stream got it
    assert json.loads(stream_sink.getvalue().strip())["status"] == "matched"
    # File got it
    assert json.loads(file_target.read_text().strip())["status"] == "matched"
    # Apprise got it
    assert len(apprise_calls) == 1
    assert apprise_calls[0].status == "matched"


def test_multi_adapter_file_and_stream_independent(tmp_path: Path) -> None:
    stream_sink = io.StringIO()
    file_target = tmp_path / "log.jsonl"
    adapters = [StreamAdapter(stream=stream_sink), FileAdapter(file_target)]
    for p in [_matched(), _timeout(), _matched()]:
        for a in adapters:
            a.dispatch(p)
    for a in adapters:
        a.close()

    stream_lines = stream_sink.getvalue().splitlines()
    file_lines = file_target.read_text().splitlines()
    assert len(stream_lines) == 3
    assert len(file_lines) == 3
    assert stream_lines == file_lines


# ---------------------------------------------------------------------------
# parse_adapter_uri
# ---------------------------------------------------------------------------


def test_parse_stream() -> None:
    assert isinstance(parse_adapter_uri("stream"), StreamAdapter)


def test_parse_file(tmp_path: Path) -> None:
    adapter = parse_adapter_uri(f"file://{tmp_path}/log.jsonl")
    assert isinstance(adapter, FileAdapter)
    assert adapter.path == tmp_path / "log.jsonl"


def test_parse_apprise_uri() -> None:
    adapter = parse_adapter_uri("discord://1234/TOKEN")
    assert isinstance(adapter, AppriseAdapter)
    assert adapter.uri == "discord://1234/TOKEN"


def test_parse_file_requires_path() -> None:
    with pytest.raises(ValueError, match="file:// URI requires a path"):
        parse_adapter_uri("file://")
