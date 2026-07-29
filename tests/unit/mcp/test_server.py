"""Security and execution-boundary tests for MCP transports."""

from __future__ import annotations

import inspect
import json
from typing import cast
from unittest.mock import AsyncMock

import mcp.types as mcp_types
import pytest

from chainwake.mcp import server as server_module
from chainwake.mcp.exec import DEFAULT_TOOL_TIMEOUT_SECONDS


@pytest.mark.unit
def test_http_defaults_to_loopback():
    assert inspect.signature(server_module.run_http).parameters["host"].default == "127.0.0.1"


@pytest.mark.unit
def test_tool_safety_timeout_defaults_to_24_hours():
    assert DEFAULT_TOOL_TIMEOUT_SECONDS == 24 * 60 * 60


@pytest.mark.unit
def test_server_advertises_stable_chainwake_identity():
    assert server_module.build_server().name == "chainwake"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_server_advertises_bittensor_and_ethereum_tools():
    server = server_module.build_server()
    handler = server.request_handlers[mcp_types.ListToolsRequest]

    result = await handler(mcp_types.ListToolsRequest())

    root = cast(mcp_types.ListToolsResult, result.root)
    names = {tool.name for tool in root.tools}
    assert "chainwake_bt_subnet_price" in names
    assert "chainwake_eth_network_base_fee" in names


@pytest.mark.unit
@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "192.168.1.10", "chainwake.example"],  # noqa: S104
)
def test_http_rejects_non_loopback_bind_without_auth(host):
    with pytest.raises(ValueError, match="loopback"):
        server_module._validate_http_host(host)


@pytest.mark.unit
@pytest.mark.parametrize("host", ["127.0.0.1", "127.42.0.7", "::1", "localhost"])
def test_http_accepts_loopback_bind(host):
    assert server_module._validate_http_host(host) == host


@pytest.mark.unit
@pytest.mark.parametrize(
    "uri",
    [
        "file:///etc/cron.d/chainwake",
        "FILE:///tmp/chainwake.jsonl",
    ],
)
def test_http_tool_rejects_file_output(uri):
    with pytest.raises(ValueError, match="file"):
        server_module._validate_http_arguments({"out": [uri]})


@pytest.mark.unit
def test_http_tool_rejects_rpc_override_to_prevent_ssrf():
    with pytest.raises(ValueError, match="rpc_url"):
        server_module._validate_http_arguments({"rpc_url": "ws://169.254.169.254:9944"})


@pytest.mark.unit
@pytest.mark.parametrize("uri", ["tgram://token/chat", "discord://webhook", "stream"])
def test_http_tool_preserves_non_file_output_adapters(uri):
    server_module._validate_http_arguments({"out": [uri]})


def _call_request(arguments):
    return mcp_types.CallToolRequest(
        params=mcp_types.CallToolRequestParams(
            name="chainwake_bt_event",
            arguments=arguments,
        )
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_handler_awaits_async_child_execution(monkeypatch):
    run_tool = AsyncMock(return_value={"status": "matched"})
    monkeypatch.setattr(server_module, "run_tool", run_tool)
    server = server_module.build_server()
    handler = server.request_handlers[mcp_types.CallToolRequest]

    result = await handler(_call_request({"event_type": "transfer"}))

    run_tool.assert_awaited_once()
    root = cast(mcp_types.CallToolResult, result.root)
    content = cast(mcp_types.TextContent, root.content[0])
    payload = json.loads(content.text)
    assert payload["status"] == "matched"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ethereum_tool_handler_routes_to_ethereum_chain(monkeypatch):
    run_tool = AsyncMock(return_value={"status": "matched"})
    monkeypatch.setattr(server_module, "run_tool", run_tool)
    server = server_module.build_server()
    handler = server.request_handlers[mcp_types.CallToolRequest]
    request = mcp_types.CallToolRequest(
        params=mcp_types.CallToolRequestParams(
            name="chainwake_eth_network_base_fee",
            arguments={"condition": {"kind": "below", "value": 10}},
        )
    )

    await handler(request)

    assert run_tool.await_args is not None
    assert run_tool.await_args.args[:2] == ("eth", "chainwake_eth_network_base_fee")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("remote_http", [False, True], ids=["stdio", "http"])
@pytest.mark.parametrize(
    "status",
    ["user_error", "provider_error", "auth_error", "internal_error"],
)
async def test_tool_handler_returns_full_error_payload_as_agent_visible_result(
    monkeypatch,
    remote_http,
    status,
):
    payload = {
        "status": status,
        "message": f"{status} detail for the agent",
        "reason": "focused-test-reason",
        "watcher": {
            "chain": "bt",
            "resource": "event",
            "resource_id": None,
            "sub_resource": "transfer",
            "invocation": ["chainwake", "bt", "event", "--type", "transfer"],
        },
        "condition": {"operator": "event", "target": "transfer"},
        "observed": None,
    }
    run_tool = AsyncMock(return_value=payload)
    monkeypatch.setattr(server_module, "run_tool", run_tool)
    server = server_module.build_server(remote_http=remote_http)
    handler = server.request_handlers[mcp_types.CallToolRequest]

    result = await handler(_call_request({"event_type": "transfer"}))

    root = cast(mcp_types.CallToolResult, result.root)
    content = cast(mcp_types.TextContent, root.content[0])
    assert root.isError is False
    assert json.loads(content.text) == payload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_handler_forwards_configured_safety_timeout(monkeypatch):
    run_tool = AsyncMock(return_value={"status": "timeout"})
    monkeypatch.setattr(server_module, "run_tool", run_tool)
    server = server_module.build_server(tool_timeout_seconds=3 * 24 * 60 * 60)
    handler = server.request_handlers[mcp_types.CallToolRequest]

    await handler(
        _call_request(
            {
                "event_type": "transfer",
                "max_runtime": "2d",
            }
        )
    )

    await_args = run_tool.await_args
    assert await_args is not None
    assert await_args.kwargs["timeout_seconds"] == 3 * 24 * 60 * 60


@pytest.mark.unit
@pytest.mark.asyncio
async def test_http_handler_rejects_unsafe_arguments_before_child_execution(monkeypatch):
    run_tool = AsyncMock()
    monkeypatch.setattr(server_module, "run_tool", run_tool)
    server = server_module.build_server(remote_http=True)
    handler = server.request_handlers[mcp_types.CallToolRequest]

    result = await handler(_call_request({"event_type": "transfer", "out": ["file:///tmp/result"]}))

    root = cast(mcp_types.CallToolResult, result.root)
    assert root.isError
    run_tool.assert_not_awaited()
