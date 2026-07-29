"""CLI subcommand: chainwake mcp serve --stdio | --port N."""

from __future__ import annotations

import json
import math
import re
import sys
from typing import Annotated, Never

import anyio
import cyclopts

from chainwake.core.duration import InvalidDurationError, duration_to_seconds, parse_duration
from chainwake.mcp.server import run_http, run_stdio

_DEFAULT_TOOL_TIMEOUT = "24h"
_CLIENT_TIMEOUT_GRACE_SECONDS = 60 * 60
_YAML_PLAIN_SCALAR = re.compile(r"^[A-Za-z0-9_./-]+$")
_TOOL_TIMEOUT_PARAM = cyclopts.Parameter(
    name="--tool-timeout",
    env_var="CHAINWAKE_MCP_TOOL_TIMEOUT",
    help=(
        "Server-side safety limit for one tool call (e.g. 2h, 3d). "
        "Default: 24h. Cancellation still terminates the watcher."
    ),
)


def _die(message: str) -> Never:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


def _yaml_scalar(value: str) -> str:
    """Render the command as a safe, deterministic YAML scalar."""
    return value if _YAML_PLAIN_SCALAR.fullmatch(value) else json.dumps(value)


def _parse_tool_timeout(raw: str) -> tuple[str, float]:
    """Return a canonical wall-clock duration and its positive seconds."""
    try:
        canonical = parse_duration(raw)
        seconds = duration_to_seconds(canonical)
    except InvalidDurationError as exc:
        _die(f"--tool-timeout {raw!r} — {exc}")
    if seconds <= 0:
        _die("--tool-timeout must be greater than zero")
    return canonical, seconds


def _client_timeout_seconds(tool_timeout_seconds: float) -> int:
    return math.ceil(tool_timeout_seconds) + _CLIENT_TIMEOUT_GRACE_SECONDS


def _hermes_config(command: str, tool_timeout: str = _DEFAULT_TOOL_TIMEOUT) -> str:
    """Return a complete Hermes ``mcp_servers`` configuration fragment."""
    canonical, seconds = _parse_tool_timeout(tool_timeout)
    return (
        "mcp_servers:\n"
        "  chainwake:\n"
        f"    command: {_yaml_scalar(command)}\n"
        "    args:\n"
        "      - mcp\n"
        "      - serve\n"
        "      - --stdio\n"
        "      - --tool-timeout\n"
        f"      - {_yaml_scalar(canonical)}\n"
        f"    timeout: {_client_timeout_seconds(seconds)}\n"
    )


def _openclaw_config(command: str, tool_timeout: str = _DEFAULT_TOOL_TIMEOUT) -> str:
    """Return a complete OpenClaw ``mcp.servers`` JSON fragment."""
    canonical, seconds = _parse_tool_timeout(tool_timeout)
    config = {
        "mcp": {
            "servers": {
                "chainwake": {
                    "command": command,
                    "args": [
                        "mcp",
                        "serve",
                        "--stdio",
                        "--tool-timeout",
                        canonical,
                    ],
                    "requestTimeoutMs": _client_timeout_seconds(seconds) * 1_000,
                    "connectionTimeoutMs": 10_000,
                }
            }
        }
    }
    return f"{json.dumps(config, indent=2)}\n"


def build_mcp_app() -> cyclopts.App:
    """Return the ``mcp`` cyclopts sub-app."""
    mcp_app = cyclopts.App(name="mcp", help="MCP server — expose chain watchers as MCP tools.")
    serve_app = cyclopts.App(name="serve", help="Start the MCP server.")
    config_app = cyclopts.App(
        name="config",
        help=(
            "Print copy-paste MCP client config. Set each tool call's max_runtime "
            "below the server tool timeout; generated clients add one hour of grace."
        ),
    )
    mcp_app.command(serve_app)
    mcp_app.command(config_app)

    @serve_app.default
    def serve(
        *,
        stdio: Annotated[bool, cyclopts.Parameter(name="--stdio", negative="")] = False,
        port: Annotated[int | None, cyclopts.Parameter(name="--port")] = None,
        host: Annotated[str, cyclopts.Parameter(name="--host")] = "127.0.0.1",
        tool_timeout: Annotated[str, _TOOL_TIMEOUT_PARAM] = _DEFAULT_TOOL_TIMEOUT,
    ) -> None:
        """Start the MCP server in stdio or HTTP mode."""
        if stdio and port is not None:
            _die("--stdio and --port are mutually exclusive")
        if not stdio and port is None:
            _die("one of --stdio or --port is required")
        _canonical, tool_timeout_seconds = _parse_tool_timeout(tool_timeout)
        if stdio:
            anyio.run(run_stdio, "all", tool_timeout_seconds)
        elif port is not None:
            try:
                run_http(
                    port,
                    host=host,
                    tool_timeout_seconds=tool_timeout_seconds,
                )
            except ValueError as exc:
                _die(str(exc))

    @config_app.command
    def hermes(
        *,
        command: Annotated[
            str,
            cyclopts.Parameter(
                name="--command",
                help="Executable Hermes should launch (use an absolute path if needed).",
            ),
        ] = "chainwake",
        tool_timeout: Annotated[str, _TOOL_TIMEOUT_PARAM] = _DEFAULT_TOOL_TIMEOUT,
    ) -> None:
        """Print YAML for ~/.hermes/config.yaml."""
        sys.stdout.write(_hermes_config(command, tool_timeout))

    @config_app.command
    def openclaw(
        *,
        command: Annotated[
            str,
            cyclopts.Parameter(
                name="--command",
                help="Executable OpenClaw should launch (use an absolute path if needed).",
            ),
        ] = "chainwake",
        tool_timeout: Annotated[str, _TOOL_TIMEOUT_PARAM] = _DEFAULT_TOOL_TIMEOUT,
    ) -> None:
        """Print JSON for the OpenClaw configuration."""
        sys.stdout.write(_openclaw_config(command, tool_timeout))

    return mcp_app
