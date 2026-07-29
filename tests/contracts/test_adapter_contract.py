"""Adapter contract test pack.

Every Adapter — DefaultAdapter, StreamAdapter, FileAdapter, AppriseAdapter —
must satisfy the same contract: dispatch a payload, perform the documented
side effect, honour `should_exit_after_dispatch`, and tear down via `close()`.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from chainwake.output.adapters import (
    Adapter,
    AppriseAdapter,
    DefaultAdapter,
    FileAdapter,
    StreamAdapter,
    parse_adapter_uri,
)
from chainwake.output.schema import (
    Budget,
    DeltaCondition,
    MatchedPayload,
    ObservedDelta,
    Payload,
    Process,
    Watcher,
    Window,
)

pytestmark = pytest.mark.unit


def _ts() -> datetime:
    return datetime(2026, 5, 5, 19, 42, 10, tzinfo=UTC)


def _payload() -> Payload:
    return MatchedPayload(
        watcher=Watcher(
            chain="bt",
            resource="subnet",
            resource_id="19",
            sub_resource="pool.price",
            primitive="delta",
            invocation=["chainwake", "bt", "subnet", "19", "price"],
        ),
        condition=DeltaCondition(
            operator="drop-pct",
            target=5.0,
            window=Window(unit="time", value="1h"),
        ),
        observed=ObservedDelta(
            path="subnet.19.pool.price",
            value=0.0432,
            previous_value=0.0455,
            delta=-0.0023,
            delta_pct=-5.05,
            block=8_119_123,
            block_hash="0xabc",
            timestamp=_ts(),
        ),
        budget=Budget(runtime_ms=10, rpc_calls=1, estimated_ru_consumed=1),
        process=Process(pid=1, started_at=_ts()),
    )


def test_runtime_checkable_protocol_recognises_all_impls(tmp_path: Path) -> None:
    assert isinstance(DefaultAdapter(), Adapter)
    assert isinstance(StreamAdapter(), Adapter)
    assert isinstance(FileAdapter(tmp_path / "example"), Adapter)
    assert isinstance(AppriseAdapter(uri="tgram://x/y"), Adapter)


def test_default_adapter_writes_indented_json_and_signals_exit() -> None:
    sink = io.StringIO()
    adapter = DefaultAdapter(stream=sink)
    payload = _payload()
    adapter.dispatch(payload)
    raw = sink.getvalue()
    assert "\n" in raw
    parsed = json.loads(raw)
    assert parsed["status"] == "matched"
    assert adapter.should_exit_after_dispatch is True
    adapter.close()


def test_stream_adapter_writes_ndjson_and_keeps_alive() -> None:
    sink = io.StringIO()
    adapter = StreamAdapter(stream=sink)
    payload = _payload()
    adapter.dispatch(payload)
    adapter.dispatch(payload)
    lines = sink.getvalue().splitlines()
    assert len(lines) == 2
    for line in lines:
        parsed = json.loads(line)
        assert parsed["status"] == "matched"
    assert adapter.should_exit_after_dispatch is False
    adapter.close()


def test_file_adapter_appends_ndjson(tmp_path: Path) -> None:
    target = tmp_path / "log.jsonl"
    adapter = FileAdapter(target)
    adapter.dispatch(_payload())
    adapter.dispatch(_payload())
    adapter.close()

    lines = target.read_text().splitlines()
    assert len(lines) == 2
    for line in lines:
        assert json.loads(line)["status"] == "matched"


def test_file_adapter_creates_parent_directories(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper" / "log.jsonl"
    adapter = FileAdapter(target)
    adapter.dispatch(_payload())
    adapter.close()
    assert target.exists()


def test_file_adapter_close_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "log.jsonl"
    adapter = FileAdapter(target)
    adapter.dispatch(_payload())
    adapter.close()
    adapter.close()


def test_apprise_adapter_dispatches_and_keeps_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatched: list[tuple[str, object]] = []

    def _fake_dispatch(uri: str, payload: object) -> bool:
        dispatched.append((uri, payload))
        return True

    monkeypatch.setattr("chainwake.output.adapters.apprise_dispatch", _fake_dispatch)

    adapter = AppriseAdapter(uri="tgram://bot/chat")
    adapter.dispatch(_payload())
    assert len(dispatched) == 1
    assert dispatched[0][0] == "tgram://bot/chat"
    assert adapter.should_exit_after_dispatch is False


def test_parse_adapter_uri_stream() -> None:
    adapter = parse_adapter_uri("stream")
    assert isinstance(adapter, StreamAdapter)


def test_parse_adapter_uri_file(tmp_path: Path) -> None:
    target = tmp_path / "log.jsonl"
    adapter = parse_adapter_uri(f"file://{target}")
    assert isinstance(adapter, FileAdapter)
    assert adapter.path == target


def test_parse_adapter_uri_file_requires_path() -> None:
    with pytest.raises(ValueError, match="file:// URI requires a path"):
        parse_adapter_uri("file://")


def test_parse_adapter_uri_apprise_default() -> None:
    adapter = parse_adapter_uri("tgram://bot/chat")
    assert isinstance(adapter, AppriseAdapter)
    assert adapter.uri == "tgram://bot/chat"


def test_apprise_adapter_instances_are_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatched: list[str] = []

    def _fake_dispatch(uri: str, payload: object) -> bool:
        dispatched.append(uri)
        return True

    monkeypatch.setattr("chainwake.output.adapters.apprise_dispatch", _fake_dispatch)

    a = AppriseAdapter(uri="tgram://x/y")
    b = AppriseAdapter(uri="discord://1234/TOKEN")
    a.dispatch(_payload())
    assert dispatched == ["tgram://x/y"]
    b.dispatch(_payload())
    assert dispatched == ["tgram://x/y", "discord://1234/TOKEN"]


def test_payload_serialisation_is_stable() -> None:
    sink = io.StringIO()
    adapter = StreamAdapter(stream=sink)
    payload = _payload()
    adapter.dispatch(payload)
    adapter.dispatch(payload)
    adapter.close()
    a, b = sink.getvalue().splitlines()
    assert a == b


def _all_adapters(tmp_path: Path) -> list[Adapter]:
    return [
        DefaultAdapter(stream=io.StringIO()),
        StreamAdapter(stream=io.StringIO()),
        FileAdapter(tmp_path / "log.jsonl"),
        AppriseAdapter(uri="tgram://bot/chat"),
    ]


@pytest.mark.parametrize(
    "adapter_name",
    ["default", "stream", "file", "apprise"],
)
def test_every_adapter_dispatches_without_error(adapter_name: str, tmp_path: Path) -> None:
    adapters = _all_adapters(tmp_path)
    by_name = {a.name: a for a in adapters}
    adapter = by_name[adapter_name]
    adapter.dispatch(_payload())
    adapter.close()
