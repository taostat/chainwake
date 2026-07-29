"""MCP discovery and translation contracts for Base and BSC."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from chainwake.core.registry import all_entries
from chainwake.mcp.exec import _build_argv
from chainwake.mcp.tools import build_tools

TX_HASH = f"0x{'ab' * 32}"

pytestmark = pytest.mark.unit


def test_base_mcp_catalogue_covers_every_base_observable() -> None:
    tools = build_tools("base")

    assert [tool.name for tool in tools] == [
        "chainwake_base_network_base_fee",
        "chainwake_base_network_l1_base_fee",
        "chainwake_base_network_l1_blob_base_fee",
        "chainwake_base_token_price",
        "chainwake_base_tx",
    ]
    assert {entry.path_template for entry in all_entries(chain="base")} == {
        "network.base-fee",
        "network.l1-base-fee",
        "network.l1-blob-base-fee",
        "token.{token}.price",
        "tx.{tx_hash}",
    }


def test_bsc_mcp_catalogue_covers_every_bsc_observable() -> None:
    tools = build_tools("bsc")

    assert [tool.name for tool in tools] == [
        "chainwake_bsc_network_gas_price",
        "chainwake_bsc_token_price",
        "chainwake_bsc_tx",
    ]
    assert {entry.path_template for entry in all_entries(chain="bsc")} == {
        "network.gas-price",
        "token.{token}.price",
        "tx.{tx_hash}",
    }


def test_base_safe_transaction_maps_to_canonical_cli() -> None:
    with patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake"):
        argv = _build_argv(
            "base",
            "chainwake_base_tx",
            {
                "tx_hash": TX_HASH,
                "finality": "safe",
                "max_runtime": "1h",
            },
        )

    assert argv == [
        "/usr/local/bin/chainwake",
        "base",
        "tx",
        TX_HASH,
        "--finality",
        "safe",
        "--max-runtime",
        "1h",
    ]


def test_bsc_gas_price_maps_to_canonical_cli() -> None:
    with patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake"):
        argv = _build_argv(
            "bsc",
            "chainwake_bsc_network_gas_price",
            {
                "condition": {"kind": "above", "value": 0.1},
                "max_runtime": "30m",
            },
        )

    assert argv == [
        "/usr/local/bin/chainwake",
        "bsc",
        "network",
        "gas-price",
        "--above",
        "0.1",
        "--max-runtime",
        "30m",
    ]


def test_bsc_mcp_schema_rejects_safe_finality() -> None:
    tool = next(tool for tool in build_tools("bsc") if tool.name == "chainwake_bsc_tx")

    assert tool.inputSchema["properties"]["finality"]["enum"] == ["included", "finalized"]


def test_token_price_maps_to_id_first_canonical_cli() -> None:
    with patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake"):
        argv = _build_argv(
            "eth",
            "chainwake_eth_token_price",
            {
                "token": "DAI",
                "condition": {"kind": "below", "value": 0.995},
                "max_runtime": "1h",
            },
        )

    assert argv == [
        "/usr/local/bin/chainwake",
        "eth",
        "token",
        "DAI",
        "price",
        "--below",
        "0.995",
        "--max-runtime",
        "1h",
    ]


def test_token_price_schema_only_offers_wall_clock_delta_windows() -> None:
    tool = next(tool for tool in build_tools("bsc") if tool.name == "chainwake_bsc_token_price")
    definitions = tool.inputSchema["$defs"]

    for name in (
        "ExternalPriceDropPctCondition",
        "ExternalPriceRisePctCondition",
        "ExternalPriceMovePctCondition",
    ):
        assert "window_time" in definitions[name]["properties"]
        assert "window_blocks" not in definitions[name]["properties"]
