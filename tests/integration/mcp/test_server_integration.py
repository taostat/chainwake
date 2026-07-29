"""Integration test: stdio MCP server tool invocation against localnet.

Boots the MCP server over stdio using the MCP SDK's test client, sends a
``call_tool`` request for ``chainwake_bt_subnet_price``, and asserts that the
response contains a valid chainwake JSON payload.

Requires ``CHAINWAKE_REUSE_NODE=1`` when a localnet is already running.
Registers a fresh subnet per test; never assumes a netuid exists.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from typing import Any

import pytest

pytest.importorskip("mcp", reason="mcp SDK required for integration tests")

from mcp import ClientSession, StdioServerParameters, stdio_client

from tests.integration.harness.local_chain import (
    derive_dev_account,
    tao_to_rao,
)


def _chainwake_exe() -> str:
    exe = shutil.which("chainwake")
    if exe is None:
        # Fall back to running via the current Python interpreter.
        return sys.executable
    return exe


def _server_params(extra_env: dict[str, str] | None = None) -> StdioServerParameters:
    exe = _chainwake_exe()
    if exe == sys.executable:
        args = ["-m", "chainwake", "mcp", "serve", "--stdio"]
    else:
        args = ["mcp", "serve", "--stdio"]
    env = {**os.environ, **(extra_env or {})}
    return StdioServerParameters(command=exe, args=args, env=env)


@pytest.mark.integration
async def test_stdio_mcp_list_tools(local_chain: Any) -> None:
    """The stdio server exposes tools generated from the registry."""
    async with (
        stdio_client(_server_params()) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools_result = await session.list_tools()
        tool_names = [t.name for t in tools_result.tools]
        assert "chainwake_bt_subnet_price" in tool_names, (
            f"expected tool not found; got: {tool_names}"
        )


@pytest.mark.integration
async def test_stdio_mcp_subnet_price_call(local_chain: Any) -> None:
    """Tool call for subnet price returns a valid chainwake JSON payload.

    Registers a fresh subnet so the netuid is known and exists.  Uses
    ``below 9999`` which is guaranteed to match on any live localnet.
    """
    # Fund and register a fresh subnet for isolation.
    owner = derive_dev_account("dave").keypair
    await local_chain.fund_account(owner.ss58_address, rao=tao_to_rao(200))
    result_dr = await local_chain.register_subnet(owner)
    netuid = int(result_dr.extra["netuid"])

    rpc_url = "ws://127.0.0.1:9944"

    async with (
        stdio_client(_server_params({"CHAINWAKE_BT_RPC_URL": rpc_url})) as (
            read,
            write,
        ),
        ClientSession(read, write) as session,
    ):
        await session.initialize()

        call_result = await session.call_tool(
            "chainwake_bt_subnet_price",
            arguments={
                "netuid": netuid,
                "condition": {"kind": "below", "value": 9999.0},
                "rpc_url": rpc_url,
            },
        )

        assert call_result.content, "expected non-empty content"
        # Access text via model_dump to stay type-safe across MCP SDK versions.
        raw = call_result.content[0].model_dump().get("text", "")
        payload = json.loads(raw)

        assert payload.get("status") in ("matched", "timeout"), (
            f"unexpected status: {payload.get('status')}"
        )
