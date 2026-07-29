"""Distribution contracts for native Hermes and OpenClaw installs."""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
HERMES_SKILL = ROOT / "integrations" / "hermes" / "chainwake" / "SKILL.md"
OPENCLAW_SKILL = ROOT / "integrations" / "openclaw" / "chainwake" / "SKILL.md"


def _project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as source:
        return str(tomllib.load(source)["project"]["version"])


def _frontmatter(path: Path) -> dict[str, Any]:
    document = path.read_text(encoding="utf-8")
    assert document.startswith("---\n")
    _, raw, _ = document.split("---", 2)
    parsed = yaml.safe_load(raw)
    assert isinstance(parsed, dict)
    return parsed


def _load_hermes_plugin() -> ModuleType:
    plugin_path = ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location("chainwake_hermes_plugin", plugin_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_openclaw_skill_declares_one_click_uv_install() -> None:
    metadata = _frontmatter(OPENCLAW_SKILL)["metadata"]["openclaw"]

    assert metadata["requires"]["bins"] == ["chainwake"]
    assert metadata["install"] == [
        {
            "kind": "uv",
            "package": f"chainwake=={_project_version()}",
            "bins": ["chainwake"],
        }
    ]
    assert metadata["envVars"] == [
        {
            "name": "CHAINWAKE_BT_API_KEY",
            "required": False,
            "description": "Optional Blockmachine key for higher Bittensor rate limits.",
        },
        {
            "name": "CHAINWAKE_ETH_API_KEY",
            "required": False,
            "description": "Optional key for an authenticated Ethereum RPC endpoint.",
        },
        {
            "name": "CHAINWAKE_BASE_API_KEY",
            "required": False,
            "description": "Optional key for an authenticated Base RPC endpoint.",
        },
        {
            "name": "CHAINWAKE_BSC_API_KEY",
            "required": False,
            "description": "Optional key for an authenticated BSC RPC endpoint.",
        },
        {
            "name": "CHAINWAKE_COINGECKO_API_KEY",
            "required": False,
            "description": "Optional free Demo key after a CoinGecko price rate limit.",
        },
    ]


def test_hermes_repository_is_an_installable_plugin() -> None:
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))

    assert manifest == {
        "manifest_version": 1,
        "name": "blockmachine-chainwake",
        "version": _project_version(),
        "description": "Blockmachine Chainwake — event-driven chain monitoring for agents.",
        "author": "Blockmachine",
        "kind": "standalone",
        "hooks": ["pre_llm_call"],
    }


def test_hermes_plugin_registers_discoverable_bundled_skill() -> None:
    registrations: list[dict[str, Any]] = []
    hooks: list[tuple[str, Any]] = []

    class FakeContext:
        def register_skill(self, name: str, path: Path, description: str = "") -> None:
            registrations.append({"name": name, "path": path, "description": description})

        def register_hook(self, name: str, callback: Any) -> None:
            hooks.append((name, callback))

    module = _load_hermes_plugin()
    module.register(FakeContext())

    assert registrations == [
        {
            "name": "chainwake",
            "path": HERMES_SKILL,
            "description": (
                "Sleep until a chain condition matches, then resume from process output."
            ),
        }
    ]
    assert len(hooks) == 1
    hook_name, callback = hooks[0]
    assert hook_name == "pre_llm_call"
    assert callback(user_message="what can chainwake do?") == {
        "context": (
            "Blockmachine Chainwake is installed for event-driven Bittensor and "
            "Ethereum monitoring. "
            "For Chainwake or chain-monitoring requests, first load "
            "skill_view(name='blockmachine-chainwake:chainwake')."
        )
    }
