"""Ethereum MCP discovery and CLI translation contract."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from chainwake.core.registry import all_entries
from chainwake.mcp.exec import _build_argv
from chainwake.mcp.tools import build_tools

TX_HASH = f"0x{'ab' * 32}"


@pytest.mark.unit
def test_ethereum_mcp_catalogue_covers_every_ethereum_observable():
    tools = build_tools("eth")

    assert [tool.name for tool in tools] == [
        "chainwake_eth_network_base_fee",
        "chainwake_eth_token_price",
        "chainwake_eth_tx",
    ]
    assert {entry.path_template for entry in all_entries(chain="eth")} == {
        "network.base-fee",
        "token.{token}.price",
        "tx.{tx_hash}",
    }


@pytest.mark.unit
def test_ethereum_base_fee_tool_exposes_numeric_condition_and_common_limits():
    tool = next(
        tool for tool in build_tools("eth") if tool.name == "chainwake_eth_network_base_fee"
    )

    assert tool.description is not None
    assert "base fee" in tool.description.lower()
    assert "gwei" in tool.description.lower()
    assert "condition" in tool.inputSchema["required"]
    assert {"rpc_url", "name", "max_runtime", "max_ru"} <= set(tool.inputSchema["properties"])
    assert "out" not in tool.inputSchema["properties"]


@pytest.mark.unit
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
def test_ethereum_base_fee_tool_maps_to_canonical_cli(mock_which):
    argv = _build_argv(
        "eth",
        "chainwake_eth_network_base_fee",
        {
            "condition": {"kind": "move-pct", "pct": 5},
            "max_runtime": "1h",
        },
    )

    assert argv == [
        "/usr/local/bin/chainwake",
        "eth",
        "network",
        "base-fee",
        "--move-pct",
        "5",
        "--max-runtime",
        "1h",
    ]


@pytest.mark.unit
def test_ethereum_transaction_tool_exposes_finality_and_confirmations():
    tool = next(tool for tool in build_tools("eth") if tool.name == "chainwake_eth_tx")

    assert tool.description is not None
    assert "transaction" in tool.description.lower()
    assert "tx_hash" in tool.inputSchema["required"]
    assert {"finality", "confirmations", "max_runtime", "max_ru"} <= set(
        tool.inputSchema["properties"]
    )


@pytest.mark.unit
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
def test_ethereum_transaction_tool_maps_to_canonical_cli(mock_which):
    argv = _build_argv(
        "eth",
        "chainwake_eth_tx",
        {
            "tx_hash": TX_HASH,
            "finality": "included",
            "confirmations": 3,
            "max_runtime": "1h",
        },
    )

    assert argv == [
        "/usr/local/bin/chainwake",
        "eth",
        "tx",
        TX_HASH,
        "--finality",
        "included",
        "--confirmations",
        "3",
        "--max-runtime",
        "1h",
    ]
