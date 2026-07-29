"""Executable ``max_ru`` descriptions match the implemented metering."""

from __future__ import annotations

from chainwake.cli.chains.bittensor import _MAX_RU_PARAM
from chainwake.mcp.tools import build_tools


def test_cli_calls_max_ru_a_registry_estimated_observation_budget() -> None:
    help_text = str(_MAX_RU_PARAM.help).lower()

    assert "registry-estimated observation budget" in help_text
    assert "provider billing cap" in help_text
    assert "bootstrap" in help_text
    assert "sdk" in help_text


def test_every_mcp_max_ru_field_disclaims_provider_billing() -> None:
    for tool in build_tools("bt"):
        description = (
            tool.inputSchema.get("properties", {}).get("max_ru", {}).get("description", "").lower()
        )
        assert "registry-estimated observation budget" in description, tool.name
        assert "not a provider billing cap" in description, tool.name
