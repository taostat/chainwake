"""Interactive welcome banner for the bare ``chainwake`` command."""

from __future__ import annotations

from typing import TextIO

_WORDMARK = (
    "  BLOCKMACHINE // CHAINWAKE\n"
    "   ____ _           _                      _\n"
    "  / ___| |__   __ _(_)_ __ __      ____ _| | _____\n"
    " | |   | '_ \\ / _` | | '_ \\ \\ /\\ / / _` | |/ / _ \\\n"
    " | |___| | | | (_| | | | | |\\ V  V / (_| |   <  __/\n"
    "  \\____|_| |_|\\__,_|_|_| |_| \\_/\\_/ \\__,_|_|\\_\\___|\n"
)
_TAGLINE = "  Bittensor and EVM chain monitoring for AI agents\n\n"


def render_banner(stream: TextIO) -> None:
    """Render the static Blockmachine Chainwake wordmark."""
    stream.write(_WORDMARK)
    stream.write(_TAGLINE)
    stream.flush()


__all__ = ["render_banner"]
