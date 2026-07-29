"""MCP server construction for Blockmachine Chainwake.

Uses the MCP lowlevel server for both stdio and HTTP transports so tool input
schemas are taken from ``build_tools()`` directly rather than derived from
Python function signatures.  FastMCP's ``add_tool`` API derives schemas from
function signatures and cannot accept custom schemas; using the lowlevel
``Server`` avoids that constraint for all transports.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
from collections.abc import AsyncIterator
from typing import Any

import anyio
import mcp.types as mcp_types
import uvicorn
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.routing import Route

from chainwake.mcp.exec import DEFAULT_TOOL_TIMEOUT_SECONDS, run_tool
from chainwake.mcp.tools import build_tools

_CHAIN = "all"
_LOOPBACK_HOSTNAME = "localhost"


def _text_content(text: str) -> list[mcp_types.TextContent]:
    return [mcp_types.TextContent(type="text", text=text)]


def _validate_http_host(host: str) -> str:
    """Allow HTTP only on loopback until the server has real authentication."""
    if host == _LOOPBACK_HOSTNAME:
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is None or not address.is_loopback:
        raise ValueError(
            "MCP HTTP may bind only to a loopback address because authentication is not implemented"
        )
    return host


def _validate_http_arguments(arguments: dict[str, Any]) -> None:
    """Reject HTTP-only server-side access to local files and arbitrary RPCs."""
    if arguments.get("rpc_url") is not None:
        raise ValueError(
            "rpc_url overrides are disabled over MCP HTTP; configure the server endpoint instead"
        )
    for uri in arguments.get("out", []):
        if str(uri).lower().startswith("file://"):
            raise ValueError("file output adapters are disabled over MCP HTTP")


def build_server(
    chain: str = _CHAIN,
    *,
    remote_http: bool = False,
    tool_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
) -> Server:
    """Construct and return the MCP lowlevel server with all chainwake tools registered."""
    tools = build_tools(chain)
    server: Server = Server(name="chainwake")

    @server.list_tools()
    async def list_tools() -> list[mcp_types.Tool]:
        return tools

    @server.call_tool()
    async def call_tool(
        name: str,
        arguments: dict[str, Any],
    ) -> list[mcp_types.TextContent]:
        if remote_http:
            _validate_http_arguments(arguments)
        tool_chain = name.removeprefix("chainwake_").split("_", 1)[0]
        if chain not in {"all", tool_chain}:
            raise ValueError(f"tool {name!r} does not belong to MCP chain {chain!r}")
        payload = await run_tool(
            tool_chain,
            name,
            arguments,
            timeout_seconds=tool_timeout_seconds,
        )
        return _text_content(json.dumps(payload, indent=2))

    return server


async def run_stdio(
    chain: str = _CHAIN,
    tool_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
) -> None:
    """Run the MCP server over stdio (for Claude Desktop, Cursor, etc.)."""
    server = build_server(chain, tool_timeout_seconds=tool_timeout_seconds)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def run_http(
    port: int,
    chain: str = _CHAIN,
    host: str = "127.0.0.1",
    tool_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
) -> None:
    """Run the MCP server over Streamable HTTP transport.

    Starts uvicorn on a loopback address using the same lowlevel server as the
    stdio path so tool schemas are preserved exactly. Non-loopback binds fail
    closed because this server does not implement HTTP authentication.
    """
    _validate_http_host(host)
    server = build_server(
        chain,
        remote_http=True,
        tool_timeout_seconds=tool_timeout_seconds,
    )
    session_manager = StreamableHTTPSessionManager(app=server, stateless=True)

    @contextlib.asynccontextmanager
    async def _lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    async def _handle_mcp(scope: Any, receive: Any, send: Any) -> None:
        await session_manager.handle_request(scope, receive, send)

    starlette_app = Starlette(
        routes=[Route("/mcp", endpoint=_handle_mcp)],
        lifespan=_lifespan,
    )
    config = uvicorn.Config(starlette_app, host=host, port=port, log_level="info")
    anyio.run(uvicorn.Server(config).serve)


__all__ = ["build_server", "run_http", "run_stdio"]
