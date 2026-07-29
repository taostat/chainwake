"""chainwake entry point."""

from __future__ import annotations

import sys

import cyclopts

from chainwake.cli.app import build_app
from chainwake.cli.banner import render_banner
from chainwake.output.render import emit_user_error


def main() -> None:
    app = build_app()
    try:
        if not sys.argv[1:]:
            render_banner(sys.stdout)
        # Route via the meta-app so the cross-cutting --json / --no-json
        # flags are parsed before the inner command runs.
        app.meta(sys.argv[1:], exit_on_error=False)
    except cyclopts.CycloptsError as exc:
        # Cyclopts parse errors (missing argument, unknown option, etc.)
        # become user_error envelopes via the shared, render-mode-aware
        # emitter — interactive callers see prose, agents see JSON.
        emit_user_error("invalid_input", str(exc))
        sys.exit(2)
    except SystemExit:
        raise


if __name__ == "__main__":
    main()
