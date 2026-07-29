"""Render durable job helpers separately from watcher output."""

from __future__ import annotations

import json
import sys
from typing import Any

from pydantic import BaseModel

from chainwake.output.render import RenderMode, render_mode_from_env


def _json_value(value: BaseModel | dict[str, Any]) -> dict[str, Any]:
    return value.model_dump(mode="json") if isinstance(value, BaseModel) else value


def emit_job_value(
    value: BaseModel | dict[str, Any],
    *,
    human: str,
) -> None:
    """Write JSON for agents/pipes and concise prose for an interactive TTY."""
    mode = render_mode_from_env()
    if mode is RenderMode.HUMAN or (mode is RenderMode.AUTO and sys.stdout.isatty()):
        print(human)
        return
    print(json.dumps(_json_value(value), indent=2, sort_keys=True, allow_nan=False))


__all__ = ["emit_job_value"]
