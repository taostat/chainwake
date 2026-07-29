"""Unit tests for ``_emit_user_error`` render-mode handling.

Parse-time validation errors in ``chainwake bt`` must honour the same
render mode (``--json`` / ``--no-json`` / TTY auto-detect) as runtime
errors. A bare ``print(json.dumps(...))`` regardless of mode is a bug:
human-mode callers want prose, not a 13-line JSON blob.

These tests exercise the bittensor CLI's ``_emit_user_error`` thin
wrapper (which delegates to :func:`chainwake.output.render.emit_user_error`),
asserting that the env-var-driven render mode is honoured and that the
TTY fallback produces JSON when the env var is unset (the test default,
since pytest's captured stdout is not a TTY).
"""

from __future__ import annotations

import json as _json
from io import StringIO
from typing import Any
from unittest.mock import patch

import pytest

from chainwake.cli.chains import bittensor as bt_module
from chainwake.output.render import RENDER_MODE_ENV_VAR

pytestmark = pytest.mark.unit


def _capture_emit(reason: str, message: str) -> tuple[int, str]:
    """Call ``_emit_user_error`` and return ``(exit_code, stdout)``."""
    buffer = StringIO()
    with patch("sys.stdout", buffer):
        code = bt_module._emit_user_error(reason, message)
    return code, buffer.getvalue()


class TestEmitUserErrorRenderMode:
    def test_human_env_var_emits_prose(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(RENDER_MODE_ENV_VAR, "human")
        code, stdout = _capture_emit("invalid_input", "missing flag --foo")
        assert code == 2
        # Prose form: "error: <message>" — no JSON braces.
        assert "{" not in stdout
        assert "}" not in stdout
        assert "error: missing flag --foo" in stdout

    def test_json_env_var_emits_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(RENDER_MODE_ENV_VAR, "json")
        code, stdout = _capture_emit("invalid_input", "missing flag --foo")
        assert code == 2
        # JSON form: must include opening and closing braces and the status field.
        assert "{" in stdout
        assert "}" in stdout
        assert '"status": "user_error"' in stdout
        assert '"message": "missing flag --foo"' in stdout

    def test_no_env_var_non_tty_falls_back_to_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Unset the env var; pytest's captured stdout is not a TTY by default,
        # so the AUTO path must resolve to JSON.
        monkeypatch.delenv(RENDER_MODE_ENV_VAR, raising=False)
        code, stdout = _capture_emit("invalid_input", "missing flag --foo")
        assert code == 2
        assert "{" in stdout
        assert '"status": "user_error"' in stdout


class TestEmitUserErrorPayloadShape:
    def test_human_message_includes_reason_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Human prose surfaces the message verbatim."""
        monkeypatch.setenv(RENDER_MODE_ENV_VAR, "human")
        _, stdout = _capture_emit("invalid_input", "one of --below or --above is required")
        assert "one of --below or --above is required" in stdout

    def test_json_payload_carries_reason_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """JSON payload always carries the reason for agents."""
        monkeypatch.setenv(RENDER_MODE_ENV_VAR, "json")
        _, stdout = _capture_emit("custom_reason", "some message")
        parsed: dict[str, Any] = _json.loads(stdout)
        assert parsed["status"] == "user_error"
        assert parsed["reason"] == "custom_reason"
        assert parsed["message"] == "some message"
