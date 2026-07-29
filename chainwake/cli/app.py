"""Cyclopts application construction.

The root app exposes ``--json`` and ``--no-json`` cross-cutting flags via a
cyclopts meta-app. The meta launcher writes ``CHAINWAKE_RENDER_MODE`` to
the environment so the dispatch layer can construct ``DefaultAdapter``
with the right :class:`~chainwake.output.render.RenderMode` without every
leaf command having to thread the flag through.

When neither flag is set, the default stdout adapter detects whether
stdout is attached to a TTY: TTY → human prose, pipe/redirect → JSON.
``--json`` forces JSON; ``--no-json`` (alias ``--human``) forces prose.
The two flags are mutually exclusive — supplying both is a user error.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Annotated, Never

import cyclopts

from chainwake import __version__
from chainwake.cli.chains.bittensor import build_bt_app
from chainwake.cli.chains.evm import build_evm_app
from chainwake.cli.jobs import build_daemon_app, build_jobs_app
from chainwake.cli.mcp import build_mcp_app
from chainwake.jobs.config import (
    DURABLE_ARGV_ENV_VAR,
    DURABLE_CONTEXT_ENV_VAR,
    DURABLE_ENV_VAR,
)
from chainwake.output.render import RENDER_MODE_ENV_VAR, RenderMode, emit_user_error
from chainwake.providers.evm import BASE_PROFILE, BSC_PROFILE, ETHEREUM_PROFILE


def _emit_render_flag_user_error(message: str) -> Never:
    """Emit a ``user_error`` envelope honouring render mode and exit 2.

    Used when ``--json`` and ``--no-json`` are both supplied. The request
    is invalid, but interactive callers still want prose; agents still
    want JSON. ``CHAINWAKE_RENDER_MODE`` (or TTY auto-detect) decides.
    """
    emit_user_error("invalid_input", message)
    sys.exit(2)


def _apply_render_flags(json_flag: bool, human_flag: bool) -> None:
    """Translate ``--json`` / ``--no-json`` into the render-mode env var."""
    if json_flag and human_flag:
        _emit_render_flag_user_error("--json and --no-json (--human) are mutually exclusive")
    if json_flag:
        os.environ[RENDER_MODE_ENV_VAR] = RenderMode.JSON.value
    elif human_flag:
        os.environ[RENDER_MODE_ENV_VAR] = RenderMode.HUMAN.value


def _apply_execution_flags(
    *,
    durable: bool,
    context: str | None,
    tokens: tuple[str, ...],
) -> None:
    """Capture one validated inner invocation for durable dispatch."""
    if context is not None and not durable:
        raise ValueError("--context requires --durable")
    if not durable:
        for name in (DURABLE_ENV_VAR, DURABLE_CONTEXT_ENV_VAR, DURABLE_ARGV_ENV_VAR):
            os.environ.pop(name, None)
        return
    os.environ[DURABLE_ENV_VAR] = "1"
    os.environ[DURABLE_ARGV_ENV_VAR] = json.dumps(list(tokens), separators=(",", ":"))
    if context:
        os.environ[DURABLE_CONTEXT_ENV_VAR] = context
    else:
        os.environ.pop(DURABLE_CONTEXT_ENV_VAR, None)


def build_app() -> cyclopts.App:
    app = cyclopts.App(
        name="chainwake",
        help="Chain monitoring for AI agents.",
        version=__version__,
        print_error=False,
    )
    app.command(build_bt_app(), name=["bt", "bittensor"])
    app.command(build_evm_app(ETHEREUM_PROFILE), name=["eth", "ethereum"])
    app.command(build_evm_app(BASE_PROFILE), name="base")
    app.command(build_evm_app(BSC_PROFILE), name="bsc")
    app.command(build_mcp_app())
    app.command(build_jobs_app())
    app.command(build_daemon_app())
    app.register_install_completion_command()

    # Let --help fall through the launcher to the inner app and the
    # id-first meta dispatcher in chains/bittensor.py. Otherwise cyclopts
    # intercepts help at the launcher level and routes it to whichever
    # command_app token traversal landed on (the resource, not the leaf).
    app.meta.help_flags = []

    @app.meta.default
    def _launcher(
        *tokens: Annotated[str, cyclopts.Parameter(allow_leading_hyphen=True)],
        json: Annotated[
            bool,
            cyclopts.Parameter(
                name="--json",
                help="Force JSON output regardless of TTY.",
                negative="",
            ),
        ] = False,
        no_json: Annotated[
            bool,
            cyclopts.Parameter(
                name=["--no-json", "--human"],
                help="Force human-readable output regardless of TTY.",
                negative="",
            ),
        ] = False,
        durable: Annotated[
            bool,
            cyclopts.Parameter(
                name="--durable",
                help="Persist this watcher as a background job and return immediately.",
                negative="",
            ),
        ] = False,
        context: Annotated[
            str | None,
            cyclopts.Parameter(
                name="--context",
                help="Context returned to the waiting agent when a durable job completes.",
            ),
        ] = None,
    ) -> None:
        _apply_render_flags(json, no_json)
        try:
            _apply_execution_flags(durable=durable, context=context, tokens=tokens)
        except ValueError as exc:
            _emit_render_flag_user_error(str(exc))
        command, bound, _ = app.parse_args(tokens)
        command(*bound.args, **bound.kwargs)

    return app
