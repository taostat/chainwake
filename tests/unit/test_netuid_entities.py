"""Regression tests for netuid domains and missing Bittensor entities."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock

import pytest
from async_substrate_interface import AsyncSubstrateInterface
from pydantic import ValidationError

from chainwake.cli.app import build_app
from chainwake.core.errors import UserError
from chainwake.mcp.tools import TOOL_SPECS, build_tools
from chainwake.providers.bittensor import BittensorProvider
from tests.ss58 import ALICE_SS58

pytestmark = pytest.mark.unit

_U16_MAX = 65_535
_HOTKEY = ALICE_SS58


def _valid_args(tool_name: str) -> dict[str, object]:
    """Return the smallest valid payload for every netuid-bearing MCP tool."""
    args: dict[str, object] = {"netuid": 1}
    if "_neuron_" in tool_name or "_validator_" in tool_name:
        args["hotkey"] = _HOTKEY
    if tool_name.endswith(("_hyperparams",)):
        return args
    if tool_name.endswith(("_identity",)):
        args["condition"] = {"kind": "on-change"}
    elif tool_name.endswith(("_last_update", "_weights")):
        args["silent_for"] = "1block"
    else:
        args["condition"] = {"kind": "below", "value": 1.0}
    if tool_name.endswith("_depth_for_trade"):
        args.update(size=1.0, max_bps=100.0)
    return args


def test_all_mcp_netuids_advertise_and_enforce_chain_bounds() -> None:
    """Every netuid field must reject values outside Subtensor's u16 domain."""
    tools_by_name = {tool.name: tool for tool in build_tools("bt")}
    netuid_specs = [spec for spec in TOOL_SPECS if "netuid" in spec.input_model.model_fields]
    assert netuid_specs

    for spec in netuid_specs:
        tool = tools_by_name[spec.name]
        netuid_schema = tool.inputSchema["properties"]["netuid"]
        assert netuid_schema["minimum"] == 0, tool.name
        assert netuid_schema["maximum"] <= _U16_MAX, tool.name

        args = _valid_args(tool.name)
        for invalid in (-1, _U16_MAX + 1):
            args["netuid"] = invalid
            with pytest.raises(ValidationError):
                spec.input_model.model_validate(args)


@pytest.mark.parametrize(
    "argv",
    [
        ["bt", "subnet", "-1", "price", "--below", "1"],
        ["bt", "subnet", "65536", "price", "--below", "1"],
        ["bt", "neuron", "-1", _HOTKEY, "dividends", "--below", "1"],
        ["bt", "neuron", "65536", _HOTKEY, "dividends", "--below", "1"],
    ],
)
def test_cli_rejects_out_of_u16_netuid_before_dispatch(argv: list[str]) -> None:
    dispatch = AsyncMock(return_value=1)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("chainwake.cli.chains.bittensor._dispatch_numeric", dispatch)
        with pytest.raises(SystemExit) as exit_info:
            build_app()(argv, exit_on_error=False)

    assert exit_info.value.code == 2
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_nonexistent_subnet_does_not_become_a_legitimate_zero() -> None:
    provider = BittensorProvider()
    substrate = AsyncMock()

    async def query(
        _module: str,
        storage_fn: str,
        params: list[object],
        *,
        block_hash: str,
    ) -> _ScaleValue:
        assert block_hash == "0xblock"
        assert storage_fn == "NetworksAdded"
        # Root (netuid 0) reads True — the endpoint is healthy, so the
        # False for the target netuid is authoritative and fails fast.
        return _ScaleValue(params[0] == 0)

    substrate.query.side_effect = query
    provider._substrate = cast(AsyncSubstrateInterface, substrate)
    read_price = AsyncMock(return_value=0.0)
    provider._read_subnet_price = read_price

    with pytest.raises(UserError, match=r"subnet 65535 does not exist"):
        await provider._dispatch_subnet(
            "subnet.{netuid}.pool.price",
            ["subnet", "65535", "pool", "price"],
            {},
            100,
            "0xblock",
        )

    read_price.assert_not_awaited()


@pytest.mark.asyncio
async def test_nonexistent_neuron_does_not_become_a_legitimate_zero() -> None:
    provider = BittensorProvider()
    substrate = AsyncMock()

    async def query(
        _module: str,
        storage_fn: str,
        _params: list[object],
        *,
        block_hash: str,
    ) -> _ScaleValue:
        assert block_hash == "0xblock"
        if storage_fn == "NetworksAdded":
            return _ScaleValue(True)
        if storage_fn == "Uids":
            return _ScaleValue(None)
        raise AssertionError(storage_fn)

    substrate.query.side_effect = query
    provider._substrate = cast(AsyncSubstrateInterface, substrate)
    read_dividends = AsyncMock(return_value=0.0)
    provider._read_neuron_dividends = read_dividends

    with pytest.raises(UserError, match=rf"hotkey {ALICE_SS58} is not registered on subnet 19"):
        await provider._dispatch_neuron(
            "neuron.{netuid}.{hotkey}.dividends",
            ["neuron", "19", _HOTKEY, "dividends"],
            {},
            100,
            "0xblock",
        )

    read_dividends.assert_not_awaited()


@pytest.mark.asyncio
async def test_uid_zero_remains_a_registered_neuron_with_zero_value() -> None:
    provider = BittensorProvider()
    substrate = AsyncMock()

    async def query(
        _module: str,
        storage_fn: str,
        _params: list[object],
        *,
        block_hash: str,
    ) -> _ScaleValue:
        assert block_hash == "0xblock"
        if storage_fn == "NetworksAdded":
            return _ScaleValue(True)
        if storage_fn == "Uids":
            return _ScaleValue(0)
        raise AssertionError(storage_fn)

    substrate.query.side_effect = query
    provider._substrate = cast(AsyncSubstrateInterface, substrate)
    read_dividends = AsyncMock(return_value=0.0)
    provider._read_neuron_dividends = read_dividends

    value = await provider._dispatch_neuron(
        "neuron.{netuid}.{hotkey}.dividends",
        ["neuron", "19", _HOTKEY, "dividends"],
        {},
        100,
        "0xblock",
    )

    assert value == 0.0
    read_dividends.assert_awaited_once()


class _ScaleValue:
    def __init__(self, value: object) -> None:
        self.value = value
