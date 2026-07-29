"""Subprocess execution of ``chainwake`` from MCP tool call arguments.

Translates the flat dict of MCP tool arguments back into the equivalent
``chainwake`` CLI invocation, runs it as a subprocess inheriting the
caller's environment, and returns the parsed JSON result.

Tool names map directly to CLI sub-commands:
  chainwake_bt_subnet_price -> bt subnet <netuid> price <condition-flags>
  chainwake_bt_tx           -> bt tx <tx_hash> --finality <level>
  chainwake_bt_event        -> bt event [--type <name> | --type-raw <raw>]
  chainwake_bt_validator_dividends_alpha
                            -> bt validator <hotkey> dividends-alpha --netuid <netuid>
  chainwake_bt_validator_stake_alpha
                            -> bt validator <hotkey> stake-alpha --netuid <netuid>
  chainwake_bt_neuron_stake_alpha
                            -> bt neuron <netuid> <hotkey> stake-alpha
    (use --type subnet-registered for the subnet-creation event)
  chainwake_bt_neuron_incentive -> bt neuron incentive <netuid> <hotkey>
  chainwake_bt_neuron_last_update -> bt neuron last-update <netuid> <hotkey>
  chainwake_bt_neuron_blocks_until_immunity_expires
                            -> bt neuron <netuid> <hotkey> blocks-until-immunity-expires
  chainwake_bt_validator_weights -> bt validator weights <hotkey>

Failure modes:
- Chainwake exit (0-4) with a JSON payload -> the complete payload is returned.
- Spawn failures, unexpected exit codes, and invalid output -> ``McpError``.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from typing import Any

from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from chainwake.cli.inputs.event import is_raw_event_type
from chainwake.mcp.tools import EVM_TOOL_SPECS, TOOL_SPECS, tool_spec_for

# Derived from the same manifest that builds agent-visible schemas.
_TOOL_TO_COMMAND: dict[tuple[str, str], list[str]] = {
    (spec.chain, spec.slug): list(spec.command) for spec in (*TOOL_SPECS, *EVM_TOOL_SPECS)
}

# Common optional flags shared across most tools.
_COMMON_FLAG_MAP: dict[str, str] = {
    "rpc_url": "--rpc-url",
    "name": "--name",
    "max_runtime": "--max-runtime",
    "max_ru": "--max-ru",
}

DEFAULT_TOOL_TIMEOUT_SECONDS = 24 * 60 * 60
_TERMINATE_GRACE_SECONDS = 2.0
_CHAINWAKE_EXIT_CODES = frozenset({0, 1, 2, 3, 4})


def _find_chainwake() -> str:
    """Return the ``chainwake`` executable path."""
    exe = shutil.which("chainwake")
    if exe:
        return exe
    raise RuntimeError(
        "chainwake executable not found on PATH. Install it with: pip install chainwake"
    )


_THRESHOLD_KINDS: dict[str, str] = {
    "below": "--below",
    "above": "--above",
}

_DELTA_KINDS: dict[str, str] = {
    "drop-pct": "--drop-pct",
    "rise-pct": "--rise-pct",
    "move-pct": "--move-pct",
}

_STATE_KINDS: dict[str, str] = {
    "changes-to": "--changes-to",
    "changes-from": "--changes-from",
}


def _condition_to_flags(condition: dict[str, Any]) -> list[str]:
    """Translate a Pydantic condition dict (from MCP args) to CLI flags.

    The MCP client sends the condition as a dict with a ``kind`` discriminator
    matching the CLI flag semantics defined in the input models.
    """
    kind = condition.get("kind")
    if kind in _THRESHOLD_KINDS:
        return [_THRESHOLD_KINDS[kind], str(condition["value"])]
    if kind in _DELTA_KINDS:
        return [_DELTA_KINDS[kind], str(condition["pct"]), *_window_flags(condition)]
    if kind == "on-change":
        return ["--on-change"]
    if kind in _STATE_KINDS:
        return [_STATE_KINDS[kind], str(condition["value"])]
    raise ValueError(f"unknown condition kind: {kind!r}")


def _window_flags(condition: dict[str, Any]) -> list[str]:
    """Extract an optional rolling-window flag from a delta condition dict."""
    windows = [
        (field, flag)
        for field, flag in (
            ("window_time", "--window-time"),
            ("window_blocks", "--window-blocks"),
            ("window_epochs", "--window-epochs"),
        )
        if condition.get(field) is not None
    ]
    if len(windows) > 1:
        raise ValueError(
            "delta condition accepts zero windows (watcher-start baseline) "
            "or exactly one of window_time, window_blocks, window_epochs"
        )
    if windows:
        field, flag = windows[0]
        return [flag, str(condition[field])]
    return []


def _command_args(slug: str, sub_cmd: list[str], args: dict[str, Any]) -> list[str]:
    """Build the CLI's resource-id-first command segment."""
    if slug in {"token_price", "tx"}:
        field = "token" if slug == "token_price" else "tx_hash"
        identifier = [str(args[field])] if field in args else []
        if slug == "token_price":
            result = [sub_cmd[0], *identifier, *sub_cmd[1:]]
        else:
            result = [*sub_cmd, *identifier]
        return result
    if slug.startswith("subnet_"):
        identifier = [str(args["netuid"])] if "netuid" in args else []
        return [sub_cmd[0], *identifier, *sub_cmd[1:]]
    if slug.startswith("validator_"):
        identifier = [str(args["hotkey"])] if "hotkey" in args else []
        result = [sub_cmd[0], *identifier, *sub_cmd[1:]]
        if slug in {"validator_dividends_alpha", "validator_stake_alpha"} and "netuid" in args:
            result += ["--netuid", str(args["netuid"])]
        return result
    if slug.startswith("neuron_"):
        identifiers = [str(args[field]) for field in ("netuid", "hotkey") if field in args]
        return [sub_cmd[0], *identifiers, *sub_cmd[1:]]
    if slug.startswith("account_"):
        identifier = [str(args["coldkey"])] if "coldkey" in args else []
        return [sub_cmd[0], *identifier, *sub_cmd[1:]]
    return list(sub_cmd)


def _build_argv(  # noqa: PLR0912, PLR0915
    chain: str, tool_name: str, args: dict[str, Any]
) -> list[str]:
    """Translate MCP tool name + args into a chainwake CLI argv list."""
    if "out" in args:
        raise ValueError(
            "out is not supported for MCP tools because every match must exit "
            "and return its wake context to the awaiting agent"
        )
    exe = _find_chainwake()
    prefix = f"chainwake_{chain}_"
    if not tool_name.startswith(prefix):
        raise ValueError(f"unexpected tool name {tool_name!r} for chain {chain!r}")

    slug = tool_name[len(prefix) :]
    sub_cmd = _TOOL_TO_COMMAND.get((chain, slug))
    if sub_cmd is None:
        raise ValueError(f"no command mapping for tool {tool_name!r}")
    spec = tool_spec_for(slug, chain=chain)
    if spec is None:
        raise ValueError(f"no input model for tool {tool_name!r}")
    # The advertised schema is advisory at the MCP transport layer. Validate
    # it again here so direct clients cannot bypass checksum/range checks and
    # start a provider process with malformed input.
    spec.input_model.model_validate(args, extra="forbid")

    argv: list[str] = [exe, chain, *_command_args(slug, sub_cmd, args)]

    if slug in {
        "network_runtime_version",
        "subnet_hyperparams",
        "validator_child_keys",
    }:
        argv.append("--on-change")

    # --- condition flags ---
    condition = args.get("condition")
    if isinstance(condition, dict):
        argv += _condition_to_flags(condition)

    # --- liveness duration ---
    if (
        slug in {"account_activity", "neuron_last_update", "validator_weights"}
        and "silent_for" in args
    ):
        argv += ["--silent-for", str(args["silent_for"])]

    # --- tx finality ---
    if slug == "tx" and "finality" in args:
        argv += ["--finality", str(args["finality"])]
    if slug == "tx" and args.get("confirmations") is not None:
        argv += ["--confirmations", str(args["confirmations"])]

    # --- event type ---
    if slug == "event":
        event_type = args.get("event_type")
        type_raw = args.get("type_raw")
        if (event_type is None) == (type_raw is None):
            raise ValueError("exactly one of event_type or type_raw is required")
        if event_type is not None:
            argv += ["--type", str(args["event_type"])]
        else:
            raw_event = str(args["type_raw"])
            if not is_raw_event_type(raw_event):
                raise ValueError("type_raw must use Module.Event syntax")
            argv += ["--type-raw", raw_event]
        for field, flag in (
            ("from_addr", "--from"),
            ("to_addr", "--to"),
            ("amount_min", "--amount-min"),
            ("direction", "--direction"),
        ):
            if args.get(field) is not None:
                argv += [flag, str(args[field])]
        direction = args.get("direction")
        if direction == "in":
            if args.get("to_addr") is None:
                raise ValueError("event direction='in' requires to_addr")
            argv += ["--address", str(args["to_addr"])]
        elif direction == "out":
            if args.get("from_addr") is None:
                raise ValueError("event direction='out' requires from_addr")
            argv += ["--address", str(args["from_addr"])]

    # --- mechanism selection ---
    if slug == "validator_weights" and args.get("netuid") is not None:
        argv += ["--netuid", str(args["netuid"])]
    if (
        slug in {"neuron_incentive", "neuron_last_update", "validator_weights"}
        and args.get("mechid") is not None
    ):
        argv += ["--mechid", str(args["mechid"])]

    # --- computed-observable arguments ---
    if slug == "subnet_depth_for_trade":
        for field, flag in (("size", "--size"), ("max_bps", "--max-bps")):
            if args.get(field) is not None:
                argv += [flag, str(args[field])]

    # --- common optional flags ---
    for field, flag in _COMMON_FLAG_MAP.items():
        value = args.get(field)
        if value is not None:
            argv += [flag, str(value)]

    return argv


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """Terminate a child, escalating to kill after a short grace period."""
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=_TERMINATE_GRACE_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()


async def run_tool(
    chain: str,
    tool_name: str,
    args: dict[str, Any],
    *,
    timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Execute ``chainwake`` and return the parsed JSON result.

    Documented Chainwake exit codes 0-4 are application-level results, so their
    complete JSON payloads are returned to the agent regardless of status.
    Spawn failures, unexpected exit codes, and invalid output raise
    ``McpError``. A server-side timeout bounds every call even when the watcher
    omitted ``max_runtime``; timeout or task cancellation terminates the child
    process before returning.
    """
    argv = _build_argv(chain, tool_name, args)
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise McpError(
            ErrorData(
                code=-32603,
                message=f"chainwake process could not start: {exc}",
            )
        ) from exc
    try:
        stdout, _stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        await asyncio.shield(_terminate_process(process))
        raise McpError(
            ErrorData(
                code=-32603,
                message=f"chainwake tool call timed out after {timeout_seconds:g} seconds",
            )
        ) from None
    except asyncio.CancelledError:
        await asyncio.shield(_terminate_process(process))
        raise

    raw = stdout.decode("utf-8", errors="replace").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise McpError(
            ErrorData(
                code=-32603,
                message=f"chainwake produced non-JSON output: {raw!r}",
            )
        ) from None

    if not isinstance(payload, dict) or not isinstance(payload.get("status"), str):
        raise McpError(
            ErrorData(
                code=-32603,
                message="chainwake output must be a JSON object with a string status field",
            )
        )

    if process.returncode not in _CHAINWAKE_EXIT_CODES:
        raise McpError(
            ErrorData(
                code=-32603,
                message=(
                    f"chainwake exited {process.returncode}: {payload.get('status', 'unknown')}"
                ),
                data=payload,
            )
        )

    return payload


__all__ = ["DEFAULT_TOOL_TIMEOUT_SECONDS", "run_tool"]
