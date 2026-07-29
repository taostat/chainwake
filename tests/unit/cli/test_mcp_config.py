"""CLI contracts for copy-paste MCP client configuration."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parents[3]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "chainwake", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_hermes_config_is_copy_paste_yaml_with_anonymous_defaults() -> None:
    result = _run("mcp", "config", "hermes")

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout == (
        "mcp_servers:\n"
        "  chainwake:\n"
        "    command: chainwake\n"
        "    args:\n"
        "      - mcp\n"
        "      - serve\n"
        "      - --stdio\n"
        "      - --tool-timeout\n"
        "      - 24h\n"
        "    timeout: 90000\n"
    )
    assert "API_KEY" not in result.stdout


def test_openclaw_config_is_copy_paste_json_with_anonymous_defaults() -> None:
    result = _run("mcp", "config", "openclaw")

    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document == {
        "mcp": {
            "servers": {
                "chainwake": {
                    "command": "chainwake",
                    "args": ["mcp", "serve", "--stdio", "--tool-timeout", "24h"],
                    "requestTimeoutMs": 90_000_000,
                    "connectionTimeoutMs": 10_000,
                }
            }
        }
    }
    assert "API_KEY" not in result.stdout


def test_json_render_flag_does_not_change_mcp_config_helper_format() -> None:
    plain = _run("mcp", "config", "hermes")
    forced_json = _run("--json", "mcp", "config", "hermes")

    assert forced_json.returncode == 0, forced_json.stderr
    assert forced_json.stdout == plain.stdout
    assert forced_json.stdout.startswith("mcp_servers:\n")


@pytest.mark.parametrize("client", ["hermes", "openclaw"])
def test_config_accepts_executable_override(client: str) -> None:
    result = _run("mcp", "config", client, "--command", "/opt/chainwake/bin/chainwake")

    assert result.returncode == 0, result.stderr
    assert "/opt/chainwake/bin/chainwake" in result.stdout


def test_hermes_config_supports_three_day_monitor_with_client_grace() -> None:
    result = _run("mcp", "config", "hermes", "--tool-timeout", "3d")

    assert result.returncode == 0, result.stderr
    assert "      - --tool-timeout\n      - 3d\n" in result.stdout
    assert "    timeout: 262800\n" in result.stdout


def test_openclaw_config_supports_three_day_monitor_with_client_grace() -> None:
    result = _run("mcp", "config", "openclaw", "--tool-timeout", "3d")

    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    config = document["mcp"]["servers"]["chainwake"]
    assert config["args"][-2:] == ["--tool-timeout", "3d"]
    assert config["requestTimeoutMs"] == 262_800_000


def test_mcp_config_help_lists_supported_clients_and_timeout_guidance() -> None:
    result = _run("mcp", "config", "--help")

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "hermes" in combined.lower()
    assert "openclaw" in combined.lower()
    assert "max_runtime" in combined


def test_official_mcp_registry_metadata_matches_pypi() -> None:
    server = json.loads((ROOT / "server.json").read_text())

    assert server["$schema"] == (
        "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"
    )
    assert server["name"] == "io.github.taostat/chainwake"
    assert server["title"] == "Blockmachine Chainwake"
    assert server["repository"] == {
        "url": "https://github.com/taostat/chainwake",
        "source": "github",
    }
    assert server["version"] == "0.5.0"
    assert server["packages"] == [
        {
            "registryType": "pypi",
            "identifier": "chainwake",
            "version": "0.5.0",
            "transport": {"type": "stdio"},
            "packageArguments": [
                {"type": "positional", "value": "mcp"},
                {"type": "positional", "value": "serve"},
                {"type": "positional", "value": "--stdio"},
            ],
        }
    ]


def test_package_discovery_keywords_cover_brand_and_ownership() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()

    assert '"blockmachine"' in pyproject
    assert '"taostats"' in pyproject
    assert '"mcp"' in pyproject
