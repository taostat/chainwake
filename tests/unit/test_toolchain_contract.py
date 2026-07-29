"""Release contracts for Chainwake's supported Bittensor toolchain.

These tests intentionally inspect the committed project and CI configuration.
They keep dependency, container, and test-isolation decisions reviewable rather
than relying on whatever happens to be installed on a developer machine.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import conftest as root_conftest
from tests.integration.harness import local_chain

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SPEC_440_LOCALNET = (
    "ghcr.io/raofoundation/subtensor-localnet:"
    "sha-e4ffa2e1325c6c7db618dbceaf396310a170990c"
    "@sha256:645b7e0772cc7c062b8e46f9ddd8b3b6a3106e3e3683bf2e4fdc5caac219bc67"
)


def _project_dependencies() -> set[str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return set(project["project"]["dependencies"])


def test_runtime_dependencies_use_current_sdk_independent_chain_client() -> None:
    dependencies = _project_dependencies()

    assert "async-substrate-interface==2.2.1" in dependencies
    assert "mcp==1.28.1" in dependencies
    assert "starlette==1.3.1" in dependencies
    assert not any(item.partition("==")[0] == "bittensor" for item in dependencies)


def test_release_metadata_uses_taostat_ownership_and_blockmachine_branding() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]

    assert project["authors"] == [{"name": "Blockmachine", "email": "hello@blockmachine.io"}]
    assert project["urls"] == {
        "Homepage": "https://github.com/taostat/chainwake",
        "Documentation": "https://github.com/taostat/chainwake/tree/main/docs",
        "Repository": "https://github.com/taostat/chainwake",
        "Issues": "https://github.com/taostat/chainwake/issues",
    }


def test_localnet_is_official_spec_440_and_immutable() -> None:
    compose = (ROOT / "tests/integration/docker/docker-compose.subtensor.yml").read_text()

    assert SPEC_440_LOCALNET in compose
    assert "opentensor/subtensor-localnet" not in compose
    assert ":latest" not in compose


def test_tag_publish_requires_release_validation_before_build_and_publish() -> None:
    workflow = (ROOT / ".github/workflows/publish.yml").read_text()
    integration_workflow = (ROOT / ".github/workflows/integration.yml").read_text()

    assert "validate-chainwake:" in workflow
    assert "integration-chainwake:" in workflow
    assert "uses: ./.github/workflows/integration.yml" in workflow
    assert "workflow_call:" in integration_workflow
    assert 'scripts/check_release_version.py "$GITHUB_REF_NAME"' in workflow
    for required_gate in (
        "uv sync --locked --dev",
        "uv run ruff check",
        "uv run ruff format --check",
        "uv run ty check",
        "scripts/generate_json_schema.py --check",
        "uv run pytest -m unit -n auto",
        "pip-audit",
    ):
        assert required_gate in workflow
    assert "needs: [validate-chainwake, integration-chainwake]" in workflow
    assert "needs: build-chainwake" in workflow
    assert "environment: pypi" in workflow


def test_precommit_checks_use_the_project_environment() -> None:
    hooks = (ROOT / ".pre-commit-config.yaml").read_text()

    assert "entry: uv run ty check" in hooks
    assert "entry: uv run ruff check --force-exclude" in hooks
    assert "entry: uv run ruff format --check --force-exclude" in hooks
    assert "--exclude" not in hooks


def test_release_version_checker_rejects_mismatched_tag() -> None:
    checker = ROOT / "scripts/check_release_version.py"

    valid = subprocess.run(
        [sys.executable, str(checker), "v0.5.0"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    invalid = subprocess.run(
        [sys.executable, str(checker), "v9.9.9"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert valid.returncode == 0, valid.stderr
    assert invalid.returncode != 0
    assert "does not match" in invalid.stderr


def test_release_version_checker_covers_hermes_entrypoint() -> None:
    checker = (ROOT / "scripts" / "check_release_version.py").read_text()

    assert '"__init__.py"' in checker
    assert '"Hermes entry-point version"' in checker


def test_unit_xdist_run_does_not_boot_integration_localnet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = SimpleNamespace(
        option=SimpleNamespace(numprocesses=4, markexpr="unit"),
        workerinput=None,
    )

    def unexpected_compose_up() -> None:
        pytest.fail("unit collection must not start Docker")

    def unexpected_asyncio_run(_coroutine: Any) -> None:
        pytest.fail("unit collection must not bootstrap chain state")

    monkeypatch.setattr(root_conftest, "compose_up", unexpected_compose_up)
    monkeypatch.setattr(asyncio, "run", unexpected_asyncio_run)

    root_conftest.pytest_configure(cast(pytest.Config, config))


def test_compose_up_waits_for_json_rpc_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_run(*_args: object, **_kwargs: object) -> None:
        calls.append("compose")

    async def fake_wait() -> None:
        calls.append("ready")

    def fake_asyncio_run(coroutine: Any) -> None:
        with pytest.raises(StopIteration):
            coroutine.send(None)

    monkeypatch.setattr(local_chain.subprocess, "run", fake_run)
    monkeypatch.setattr(local_chain, "_wait_for_local_chains_ready", fake_wait)
    monkeypatch.setattr(local_chain.asyncio, "run", fake_asyncio_run)

    local_chain.compose_up()

    assert calls == ["compose", "ready"]
