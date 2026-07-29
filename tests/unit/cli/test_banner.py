"""Tests for the bare-command Blockmachine Chainwake welcome banner."""

from __future__ import annotations

import io
import subprocess
import sys
from unittest.mock import patch

import pytest

from chainwake.cli.banner import render_banner

pytestmark = pytest.mark.unit


class _TerminalBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_bare_command_prints_static_brand_before_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "chainwake"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "BLOCKMACHINE // CHAINWAKE" in result.stdout
    assert "Bittensor and EVM chain monitoring for AI agents" in result.stdout
    assert "| BM |" not in result.stdout
    assert "==========" not in result.stdout
    assert "\x1b[" not in result.stdout
    assert result.stdout.index("BLOCKMACHINE // CHAINWAKE") < result.stdout.index(
        "Usage: chainwake"
    )


def test_redirected_banner_is_stable_and_instant() -> None:
    output = io.StringIO()

    with patch("time.sleep") as sleep:
        render_banner(output)

    sleep.assert_not_called()
    assert "\r" not in output.getvalue()
    assert "\x1b[" not in output.getvalue()
    assert "BLOCKMACHINE // CHAINWAKE" in output.getvalue()
    assert "| BM |" not in output.getvalue()


def test_terminal_banner_is_also_static() -> None:
    output = _TerminalBuffer()

    with patch("time.sleep") as sleep:
        render_banner(output)

    sleep.assert_not_called()
    assert "\x1b[" not in output.getvalue()
    assert "| BM |" not in output.getvalue()
    assert output.getvalue().endswith("  Bittensor and EVM chain monitoring for AI agents\n\n")
