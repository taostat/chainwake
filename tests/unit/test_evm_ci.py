"""Release contract for reproducible EVM integration infrastructure."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE = _ROOT / "tests" / "integration" / "docker" / "docker-compose.subtensor.yml"
_WORKFLOW = _ROOT / ".github" / "workflows" / "integration.yml"


def test_integration_compose_pins_official_multiarch_anvil_chains() -> None:
    compose = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    anvil = compose["services"]["anvil"]
    bsc_anvil = compose["services"]["bsc-anvil"]

    assert anvil["image"] == (
        "ghcr.io/foundry-rs/foundry:v1.7.1"
        "@sha256:8347b728d5d393dac1c018691b36f506d23b9dcd78341d40ea0fcb11c3a19cdd"
    )
    assert anvil["entrypoint"] == ["anvil"]
    assert anvil["command"] == [
        "--host",
        "0.0.0.0",  # noqa: S104 - verify the container binds its published port.
        "--port",
        "8545",
        "--chain-id",
        "1",
        "--no-mining",
    ]
    assert bsc_anvil["image"] == anvil["image"]
    assert bsc_anvil["entrypoint"] == ["anvil"]
    assert bsc_anvil["command"] == [
        "--host",
        "0.0.0.0",  # noqa: S104 - verify the container binds its published port.
        "--port",
        "8545",
        "--chain-id",
        "56",
        "--no-mining",
    ]
    assert bsc_anvil["ports"] == ["8546:8545"]


def test_integration_workflow_routes_evm_tests_to_compose_anvil() -> None:
    workflow = _WORKFLOW.read_text(encoding="utf-8")

    assert "compose_up; compose_up()" in workflow
    assert "CHAINWAKE_ETH_INTEGRATION_RPC_URL=ws://127.0.0.1:8545" in workflow
    assert "CHAINWAKE_BSC_INTEGRATION_RPC_URL=ws://127.0.0.1:8546" in workflow
