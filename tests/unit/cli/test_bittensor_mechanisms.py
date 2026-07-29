"""CLI and structured-input coverage for mechanism-aware observables."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from chainwake.cli.app import build_app
from chainwake.cli.inputs.common import BelowCondition
from chainwake.cli.inputs.neuron import NeuronIncentiveInput, NeuronLastUpdateInput
from chainwake.cli.inputs.validator import ValidatorWeightsInput
from chainwake.mcp.exec import _build_argv
from chainwake.mcp.tools import build_tools
from tests.ss58 import ALICE_SS58

pytestmark = pytest.mark.unit


def test_mechanism_aware_input_models_default_to_main_mechanism() -> None:
    incentive = NeuronIncentiveInput(
        netuid=19,
        hotkey=ALICE_SS58,
        condition=BelowCondition(value=0.1),
    )
    last_update = NeuronLastUpdateInput(
        netuid=19,
        hotkey=ALICE_SS58,
        silent_for="10blocks",
    )
    weights = ValidatorWeightsInput(hotkey=ALICE_SS58, silent_for="1epoch")

    assert incentive.mechid == 0
    assert last_update.mechid == 0
    assert weights.mechid == 0


def test_mechanism_aware_input_models_reject_out_of_range_mechid() -> None:
    with pytest.raises(ValidationError, match="less than or equal to 15"):
        NeuronIncentiveInput(
            netuid=19,
            hotkey=ALICE_SS58,
            mechid=16,
            condition=BelowCondition(value=0.1),
        )


def test_neuron_incentive_cli_forwards_mechid_as_read_arg() -> None:
    dispatch = AsyncMock(return_value=1)
    app = build_app()

    with (
        patch("chainwake.cli.chains.bittensor._dispatch_numeric", dispatch),
        pytest.raises(SystemExit) as exit_info,
    ):
        app(
            [
                "bt",
                "neuron",
                "19",
                ALICE_SS58,
                "incentive",
                "--mechid",
                "1",
                "--below",
                "0.1",
            ],
            exit_on_error=False,
        )

    assert exit_info.value.code == 1
    assert dispatch.await_args is not None
    assert dispatch.await_args.kwargs["read_args"] == {"mechid": 1}


def test_mcp_exposes_mechanism_aware_watcher_schemas() -> None:
    tools = {tool.name: tool for tool in build_tools("bt")}

    assert {
        "chainwake_bt_neuron_incentive",
        "chainwake_bt_neuron_last_update",
        "chainwake_bt_validator_weights",
    } <= tools.keys()
    for name in (
        "chainwake_bt_neuron_incentive",
        "chainwake_bt_neuron_last_update",
        "chainwake_bt_validator_weights",
    ):
        mechid = tools[name].inputSchema["properties"]["mechid"]
        assert mechid["default"] == 0
        assert mechid["maximum"] == 15


def test_mcp_neuron_incentive_forwards_mechid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chainwake.mcp.exec._find_chainwake", lambda: "/bin/chainwake")

    argv = _build_argv(
        "bt",
        "chainwake_bt_neuron_incentive",
        {
            "netuid": 19,
            "hotkey": ALICE_SS58,
            "mechid": 1,
            "condition": {"kind": "below", "value": 0.1},
        },
    )

    assert argv == [
        "/bin/chainwake",
        "bt",
        "neuron",
        "19",
        ALICE_SS58,
        "incentive",
        "--below",
        "0.1",
        "--mechid",
        "1",
    ]
