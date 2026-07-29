"""Unit tests for MCP tool execution (exec.py).

These tests exercise the argument translation logic without spawning a real
``chainwake`` subprocess.  Subprocess calls are mocked.

Tool names follow the new Pydantic-driven convention:
  chainwake_bt_subnet_price, chainwake_bt_tx, chainwake_bt_event, and the
  mechanism-aware neuron/validator tools.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from mcp.shared.exceptions import McpError
from pydantic import ValidationError

from chainwake.mcp.exec import _build_argv, run_tool
from chainwake.mcp.tools import TOOL_SPECS
from tests.ss58 import ALICE_SS58, BOB_SS58

# --- helpers ---


def _fake_completed_process(stdout: str, returncode: int = 0):
    class _CompletedProcess:
        def __init__(self) -> None:
            self.returncode = returncode

        async def communicate(self):
            return stdout.encode(), b""

    return _CompletedProcess()


_MATCHED_PAYLOAD = {
    "status": "matched",
    "watcher": {},
    "observed": {},
}
_TX_HASH = "0x" + "ab" * 32

_COMPLETE_CATALOGUE_CASES = {
    "subnet_price": (
        {"netuid": 28, "condition": {"kind": "below", "value": 1}},
        ["subnet", "28", "price", "--below", "1"],
    ),
    "subnet_tao_depth": (
        {"netuid": 28, "condition": {"kind": "below", "value": 1}},
        ["subnet", "28", "tao-depth", "--below", "1"],
    ),
    "subnet_alpha_depth": (
        {"netuid": 28, "condition": {"kind": "below", "value": 1}},
        ["subnet", "28", "alpha-depth", "--below", "1"],
    ),
    "subnet_depth_for_trade": (
        {
            "netuid": 28,
            "size": 10,
            "max_bps": 50,
            "condition": {"kind": "below", "value": 1},
        },
        [
            "subnet",
            "28",
            "depth-for-trade",
            "--below",
            "1",
            "--size",
            "10",
            "--max-bps",
            "50",
        ],
    ),
    "subnet_alpha_supply": (
        {"netuid": 28, "condition": {"kind": "below", "value": 1}},
        ["subnet", "28", "alpha-supply", "--below", "1"],
    ),
    "subnet_moving_price": (
        {"netuid": 28, "condition": {"kind": "below", "value": 1}},
        ["subnet", "28", "moving-price", "--below", "1"],
    ),
    "subnet_volume": (
        {"netuid": 28, "condition": {"kind": "below", "value": 1}},
        ["subnet", "28", "volume", "--below", "1"],
    ),
    "subnet_registration_cost": (
        {"netuid": 28, "condition": {"kind": "above", "value": 1}},
        ["subnet", "28", "registration-cost", "--above", "1"],
    ),
    "subnet_emission_share": (
        {"netuid": 28, "condition": {"kind": "below", "value": 1}},
        ["subnet", "28", "emission-share", "--below", "1"],
    ),
    "subnet_burn_rate": (
        {"netuid": 28, "condition": {"kind": "move-pct", "pct": 1}},
        ["subnet", "28", "burn-rate", "--move-pct", "1"],
    ),
    "subnet_ema_tao_flow": (
        {"netuid": 28, "condition": {"kind": "above", "value": 1}},
        ["subnet", "28", "ema-tao-flow", "--above", "1"],
    ),
    "subnet_hyperparams": (
        {"netuid": 28},
        ["subnet", "28", "hyperparams", "--on-change"],
    ),
    "subnet_identity": (
        {"netuid": 28, "condition": {"kind": "on-change"}},
        ["subnet", "28", "identity", "--on-change"],
    ),
    "validator_dividends_alpha": (
        {"hotkey": ALICE_SS58, "netuid": 28, "condition": {"kind": "below", "value": 1}},
        ["validator", ALICE_SS58, "dividends-alpha", "--netuid", "28", "--below", "1"],
    ),
    "validator_stake_alpha": (
        {"hotkey": ALICE_SS58, "netuid": 28, "condition": {"kind": "below", "value": 1}},
        ["validator", ALICE_SS58, "stake-alpha", "--netuid", "28", "--below", "1"],
    ),
    "validator_commission": (
        {"hotkey": ALICE_SS58, "condition": {"kind": "changes-to", "value": 0.18}},
        ["validator", ALICE_SS58, "commission", "--changes-to", "0.18"],
    ),
    "validator_weights": (
        {"hotkey": ALICE_SS58, "silent_for": "1h"},
        ["validator", ALICE_SS58, "weights", "--silent-for", "1h"],
    ),
    "validator_child_keys": (
        {"hotkey": ALICE_SS58},
        ["validator", ALICE_SS58, "child-keys", "--on-change"],
    ),
    "validator_identity": (
        {"hotkey": ALICE_SS58, "condition": {"kind": "on-change"}},
        ["validator", ALICE_SS58, "identity", "--on-change"],
    ),
    "neuron_incentive": (
        {
            "netuid": 28,
            "hotkey": ALICE_SS58,
            "condition": {"kind": "below", "value": 1},
        },
        ["neuron", "28", ALICE_SS58, "incentive", "--below", "1"],
    ),
    "neuron_dividends": (
        {
            "netuid": 28,
            "hotkey": ALICE_SS58,
            "condition": {"kind": "below", "value": 1},
        },
        ["neuron", "28", ALICE_SS58, "dividends", "--below", "1"],
    ),
    "neuron_stake_alpha": (
        {
            "netuid": 28,
            "hotkey": ALICE_SS58,
            "condition": {"kind": "below", "value": 1},
        },
        ["neuron", "28", ALICE_SS58, "stake-alpha", "--below", "1"],
    ),
    "neuron_last_update": (
        {"netuid": 28, "hotkey": ALICE_SS58, "silent_for": "10blocks"},
        ["neuron", "28", ALICE_SS58, "last-update", "--silent-for", "10blocks"],
    ),
    "neuron_blocks_until_immunity_expires": (
        {
            "netuid": 28,
            "hotkey": ALICE_SS58,
            "condition": {"kind": "below", "value": 10},
        },
        [
            "neuron",
            "28",
            ALICE_SS58,
            "blocks-until-immunity-expires",
            "--below",
            "10",
        ],
    ),
    "account_balance": (
        {"coldkey": ALICE_SS58, "condition": {"kind": "on-change"}},
        ["account", ALICE_SS58, "balance", "--on-change"],
    ),
    "account_activity": (
        {"coldkey": ALICE_SS58, "silent_for": "1h"},
        ["account", ALICE_SS58, "activity", "--silent-for", "1h"],
    ),
    "network_subnet_registration_cost": (
        {"condition": {"kind": "above", "value": 1}},
        ["network", "subnet-registration-cost", "--above", "1"],
    ),
    "network_tao_price": (
        {
            "condition": {
                "kind": "move-pct",
                "pct": 5,
                "window_time": "1h",
            }
        },
        ["network", "tao-price", "--move-pct", "5", "--window-time", "1h"],
    ),
    "network_runtime_version": (
        {},
        ["network", "runtime-version", "--on-change"],
    ),
    "network_subnet_count": (
        {"condition": {"kind": "above", "value": 64}},
        ["network", "subnet-count", "--above", "64"],
    ),
    "network_on_runtime_upgraded": (
        {},
        ["network", "on-runtime-upgraded"],
    ),
    "event": (
        {"event_type": "transfer"},
        ["event", "--type", "transfer"],
    ),
    "tx": (
        {"tx_hash": _TX_HASH, "finality": "finalized"},
        ["tx", _TX_HASH, "--finality", "finalized"],
    ),
}


@pytest.mark.unit
def test_complete_catalogue_fixture_covers_manifest():
    assert set(_COMPLETE_CATALOGUE_CASES) == {spec.slug for spec in TOOL_SPECS}


@pytest.mark.unit
def test_complete_catalogue_fixture_validates_against_every_advertised_model():
    for spec in TOOL_SPECS:
        args, _expected = _COMPLETE_CATALOGUE_CASES[spec.slug]
        spec.input_model.model_validate(args)


@pytest.mark.unit
@pytest.mark.parametrize("slug", sorted(_COMPLETE_CATALOGUE_CASES))
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
def test_every_manifest_tool_maps_to_complete_canonical_argv(mock_which, slug):
    args, expected = _COMPLETE_CATALOGUE_CASES[slug]
    argv = _build_argv("bt", f"chainwake_bt_{slug}", args)
    assert argv == ["/usr/local/bin/chainwake", "bt", *expected]


# === _build_argv — subnet price ===


@pytest.mark.unit
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
def test_build_argv_subnet_price_below(mock_which):
    argv = _build_argv(
        "bt",
        "chainwake_bt_subnet_price",
        {"netuid": 19, "condition": {"kind": "below", "value": 0.05}},
    )
    assert argv[0] == "/usr/local/bin/chainwake"
    assert argv[1] == "bt"
    assert "subnet" in argv
    assert "price" in argv
    assert "19" in argv
    assert "--below" in argv
    assert "0.05" in argv


@pytest.mark.unit
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
def test_build_argv_subnet_price_above(mock_which):
    argv = _build_argv(
        "bt",
        "chainwake_bt_subnet_price",
        {
            "netuid": 1,
            "condition": {"kind": "above", "value": 1.5},
            "rpc_url": "ws://localhost:9944",
        },
    )
    assert "--above" in argv
    assert "1.5" in argv
    assert "--rpc-url" in argv
    assert "ws://localhost:9944" in argv


@pytest.mark.unit
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
def test_build_argv_subnet_price_drop_pct_with_window_time(mock_which):
    argv = _build_argv(
        "bt",
        "chainwake_bt_subnet_price",
        {
            "netuid": 5,
            "condition": {
                "kind": "drop-pct",
                "pct": 10.0,
                "window_time": "1h",
                "window_blocks": None,
                "window_epochs": None,
            },
        },
    )
    assert "--drop-pct" in argv
    assert "10.0" in argv
    assert "--window-time" in argv
    assert "1h" in argv


@pytest.mark.unit
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
def test_build_argv_subnet_price_rise_pct_with_window_blocks(mock_which):
    argv = _build_argv(
        "bt",
        "chainwake_bt_subnet_price",
        {
            "netuid": 5,
            "condition": {
                "kind": "rise-pct",
                "pct": 5.0,
                "window_blocks": 100,
                "window_time": None,
                "window_epochs": None,
            },
        },
    )
    assert "--rise-pct" in argv
    assert "--window-blocks" in argv
    assert "100" in argv


@pytest.mark.unit
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
def test_build_argv_subnet_price_delta_without_window_uses_ever_default(mock_which):
    argv = _build_argv(
        "bt",
        "chainwake_bt_subnet_price",
        {
            "netuid": 28,
            "condition": {
                "kind": "move-pct",
                "pct": 1.0,
            },
        },
    )
    assert argv[-2:] == ["--move-pct", "1.0"]
    assert not any(arg.startswith("--window-") for arg in argv)


@pytest.mark.unit
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
def test_build_argv_rejects_ambiguous_delta_windows(mock_which):
    with pytest.raises(ValueError, match="mutually exclusive"):
        _build_argv(
            "bt",
            "chainwake_bt_subnet_price",
            {
                "netuid": 28,
                "condition": {
                    "kind": "move-pct",
                    "pct": 1.0,
                    "window_time": "1h",
                    "window_blocks": 10,
                },
            },
        )


# === _build_argv — tx ===


@pytest.mark.unit
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
def test_build_argv_tx(mock_which):
    argv = _build_argv(
        "bt",
        "chainwake_bt_tx",
        {"tx_hash": _TX_HASH, "finality": "finalized"},
    )
    assert "tx" in argv
    assert _TX_HASH in argv
    assert "--finality" in argv
    assert "finalized" in argv


# === _build_argv — event ===


@pytest.mark.unit
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
def test_build_argv_event_friendly_name(mock_which):
    argv = _build_argv(
        "bt",
        "chainwake_bt_event",
        {"event_type": "transfer"},
    )
    assert "event" in argv
    assert "--type" in argv
    assert "transfer" in argv


@pytest.mark.unit
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
def test_build_argv_event_raw_type(mock_which):
    argv = _build_argv(
        "bt",
        "chainwake_bt_event",
        {"type_raw": "Balances.Transfer"},
    )
    assert "--type-raw" in argv
    assert "Balances.Transfer" in argv


@pytest.mark.unit
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
def test_build_argv_event_forwards_all_predicate_filters(mock_which):
    argv = _build_argv(
        "bt",
        "chainwake_bt_event",
        {
            "event_type": "transfer",
            "from_addr": ALICE_SS58,
            "to_addr": BOB_SS58,
            "amount_min": 1_000,
            "direction": "in",
        },
    )

    assert argv[argv.index("--from") + 1] == ALICE_SS58
    assert argv[argv.index("--to") + 1] == BOB_SS58
    assert argv[argv.index("--amount-min") + 1] == "1000"
    assert argv[argv.index("--direction") + 1] == "in"
    assert argv[argv.index("--address") + 1] == BOB_SS58


@pytest.mark.unit
@pytest.mark.parametrize(
    "args",
    [
        {},
        {"event_type": "transfer", "type_raw": "Balances.Transfer"},
    ],
)
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
def test_build_argv_event_requires_exactly_one_type(mock_which, args):
    with pytest.raises(ValueError, match="exactly one"):
        _build_argv("bt", "chainwake_bt_event", args)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tool_name", "expected"),
    [
        (
            "chainwake_bt_validator_dividends_alpha",
            ["bt", "validator", ALICE_SS58, "dividends-alpha", "--netuid", "19"],
        ),
        (
            "chainwake_bt_validator_stake_alpha",
            ["bt", "validator", ALICE_SS58, "stake-alpha", "--netuid", "19"],
        ),
        (
            "chainwake_bt_neuron_stake_alpha",
            ["bt", "neuron", "19", ALICE_SS58, "stake-alpha"],
        ),
    ],
)
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
def test_build_argv_alpha_unit_tools(mock_which, tool_name, expected):
    argv = _build_argv(
        "bt",
        tool_name,
        {
            "netuid": 19,
            "hotkey": ALICE_SS58,
            "condition": {"kind": "below", "value": 100},
        },
    )
    assert argv[: len(expected) + 1] == ["/usr/local/bin/chainwake", *expected]
    assert argv[-2:] == ["--below", "100"]


@pytest.mark.unit
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
def test_build_argv_neuron_blocks_until_immunity(mock_which):
    argv = _build_argv(
        "bt",
        "chainwake_bt_neuron_blocks_until_immunity_expires",
        {
            "netuid": 19,
            "hotkey": ALICE_SS58,
            "condition": {"kind": "below", "value": 100},
        },
    )
    assert argv == [
        "/usr/local/bin/chainwake",
        "bt",
        "neuron",
        "19",
        ALICE_SS58,
        "blocks-until-immunity-expires",
        "--below",
        "100",
    ]


# === _build_argv — common flags ===


@pytest.mark.unit
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
def test_build_argv_max_runtime_flag(mock_which):
    argv = _build_argv(
        "bt",
        "chainwake_bt_subnet_price",
        {"netuid": 1, "condition": {"kind": "below", "value": 0.1}, "max_runtime": "5m"},
    )
    assert "--max-runtime" in argv
    assert "5m" in argv


@pytest.mark.unit
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
def test_build_argv_accepts_long_running_monitor(mock_which):
    argv = _build_argv(
        "bt",
        "chainwake_bt_network_runtime_version",
        {"max_runtime": "3d"},
    )
    assert argv[argv.index("--max-runtime") + 1] == "3d"


@pytest.mark.unit
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
def test_build_argv_max_ru_flag(mock_which):
    argv = _build_argv(
        "bt",
        "chainwake_bt_subnet_price",
        {
            "netuid": 1,
            "condition": {"kind": "below", "value": 0.1},
            "max_ru": 25_000,
        },
    )
    assert argv[argv.index("--max-ru") + 1] == "25000"


@pytest.mark.unit
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
def test_build_argv_rejects_poll_seconds_for_native_monitoring(mock_which):
    with pytest.raises(ValidationError):
        _build_argv(
            "bt",
            "chainwake_bt_subnet_registration_cost",
            {
                "netuid": 28,
                "condition": {"kind": "above", "value": 1},
                "poll_seconds": 0.25,
            },
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tool_name", "expected"),
    [
        (
            "chainwake_bt_network_runtime_version",
            ["bt", "network", "runtime-version", "--on-change"],
        ),
        (
            "chainwake_bt_network_on_runtime_upgraded",
            ["bt", "network", "on-runtime-upgraded"],
        ),
    ],
)
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
def test_build_argv_network_runtime_tools(mock_which, tool_name, expected):
    argv = _build_argv("bt", tool_name, {"max_runtime": "1m", "max_ru": 10_000})
    assert argv[: len(expected) + 1] == ["/usr/local/bin/chainwake", *expected]
    assert argv[argv.index("--max-runtime") + 1] == "1m"
    assert argv[argv.index("--max-ru") + 1] == "10000"


@pytest.mark.unit
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
def test_build_argv_rejects_out_so_mcp_always_returns_on_match(mock_which):
    with pytest.raises(ValueError, match=r"out.*not supported.*MCP"):
        _build_argv(
            "bt",
            "chainwake_bt_subnet_price",
            {
                "netuid": 1,
                "condition": {"kind": "below", "value": 0.1},
                "out": ["slack://xxx"],
            },
        )


@pytest.mark.unit
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
def test_build_argv_rejects_even_empty_out_field(mock_which):
    with pytest.raises(ValueError, match=r"out.*not supported.*MCP"):
        _build_argv(
            "bt",
            "chainwake_bt_event",
            {"event_type": "transfer", "out": []},
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tool_name", "args", "expected"),
    [
        (
            "chainwake_bt_subnet_burn_rate",
            {"netuid": 28, "condition": {"kind": "move-pct", "pct": 1}},
            ["bt", "subnet", "28", "burn-rate", "--move-pct", "1"],
        ),
        (
            "chainwake_bt_subnet_depth_for_trade",
            {
                "netuid": 28,
                "size": 10,
                "max_bps": 50,
                "condition": {"kind": "below", "value": 0},
            },
            [
                "bt",
                "subnet",
                "28",
                "depth-for-trade",
                "--below",
                "0",
                "--size",
                "10",
                "--max-bps",
                "50",
            ],
        ),
        (
            "chainwake_bt_validator_identity",
            {"hotkey": ALICE_SS58, "condition": {"kind": "on-change"}},
            ["bt", "validator", ALICE_SS58, "identity", "--on-change"],
        ),
        (
            "chainwake_bt_account_activity",
            {"coldkey": ALICE_SS58, "silent_for": "1h"},
            ["bt", "account", ALICE_SS58, "activity", "--silent-for", "1h"],
        ),
        (
            "chainwake_bt_network_subnet_count",
            {"condition": {"kind": "above", "value": 64}},
            ["bt", "network", "subnet-count", "--above", "64"],
        ),
    ],
)
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
def test_build_argv_complete_catalogue_uses_canonical_cli_order(
    mock_which, tool_name, args, expected
):
    argv = _build_argv("bt", tool_name, args)
    assert argv == ["/usr/local/bin/chainwake", *expected]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        (
            "chainwake_bt_validator_commission",
            {"hotkey": "5Fxxx", "condition": {"kind": "on-change"}},
        ),
        (
            "chainwake_bt_neuron_incentive",
            {"netuid": 1, "hotkey": "5Fxxx", "condition": {"kind": "below", "value": 1}},
        ),
        (
            "chainwake_bt_account_balance",
            {"coldkey": "5Fxxx", "condition": {"kind": "below", "value": 1}},
        ),
        (
            "chainwake_bt_event",
            {"event_type": "transfer", "from_addr": "5Fxxx"},
        ),
    ],
)
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
def test_build_argv_validates_agent_addresses_before_spawning(mock_which, tool_name, args):
    with pytest.raises(ValidationError):
        _build_argv("bt", tool_name, args)


@pytest.mark.unit
@patch("chainwake.mcp.exec.shutil.which", return_value=None)
def test_build_argv_no_chainwake_raises(mock_which):
    with pytest.raises(RuntimeError, match="chainwake executable not found"):
        _build_argv("bt", "chainwake_bt_subnet_price", {"netuid": 1})


@pytest.mark.unit
def test_build_argv_wrong_chain_raises():
    with pytest.raises(ValueError, match="unexpected tool name"):
        _build_argv("eth", "chainwake_bt_subnet_price", {})


@pytest.mark.unit
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
def test_build_argv_unknown_tool_raises(mock_which):
    with pytest.raises(ValueError, match="no command mapping"):
        _build_argv("bt", "chainwake_bt_nonexistent_tool", {})


# === run_tool ===


@pytest.mark.unit
@pytest.mark.asyncio
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
@patch("chainwake.mcp.exec.asyncio.create_subprocess_exec", new_callable=AsyncMock)
async def test_run_tool_success(mock_exec, mock_which):
    mock_exec.return_value = _fake_completed_process(json.dumps(_MATCHED_PAYLOAD), returncode=0)
    result = await run_tool(
        "bt",
        "chainwake_bt_subnet_price",
        {"netuid": 1, "condition": {"kind": "below", "value": 0.5}},
    )
    assert result["status"] == "matched"


@pytest.mark.unit
@pytest.mark.asyncio
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
@patch("chainwake.mcp.exec.asyncio.create_subprocess_exec", new_callable=AsyncMock)
async def test_run_tool_preserves_complete_matched_wake_context(mock_exec, mock_which):
    payload = {
        "status": "matched",
        "watcher": {
            "chain": "bt",
            "resource": "subnet",
            "resource_id": "28",
            "sub_resource": "burn-rate",
            "name": "burn wake",
            "primitive": "delta",
            "invocation": [
                "chainwake",
                "bt",
                "subnet",
                "28",
                "burn-rate",
                "--move-pct",
                "1",
            ],
        },
        "condition": {
            "operator": "move-pct",
            "target": 1,
            "window": {"unit": "ever", "value": "watcher-start"},
        },
        "observed": {
            "path": "subnet.28.burn-rate",
            "value": 0.2,
            "previous_value": 0.1,
            "delta": 0.1,
            "delta_pct": 100,
            "block": 1,
            "block_hash": "0x1",
            "timestamp": "2026-07-28T00:00:00Z",
        },
        "budget": {"runtime_ms": 1, "rpc_calls": 2, "estimated_ru_consumed": 2},
        "process": {"pid": 1, "started_at": "2026-07-28T00:00:00Z"},
    }
    mock_exec.return_value = _fake_completed_process(json.dumps(payload), returncode=0)

    result = await run_tool(
        "bt",
        "chainwake_bt_subnet_burn_rate",
        {"netuid": 28, "condition": {"kind": "move-pct", "pct": 1}},
    )

    assert result == payload


@pytest.mark.unit
@pytest.mark.asyncio
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
@patch("chainwake.mcp.exec.asyncio.create_subprocess_exec", new_callable=AsyncMock)
async def test_run_tool_timeout_exit1_is_ok(mock_exec, mock_which):
    payload = {
        "status": "timeout",
        "reason": "max_runtime_reached",
        "watcher": {
            "chain": "bt",
            "resource": "network",
            "resource_id": None,
            "sub_resource": "runtime-version",
            "name": None,
            "primitive": "state",
            "invocation": ["chainwake", "bt", "network", "runtime-version", "--on-change"],
        },
        "condition": {"operator": "on-change", "target": None},
        "observed": None,
        "budget": {"runtime_ms": 1000, "rpc_calls": 1, "estimated_ru_consumed": 1},
        "process": {"pid": 1, "started_at": "2026-07-28T00:00:00Z"},
    }
    mock_exec.return_value = _fake_completed_process(json.dumps(payload), returncode=1)
    result = await run_tool("bt", "chainwake_bt_network_runtime_version", {"max_runtime": "1s"})
    assert result == payload


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("returncode", "status"),
    [
        (2, "user_error"),
        (3, "provider_error"),
        (3, "auth_error"),
        (4, "internal_error"),
    ],
)
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
@patch("chainwake.mcp.exec.asyncio.create_subprocess_exec", new_callable=AsyncMock)
async def test_run_tool_returns_full_structured_error_context(
    mock_exec,
    mock_which,
    returncode,
    status,
):
    payload = {
        "status": status,
        "message": f"{status} detail for the agent",
        "reason": "focused-test-reason",
        "watcher": {
            "chain": "bt",
            "resource": "subnet",
            "resource_id": "1",
            "sub_resource": "price",
            "invocation": ["chainwake", "bt", "subnet", "1", "price", "--below", "0.5"],
        },
        "condition": {"operator": "below", "target": 0.5},
        "observed": None,
    }
    mock_exec.return_value = _fake_completed_process(json.dumps(payload), returncode=returncode)

    result = await run_tool(
        "bt",
        "chainwake_bt_subnet_price",
        {"netuid": 1, "condition": {"kind": "below", "value": 0.5}},
    )

    assert result == payload


@pytest.mark.unit
@pytest.mark.asyncio
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
@patch("chainwake.mcp.exec.asyncio.create_subprocess_exec", new_callable=AsyncMock)
async def test_run_tool_non_json_raises_mcp_error(mock_exec, mock_which):
    mock_exec.return_value = _fake_completed_process("not json at all", returncode=0)
    with pytest.raises(McpError, match="non-JSON"):
        await run_tool(
            "bt",
            "chainwake_bt_subnet_price",
            {"netuid": 1, "condition": {"kind": "below", "value": 0.5}},
        )


@pytest.mark.unit
@pytest.mark.asyncio
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
@patch("chainwake.mcp.exec.asyncio.create_subprocess_exec", new_callable=AsyncMock)
async def test_run_tool_non_object_json_raises_protocol_error(mock_exec, mock_which):
    mock_exec.return_value = _fake_completed_process('["not", "a", "payload"]', returncode=3)
    with pytest.raises(McpError, match="JSON object"):
        await run_tool(
            "bt",
            "chainwake_bt_subnet_price",
            {"netuid": 1, "condition": {"kind": "below", "value": 0.5}},
        )


@pytest.mark.unit
@pytest.mark.asyncio
@patch("chainwake.mcp.exec.shutil.which", return_value="/usr/local/bin/chainwake")
@patch("chainwake.mcp.exec.asyncio.create_subprocess_exec", new_callable=AsyncMock)
async def test_run_tool_spawn_failure_is_protocol_error(mock_exec, mock_which):
    mock_exec.side_effect = OSError("permission denied")
    with pytest.raises(McpError, match="could not start"):
        await run_tool(
            "bt",
            "chainwake_bt_subnet_price",
            {"netuid": 1, "condition": {"kind": "below", "value": 0.5}},
        )


class _HangingProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.started = asyncio.Event()
        self.terminated = False
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        self.started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        assert self.returncode is not None
        return self.returncode


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_tool_hard_timeout_terminates_child(monkeypatch):
    process = _HangingProcess()

    async def _create(*_argv, **_kwargs):
        return process

    monkeypatch.setattr("chainwake.mcp.exec._find_chainwake", lambda: "chainwake")
    monkeypatch.setattr("chainwake.mcp.exec.asyncio.create_subprocess_exec", _create)

    with pytest.raises(McpError, match="timed out"):
        await run_tool(
            "bt",
            "chainwake_bt_network_runtime_version",
            {"max_runtime": "1h"},
            timeout_seconds=0.01,
        )

    assert process.terminated


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_tool_cancellation_terminates_child(monkeypatch):
    process = _HangingProcess()

    async def _create(*_argv, **_kwargs):
        return process

    monkeypatch.setattr("chainwake.mcp.exec._find_chainwake", lambda: "chainwake")
    monkeypatch.setattr("chainwake.mcp.exec.asyncio.create_subprocess_exec", _create)

    task = asyncio.create_task(
        run_tool(
            "bt",
            "chainwake_bt_network_runtime_version",
            {"max_runtime": "1h"},
            timeout_seconds=60,
        )
    )
    await process.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.terminated
