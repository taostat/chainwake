"""Unit tests for chainwake.output.apprise_bridge.

apprise.Apprise is mocked throughout — no live notifications are sent.
Tests cover subject/body formatting for every payload status, the notify_type
mapping, failure handling, and the async wrapper.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import apprise
import pytest

from chainwake.output.apprise_bridge import (
    SUBJECT_PREFIX,
    _build_body,
    _build_subject,
    _notify_type_for,
    _safe_uri,
    async_dispatch,
    dispatch,
)
from chainwake.output.schema import (
    Budget,
    BudgetExhaustedPayload,
    DeltaCondition,
    EventCondition,
    InternalErrorPayload,
    LivenessCondition,
    MatchedPayload,
    ObservedDelta,
    ObservedEvent,
    ObservedLiveness,
    ObservedState,
    ObservedThreshold,
    ObservedTx,
    Process,
    ProviderErrorPayload,
    StateCondition,
    ThresholdCondition,
    TimeoutPayload,
    TxCondition,
    UserErrorPayload,
    Watcher,
    Window,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _ts() -> datetime:
    return datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)


def _watcher(**kwargs: Any) -> Watcher:
    defaults: dict[str, Any] = {
        "chain": "bt",
        "resource": "subnet",
        "resource_id": "19",
        "primitive": "threshold",
        "invocation": ["chainwake", "bt", "subnet", "19", "price"],
    }
    defaults.update(kwargs)
    return Watcher(**defaults)


def _budget() -> Budget:
    return Budget(runtime_ms=100, rpc_calls=5, estimated_ru_consumed=10)


def _process() -> Process:
    return Process(pid=1234, started_at=_ts())


def _matched_threshold() -> MatchedPayload:
    return MatchedPayload(
        watcher=_watcher(sub_resource="pool.price"),
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


def _matched_delta() -> MatchedPayload:
    return MatchedPayload(
        watcher=_watcher(sub_resource="pool.price"),
        condition=DeltaCondition(
            operator="drop-pct", target=5.0, window=Window(unit="time", value="1h")
        ),
        observed=ObservedDelta(
            path="subnet.19.pool.price",
            value=0.043,
            previous_value=0.045,
            delta=-0.002,
            delta_pct=-4.4,
            block=101,
            block_hash="0xdef",
            timestamp=_ts(),
        ),
        budget=_budget(),
        process=_process(),
    )


def _matched_state() -> MatchedPayload:
    return MatchedPayload(
        watcher=_watcher(resource="network", primitive="state"),
        condition=StateCondition(operator="on-change"),
        observed=ObservedState(
            path="network.immunity_period",
            value=200,
            previous_value=100,
            block=200,
            block_hash="0x111",
            timestamp=_ts(),
        ),
        budget=_budget(),
        process=_process(),
    )


def _matched_liveness() -> MatchedPayload:
    return MatchedPayload(
        watcher=_watcher(resource="validator", primitive="liveness"),
        condition=LivenessCondition(operator="silent-for", duration="3epochs"),
        observed=ObservedLiveness(
            path="validator.hotkey.last_update",
            last_seen_block=50,
            last_seen_timestamp=_ts(),
            elapsed="3epochs",
            block=200,
            block_hash="0x222",
            timestamp=_ts(),
        ),
        budget=_budget(),
        process=_process(),
    )


def _matched_event() -> MatchedPayload:
    return MatchedPayload(
        watcher=_watcher(resource="event", primitive="event"),
        condition=EventCondition(event_type="SubnetRegistered"),
        observed=ObservedEvent(
            event_type="SubnetRegistered",
            raw_event="...",
            args={"netuid": 42},
            block=300,
            block_hash="0x333",
            timestamp=_ts(),
        ),
        budget=_budget(),
        process=_process(),
    )


def _matched_tx() -> MatchedPayload:
    return MatchedPayload(
        watcher=_watcher(resource="tx", resource_id="0xdeadbeef", primitive="tx"),
        condition=TxCondition(finality="finalized"),
        observed=ObservedTx(
            tx_hash="0xdeadbeef",
            finality="finalized",
            block=400,
            block_hash="0x444",
            timestamp=_ts(),
        ),
        budget=_budget(),
        process=_process(),
    )


def _timeout() -> TimeoutPayload:
    return TimeoutPayload(
        watcher=_watcher(),
        condition=ThresholdCondition(operator="below", target=0.05),
        reason="max_runtime_reached",
        budget=_budget(),
        process=_process(),
    )


def _budget_exhausted() -> BudgetExhaustedPayload:
    return BudgetExhaustedPayload(
        watcher=_watcher(),
        condition=ThresholdCondition(operator="below", target=0.05),
        reason="max_ru_reached",
        budget=_budget(),
        process=_process(),
    )


def _user_error() -> UserErrorPayload:
    return UserErrorPayload(
        budget=_budget(),
        process=_process(),
        reason="invalid_observable",
        message="Unknown observable path",
    )


def _provider_error() -> ProviderErrorPayload:
    return ProviderErrorPayload(
        budget=_budget(),
        process=_process(),
        reason="rpc_unreachable",
        message="Connection refused",
    )


def _internal_error() -> InternalErrorPayload:
    return InternalErrorPayload(
        budget=_budget(),
        process=_process(),
        reason="unexpected_exception",
        message="Something went wrong",
    )


# ---------------------------------------------------------------------------
# Subject formatting
# ---------------------------------------------------------------------------


def test_subject_matched_threshold() -> None:
    s = _build_subject(_matched_threshold())
    assert s == f"{SUBJECT_PREFIX} subnet.19.pool.price matched"


def test_subject_matched_delta() -> None:
    s = _build_subject(_matched_delta())
    assert s == f"{SUBJECT_PREFIX} subnet.19.pool.price matched"


def test_subject_matched_state() -> None:
    s = _build_subject(_matched_state())
    assert s == f"{SUBJECT_PREFIX} network.immunity_period matched"


def test_subject_matched_liveness() -> None:
    s = _build_subject(_matched_liveness())
    assert s == f"{SUBJECT_PREFIX} validator.hotkey.last_update matched"


def test_subject_matched_event_uses_event_type() -> None:
    s = _build_subject(_matched_event())
    assert s == f"{SUBJECT_PREFIX} SubnetRegistered matched"


def test_subject_matched_tx_uses_hash() -> None:
    s = _build_subject(_matched_tx())
    assert s == f"{SUBJECT_PREFIX} 0xdeadbeef matched"


def test_subject_timeout() -> None:
    s = _build_subject(_timeout())
    assert s == f"{SUBJECT_PREFIX} timeout — max_runtime_reached"


def test_subject_budget_exhausted() -> None:
    s = _build_subject(_budget_exhausted())
    assert s == f"{SUBJECT_PREFIX} budget_exhausted — max_ru_reached"


def test_subject_user_error() -> None:
    s = _build_subject(_user_error())
    assert s == f"{SUBJECT_PREFIX} user_error — invalid_observable"


def test_subject_provider_error() -> None:
    s = _build_subject(_provider_error())
    assert s == f"{SUBJECT_PREFIX} provider_error — rpc_unreachable"


def test_subject_internal_error() -> None:
    s = _build_subject(_internal_error())
    assert s == f"{SUBJECT_PREFIX} internal_error — unexpected_exception"


# ---------------------------------------------------------------------------
# Body formatting
# ---------------------------------------------------------------------------


def test_body_is_valid_json() -> None:
    body = _build_body(_matched_threshold())
    parsed = json.loads(body)
    assert parsed["status"] == "matched"


def test_body_is_pretty_printed() -> None:
    body = _build_body(_matched_threshold())
    assert "\n" in body


def test_body_contains_all_payload_fields() -> None:
    body = _build_body(_matched_threshold())
    parsed = json.loads(body)
    assert "watcher" in parsed
    assert "condition" in parsed
    assert "observed" in parsed
    assert "budget" in parsed
    assert "process" in parsed


# ---------------------------------------------------------------------------
# notify_type mapping
# ---------------------------------------------------------------------------


def test_notify_type_matched_is_success() -> None:
    assert _notify_type_for(_matched_threshold()) == apprise.NotifyType.SUCCESS


def test_notify_type_timeout_is_warning() -> None:
    assert _notify_type_for(_timeout()) == apprise.NotifyType.WARNING


def test_notify_type_budget_exhausted_is_warning() -> None:
    assert _notify_type_for(_budget_exhausted()) == apprise.NotifyType.WARNING


def test_notify_type_user_error_is_failure() -> None:
    assert _notify_type_for(_user_error()) == apprise.NotifyType.FAILURE


def test_notify_type_provider_error_is_failure() -> None:
    assert _notify_type_for(_provider_error()) == apprise.NotifyType.FAILURE


def test_notify_type_internal_error_is_failure() -> None:
    assert _notify_type_for(_internal_error()) == apprise.NotifyType.FAILURE


# ---------------------------------------------------------------------------
# _safe_uri — credential redaction
# ---------------------------------------------------------------------------


def test_safe_uri_redacts_tgram_credentials() -> None:
    safe = _safe_uri("tgram://bottoken123/chatid456")
    assert "bottoken123" not in safe
    assert "chatid456" not in safe
    assert safe.startswith("tgram://")


def test_safe_uri_redacts_discord_credentials() -> None:
    safe = _safe_uri("discord://1234567890/supersecrettoken")
    assert "supersecrettoken" not in safe
    assert safe.startswith("discord://")


def test_safe_uri_redacts_mailto_password() -> None:
    safe = _safe_uri("mailto://user:secretpass@smtp.example.com")
    assert "secretpass" not in safe
    assert safe.startswith("mailto://")


def test_safe_uri_unknown_scheme() -> None:
    safe = _safe_uri("notauri")
    assert safe == "<unknown-scheme>"


def test_safe_uri_preserves_scheme_only() -> None:
    safe = _safe_uri("discord://1234/TOKEN")
    assert safe == "discord://<redacted>"


# ---------------------------------------------------------------------------
# dispatch() — sync, apprise mocked
# ---------------------------------------------------------------------------


def _make_apprise_mock(add_returns: bool = True, notify_returns: bool = True) -> MagicMock:
    mock = MagicMock()
    mock.add.return_value = add_returns
    mock.notify.return_value = notify_returns
    return mock


def test_dispatch_calls_apprise_with_subject_body_type() -> None:
    mock_app = _make_apprise_mock()
    payload = _matched_threshold()

    with patch("chainwake.output.apprise_bridge.apprise.Apprise", return_value=mock_app):
        result = dispatch("discord://1234/TOKEN", payload)

    assert result is True
    mock_app.add.assert_called_once_with("discord://1234/TOKEN")
    call_kwargs = mock_app.notify.call_args
    assert SUBJECT_PREFIX in call_kwargs.kwargs["title"]
    assert json.loads(call_kwargs.kwargs["body"])["status"] == "matched"
    assert call_kwargs.kwargs["notify_type"] == apprise.NotifyType.SUCCESS


def test_dispatch_returns_false_when_add_fails() -> None:
    mock_app = _make_apprise_mock(add_returns=False)

    with patch("chainwake.output.apprise_bridge.apprise.Apprise", return_value=mock_app):
        result = dispatch("bad://x", _matched_threshold())

    assert result is False
    mock_app.notify.assert_not_called()


def test_dispatch_returns_false_when_notify_fails() -> None:
    mock_app = _make_apprise_mock(notify_returns=False)

    with patch("chainwake.output.apprise_bridge.apprise.Apprise", return_value=mock_app):
        result = dispatch("discord://1234/TOKEN", _matched_threshold())

    assert result is False


def test_dispatch_returns_false_on_apprise_exception() -> None:
    mock_app = _make_apprise_mock()
    mock_app.notify.side_effect = RuntimeError("network failure")

    with patch("chainwake.output.apprise_bridge.apprise.Apprise", return_value=mock_app):
        result = dispatch("discord://1234/TOKEN", _matched_threshold())

    assert result is False


def test_dispatch_does_not_raise_on_any_exception() -> None:
    with patch("chainwake.output.apprise_bridge.apprise.Apprise", side_effect=Exception("boom")):
        # Must not raise
        result = dispatch("discord://1234/TOKEN", _matched_threshold())
    assert result is False


@pytest.mark.parametrize(
    "make_payload",
    [
        _matched_threshold,
        _matched_event,
        _matched_tx,
        _timeout,
        _budget_exhausted,
        _user_error,
        _provider_error,
        _internal_error,
    ],
    ids=[
        "matched_threshold",
        "matched_event",
        "matched_tx",
        "timeout",
        "budget_exhausted",
        "user_error",
        "provider_error",
        "internal_error",
    ],
)
def test_dispatch_all_payload_statuses(make_payload: Any) -> None:
    mock_app = _make_apprise_mock()

    with patch("chainwake.output.apprise_bridge.apprise.Apprise", return_value=mock_app):
        result = dispatch("discord://1234/TOKEN", make_payload())

    assert result is True


# ---------------------------------------------------------------------------
# async_dispatch wrapper
# ---------------------------------------------------------------------------


def test_async_dispatch_calls_sync_dispatch() -> None:
    mock_app = _make_apprise_mock()
    payload = _matched_threshold()

    with patch("chainwake.output.apprise_bridge.apprise.Apprise", return_value=mock_app):
        result = asyncio.run(async_dispatch("discord://1234/TOKEN", payload))

    assert result is True
    mock_app.notify.assert_called_once()


def test_async_dispatch_propagates_false_on_failure() -> None:
    mock_app = _make_apprise_mock(add_returns=False)

    with patch("chainwake.output.apprise_bridge.apprise.Apprise", return_value=mock_app):
        result = asyncio.run(async_dispatch("bad://x", _matched_threshold()))

    assert result is False
