"""Unit tests for chainwake.output.render — RenderMode + TTY detection."""

from __future__ import annotations

import io
import json as _json
import sys
from typing import Any
from unittest.mock import patch

import pytest

from chainwake.output.render import (
    RENDER_MODE_ENV_VAR,
    RenderMode,
    emit_user_error,
    render_mode_from_env,
    resolve_render_mode,
)

pytestmark = pytest.mark.unit


class _FakeTTY(io.StringIO):
    """Stand-in stdout that reports as a TTY."""

    def isatty(self) -> bool:
        return True


class _FakePipe(io.StringIO):
    """Stand-in stdout that reports as a non-TTY (pipe / redirect)."""

    def isatty(self) -> bool:
        return False


class TestRenderModeEnum:
    def test_values(self) -> None:
        assert RenderMode.AUTO.value == "auto"
        assert RenderMode.JSON.value == "json"
        assert RenderMode.HUMAN.value == "human"

    def test_is_str_enum(self) -> None:
        # StrEnum → instances compare equal to their string values, which is
        # convenient for cyclopts/CLI-string round-trips.
        assert RenderMode.JSON == "json"
        assert RenderMode.HUMAN == "human"


class TestResolveRenderMode:
    def test_auto_with_tty_returns_human(self) -> None:
        with patch.object(sys, "stdout", _FakeTTY()):
            assert resolve_render_mode(RenderMode.AUTO) is RenderMode.HUMAN

    def test_auto_with_pipe_returns_json(self) -> None:
        with patch.object(sys, "stdout", _FakePipe()):
            assert resolve_render_mode(RenderMode.AUTO) is RenderMode.JSON

    def test_explicit_json_overrides_tty(self) -> None:
        with patch.object(sys, "stdout", _FakeTTY()):
            assert resolve_render_mode(RenderMode.JSON) is RenderMode.JSON

    def test_explicit_human_overrides_pipe(self) -> None:
        with patch.object(sys, "stdout", _FakePipe()):
            assert resolve_render_mode(RenderMode.HUMAN) is RenderMode.HUMAN

    def test_default_argument_is_auto(self) -> None:
        with patch.object(sys, "stdout", _FakePipe()):
            # No argument supplied → AUTO branch fires.
            assert resolve_render_mode() is RenderMode.JSON
        with patch.object(sys, "stdout", _FakeTTY()):
            assert resolve_render_mode() is RenderMode.HUMAN

    def test_never_returns_auto(self) -> None:
        for stdout in (_FakeTTY(), _FakePipe()):
            with patch.object(sys, "stdout", stdout):
                assert resolve_render_mode(RenderMode.AUTO) is not RenderMode.AUTO


class TestRenderModeFromEnv:
    def test_unset_returns_auto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(RENDER_MODE_ENV_VAR, raising=False)
        assert render_mode_from_env() is RenderMode.AUTO

    def test_json_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(RENDER_MODE_ENV_VAR, "json")
        assert render_mode_from_env() is RenderMode.JSON

    def test_human_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(RENDER_MODE_ENV_VAR, "human")
        assert render_mode_from_env() is RenderMode.HUMAN

    def test_uppercase_normalised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(RENDER_MODE_ENV_VAR, "JSON")
        assert render_mode_from_env() is RenderMode.JSON

    def test_whitespace_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(RENDER_MODE_ENV_VAR, " human ")
        assert render_mode_from_env() is RenderMode.HUMAN

    def test_unknown_value_falls_back_to_auto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(RENDER_MODE_ENV_VAR, "yaml")
        assert render_mode_from_env() is RenderMode.AUTO

    def test_empty_string_falls_back_to_auto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(RENDER_MODE_ENV_VAR, "")
        assert render_mode_from_env() is RenderMode.AUTO


def _capture_emit(reason: str, message: str) -> str:
    """Call ``emit_user_error`` and return captured stdout."""
    buffer = io.StringIO()
    with patch.object(sys, "stdout", buffer):
        emit_user_error(reason, message)
    return buffer.getvalue()


class TestEmitUserError:
    """Shared user_error emitter honours render mode the same as runtime."""

    def test_human_env_var_emits_prose(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(RENDER_MODE_ENV_VAR, "human")
        out = _capture_emit("invalid_input", "missing flag --foo")
        assert "{" not in out
        assert out.strip() == "error: missing flag --foo"

    def test_json_env_var_emits_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(RENDER_MODE_ENV_VAR, "json")
        out = _capture_emit("invalid_input", "missing flag --foo")
        payload: dict[str, Any] = _json.loads(out)
        assert payload["status"] == "user_error"
        assert payload["reason"] == "invalid_input"
        assert payload["message"] == "missing flag --foo"

    def test_pipe_no_env_var_falls_back_to_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Captured stdout in pytest is a non-TTY → AUTO must resolve to JSON.
        monkeypatch.delenv(RENDER_MODE_ENV_VAR, raising=False)
        out = _capture_emit("invalid_input", "any message")
        payload: dict[str, Any] = _json.loads(out)
        assert payload["status"] == "user_error"

    def test_custom_reason_round_trips_in_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(RENDER_MODE_ENV_VAR, "json")
        out = _capture_emit("custom_reason", "x")
        assert _json.loads(out)["reason"] == "custom_reason"


class TestProviderErrorHints:
    """Human-mode provider_error rendering surfaces actionable recovery hints."""

    @staticmethod
    def _make(reason: str, message: str) -> Any:
        from datetime import UTC, datetime  # noqa: PLC0415
        from typing import Literal, cast  # noqa: PLC0415

        from chainwake.output.schema import (  # noqa: PLC0415
            Budget,
            Process,
            ProviderErrorPayload,
        )

        provider_error_reason_t = Literal[
            "auth_failed", "rpc_unreachable", "rate_limited", "subscription_failed", "decode_failed"
        ]
        return ProviderErrorPayload(
            watcher=None,
            condition=None,
            budget=Budget(runtime_ms=0, rpc_calls=0, estimated_ru_consumed=0),
            process=Process(pid=1, started_at=datetime.now(UTC)),
            message=message,
            reason=cast("provider_error_reason_t", reason),
        )

    def test_rate_limited_hint_mentions_retry_and_blockmachine_upgrade(self) -> None:
        from chainwake.output.render import render_human  # noqa: PLC0415

        out = render_human(self._make("rate_limited", "rate limit exceeded"))
        assert "rate limit exceeded" in out
        assert "rate-limited" in out.lower()
        assert "retrying" in out.lower()
        assert "blockmachine.io" in out
        assert "--api-key" in out

    def test_rpc_unreachable_hint_mentions_url_and_network(self) -> None:
        from chainwake.output.render import render_human  # noqa: PLC0415

        out = render_human(self._make("rpc_unreachable", "ConnectionError"))
        assert "--rpc-url" in out
        assert "network" in out.lower()
