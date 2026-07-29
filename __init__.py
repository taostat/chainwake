"""Hermes plugin entry point for the Chainwake repository.

The repository directory is itself named ``chainwake``. Pytest therefore
imports this file as the top-level package while collecting tests from the
checkout. Point the package search path at the real Python package directory
so ``chainwake.cli`` and the rest of the installed-package surface resolve
identically from a source checkout.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

__version__ = "0.5.0"
_ROOT = Path(__file__).resolve().parent
__path__ = [str(_ROOT / "chainwake"), str(_ROOT)]


class SkillRegistrar(Protocol):
    """Subset of Hermes' plugin context used by Chainwake."""

    def register_skill(self, name: str, path: Path, description: str = "") -> None:
        """Register a plugin-provided skill."""

    def register_hook(
        self,
        name: str,
        callback: Callable[..., dict[str, str]],
    ) -> None:
        """Register a plugin lifecycle hook."""


def _discovery_context(**_: object) -> dict[str, str]:
    """Tell Hermes how to discover Chainwake without loading the full skill every turn."""
    return {
        "context": (
            "Blockmachine Chainwake is installed for event-driven Bittensor and "
            "Ethereum monitoring. "
            "For Chainwake or chain-monitoring requests, first load "
            "skill_view(name='blockmachine-chainwake:chainwake')."
        )
    }


def register(ctx: SkillRegistrar) -> None:
    """Register Chainwake's Hermes skill and its lightweight discovery hint."""
    skill = Path(__file__).resolve().parent / "integrations" / "hermes" / "chainwake" / "SKILL.md"
    ctx.register_skill(
        "chainwake",
        skill,
        description="Sleep until a chain condition matches, then resume from process output.",
    )
    ctx.register_hook("pre_llm_call", _discovery_context)
