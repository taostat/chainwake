"""Unit tests for MCP tool generation from Pydantic input models.

Covers:
- Correct tool count and names
- Input schema correctness derived from model_json_schema()
- Required / optional field classification
- Discriminated condition unions surface the expected variants
- Unknown chain returns empty list
"""

from __future__ import annotations

import json
from copy import deepcopy

import pytest
from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate
from pydantic import ValidationError

from chainwake.cli.chains.bittensor import BT_WIRED_WAKE_COMMANDS
from chainwake.cli.inputs.common import DropPctCondition, MovePctCondition, RisePctCondition
from chainwake.core.registry import all_entries
from chainwake.mcp.tools import TOOL_SPECS, build_tools

# --- helpers ---


def _find_tool(tools, name):
    return next((t for t in tools if t.name == name), None)


# === build_tools count and names ===


@pytest.mark.unit
def test_build_tools_returns_every_wired_wake():
    tools = build_tools("bt")
    assert len(tools) == 33


@pytest.mark.unit
def test_build_tools_unknown_chain_returns_empty():
    assert build_tools("sol") == []


@pytest.mark.unit
def test_build_tools_names_are_unique():
    tools = build_tools("bt")
    names = [t.name for t in tools]
    assert len(names) == len(set(names))


@pytest.mark.unit
def test_build_tools_expected_names():
    tools = build_tools("bt")
    names = {t.name for t in tools}
    assert names == {
        "chainwake_bt_account_activity",
        "chainwake_bt_account_balance",
        "chainwake_bt_subnet_price",
        "chainwake_bt_subnet_registration_cost",
        "chainwake_bt_subnet_tao_depth",
        "chainwake_bt_subnet_alpha_depth",
        "chainwake_bt_subnet_depth_for_trade",
        "chainwake_bt_subnet_alpha_supply",
        "chainwake_bt_subnet_moving_price",
        "chainwake_bt_subnet_volume",
        "chainwake_bt_subnet_emission_share",
        "chainwake_bt_subnet_burn_rate",
        "chainwake_bt_subnet_ema_tao_flow",
        "chainwake_bt_subnet_hyperparams",
        "chainwake_bt_subnet_identity",
        "chainwake_bt_tx",
        "chainwake_bt_event",
        "chainwake_bt_validator_commission",
        "chainwake_bt_validator_dividends_alpha",
        "chainwake_bt_validator_stake_alpha",
        "chainwake_bt_validator_child_keys",
        "chainwake_bt_validator_identity",
        "chainwake_bt_neuron_stake_alpha",
        "chainwake_bt_neuron_dividends",
        "chainwake_bt_neuron_incentive",
        "chainwake_bt_neuron_last_update",
        "chainwake_bt_neuron_blocks_until_immunity_expires",
        "chainwake_bt_validator_weights",
        "chainwake_bt_network_subnet_registration_cost",
        "chainwake_bt_network_subnet_count",
        "chainwake_bt_network_tao_price",
        "chainwake_bt_network_runtime_version",
        "chainwake_bt_network_on_runtime_upgraded",
    }


@pytest.mark.unit
def test_every_tool_exposes_ru_budget():
    for tool in build_tools("bt"):
        assert "max_ru" in tool.inputSchema["properties"], tool.name


@pytest.mark.unit
def test_mcp_tools_never_advertise_non_exiting_output_adapters():
    for tool in build_tools("bt"):
        assert "out" not in tool.inputSchema["properties"], tool.name


@pytest.mark.unit
def test_tao_price_only_offers_wall_clock_delta_windows():
    tool = _find_tool(build_tools("bt"), "chainwake_bt_network_tao_price")
    assert tool is not None

    for name in (
        "ExternalPriceDropPctCondition",
        "ExternalPriceRisePctCondition",
        "ExternalPriceMovePctCondition",
    ):
        properties = tool.inputSchema["$defs"][name]["properties"]
        assert "window_time" in properties
        assert "window_blocks" not in properties
        assert "window_epochs" not in properties


@pytest.mark.unit
def test_every_tool_schema_is_its_complete_pydantic_model_minus_cli_only_out():
    tools = {tool.name: tool for tool in build_tools("bt")}

    for spec in TOOL_SPECS:
        expected = deepcopy(spec.input_model.model_json_schema())
        expected["properties"].pop("out", None)
        if "required" in expected:
            expected["required"] = [field for field in expected["required"] if field != "out"]
        assert tools[spec.name].inputSchema == expected, spec.name


@pytest.mark.unit
def test_tool_manifest_covers_every_approved_registry_path():
    registry_paths = {entry.path_template for entry in all_entries()}
    manifest_paths = {path for spec in TOOL_SPECS for path in spec.registry_paths}

    assert manifest_paths == registry_paths


@pytest.mark.unit
def test_tool_manifest_covers_every_wired_cli_wake():
    assert {spec.command for spec in TOOL_SPECS} == BT_WIRED_WAKE_COMMANDS


@pytest.mark.unit
def test_tool_manifest_names_commands_and_paths_are_unique():
    names = [spec.name for spec in TOOL_SPECS]
    commands = [spec.command for spec in TOOL_SPECS]
    paths = [path for spec in TOOL_SPECS for path in spec.registry_paths]

    assert len(names) == len(set(names))
    assert len(commands) == len(set(commands))
    assert len(paths) == len(set(paths))


@pytest.mark.unit
def test_runtime_tools_are_backed_by_network_input_models():
    tools = build_tools("bt")
    runtime_version = _find_tool(tools, "chainwake_bt_network_runtime_version")
    runtime_upgrade = _find_tool(tools, "chainwake_bt_network_on_runtime_upgraded")

    assert runtime_version is not None
    assert runtime_upgrade is not None
    assert "condition" not in runtime_version.inputSchema["properties"]
    assert "event_type" not in runtime_upgrade.inputSchema["properties"]


# === subnet_price tool ===


@pytest.mark.unit
def test_subnet_price_tool_exists():
    tools = build_tools("bt")
    assert _find_tool(tools, "chainwake_bt_subnet_price") is not None


@pytest.mark.unit
def test_subnet_price_description_mentions_condition():
    tools = build_tools("bt")
    tool = _find_tool(tools, "chainwake_bt_subnet_price")
    assert tool is not None
    assert "threshold" in tool.description or "condition" in tool.description


@pytest.mark.unit
def test_subnet_price_netuid_is_required():
    tools = build_tools("bt")
    tool = _find_tool(tools, "chainwake_bt_subnet_price")
    assert tool is not None
    schema = tool.inputSchema
    assert "netuid" in schema.get("required", [])


@pytest.mark.unit
def test_subnet_price_netuid_is_integer():
    tools = build_tools("bt")
    tool = _find_tool(tools, "chainwake_bt_subnet_price")
    assert tool is not None
    props = tool.inputSchema["properties"]
    assert props["netuid"]["type"] == "integer"


@pytest.mark.unit
def test_subnet_price_condition_is_required():
    tools = build_tools("bt")
    tool = _find_tool(tools, "chainwake_bt_subnet_price")
    assert tool is not None
    assert "condition" in tool.inputSchema.get("required", [])


@pytest.mark.unit
def test_subnet_price_has_common_fields():
    tools = build_tools("bt")
    tool = _find_tool(tools, "chainwake_bt_subnet_price")
    assert tool is not None
    props = tool.inputSchema["properties"]
    for field in ("rpc_url", "name", "max_runtime", "max_ru"):
        assert field in props, f"missing common field {field!r}"


@pytest.mark.unit
def test_subnet_price_condition_variants_include_threshold_and_delta():
    """Discriminated union must include all 5 condition variants."""
    tools = build_tools("bt")
    tool = _find_tool(tools, "chainwake_bt_subnet_price")
    assert tool is not None
    schema = tool.inputSchema
    # Discriminated union definitions land in $defs; check that all 5 variants appear.
    defs = schema.get("$defs", {})
    def_names = set(defs.keys())
    expected_variants = {
        "BelowCondition",
        "AboveCondition",
        "DropPctCondition",
        "RisePctCondition",
        "MovePctCondition",
    }
    assert expected_variants.issubset(def_names), (
        f"missing condition variants: {expected_variants - def_names}"
    )


@pytest.mark.unit
def test_subnet_price_delta_windows_are_optional_watcher_start_overrides():
    tool = _find_tool(build_tools("bt"), "chainwake_bt_subnet_price")
    assert tool is not None
    drop_schema = tool.inputSchema["$defs"]["DropPctCondition"]

    assert set(drop_schema["required"]) == {"pct"}
    description = drop_schema["properties"]["window_time"]["description"]
    assert "first successful observation" in description


@pytest.mark.unit
@pytest.mark.parametrize("model", [DropPctCondition, RisePctCondition, MovePctCondition])
def test_delta_condition_models_allow_ever_or_one_explicit_window(model):
    assert model(pct=1).window_time is None
    assert model(pct=1, window_time="1h").window_time == "1h"
    assert model(pct=1, window_blocks=12).window_blocks == 12
    assert model(pct=1, window_epochs=2).window_epochs == 2


@pytest.mark.unit
@pytest.mark.parametrize("model", [DropPctCondition, RisePctCondition, MovePctCondition])
def test_delta_condition_models_reject_multiple_windows(model):
    with pytest.raises(ValidationError, match="mutually exclusive"):
        model(pct=1, window_time="1h", window_blocks=12)


@pytest.mark.unit
@pytest.mark.parametrize(
    "windows",
    [
        {},
        {"window_time": None, "window_blocks": None, "window_epochs": None},
        {"window_time": "1h"},
        {"window_blocks": 12},
        {"window_epochs": 2},
    ],
)
def test_advertised_delta_schema_accepts_ever_or_one_explicit_window(windows):
    tool = _find_tool(build_tools("bt"), "chainwake_bt_subnet_price")
    assert tool is not None
    validate(
        instance={
            "netuid": 28,
            "condition": {"kind": "move-pct", "pct": 1, **windows},
        },
        schema=tool.inputSchema,
    )


@pytest.mark.unit
def test_advertised_delta_schema_rejects_multiple_non_null_windows():
    tool = _find_tool(build_tools("bt"), "chainwake_bt_subnet_price")
    assert tool is not None
    with pytest.raises(JsonSchemaValidationError):
        validate(
            instance={
                "netuid": 28,
                "condition": {
                    "kind": "move-pct",
                    "pct": 1,
                    "window_time": "1h",
                    "window_blocks": 12,
                },
            },
            schema=tool.inputSchema,
        )


# === tx tool ===


@pytest.mark.unit
def test_tx_tool_exists():
    tools = build_tools("bt")
    assert _find_tool(tools, "chainwake_bt_tx") is not None


@pytest.mark.unit
def test_tx_tx_hash_is_required():
    tools = build_tools("bt")
    tool = _find_tool(tools, "chainwake_bt_tx")
    assert tool is not None
    assert "tx_hash" in tool.inputSchema.get("required", [])


@pytest.mark.unit
def test_tx_finality_is_required():
    tools = build_tools("bt")
    tool = _find_tool(tools, "chainwake_bt_tx")
    assert tool is not None
    assert "finality" in tool.inputSchema.get("required", [])


@pytest.mark.unit
def test_tx_finality_is_enum():
    tools = build_tools("bt")
    tool = _find_tool(tools, "chainwake_bt_tx")
    assert tool is not None
    props = tool.inputSchema["properties"]
    finality = props["finality"]
    # Literal["included", "finalized"] renders as enum in JSON Schema.
    assert "enum" in finality or finality.get("type") == "string"


# === event tool ===


@pytest.mark.unit
def test_event_tool_exists():
    tools = build_tools("bt")
    assert _find_tool(tools, "chainwake_bt_event") is not None


@pytest.mark.unit
def test_event_has_event_type_field():
    tools = build_tools("bt")
    tool = _find_tool(tools, "chainwake_bt_event")
    assert tool is not None
    assert "event_type" in tool.inputSchema["properties"]


@pytest.mark.unit
def test_event_has_type_raw_field():
    tools = build_tools("bt")
    tool = _find_tool(tools, "chainwake_bt_event")
    assert tool is not None
    assert "type_raw" in tool.inputSchema["properties"]


@pytest.mark.unit
def test_event_schema_requires_exactly_one_type():
    tools = build_tools("bt")
    tool = _find_tool(tools, "chainwake_bt_event")
    assert tool is not None
    assert tool.inputSchema["oneOf"] == [
        {"required": ["event_type"], "not": {"required": ["type_raw"]}},
        {"required": ["type_raw"], "not": {"required": ["event_type"]}},
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    [
        "chainwake_bt_validator_dividends_alpha",
        "chainwake_bt_validator_stake_alpha",
        "chainwake_bt_neuron_stake_alpha",
    ],
)
def test_stake_unit_tools_require_netuid_hotkey_and_describe_alpha(name):
    tool = _find_tool(build_tools("bt"), name)
    assert tool is not None
    required = set(tool.inputSchema.get("required", []))
    assert {"netuid", "hotkey", "condition"}.issubset(required)
    assert "alpha" in tool.description.lower()
    assert "TAO" not in tool.description


@pytest.mark.unit
def test_neuron_immunity_tool_is_block_based_and_requires_identity_and_threshold():
    tool = _find_tool(build_tools("bt"), "chainwake_bt_neuron_blocks_until_immunity_expires")
    assert tool is not None
    required = set(tool.inputSchema.get("required", []))
    assert {"netuid", "hotkey", "condition"}.issubset(required)
    assert "blocks" in tool.description.lower()
    assert "epoch" not in tool.description.lower()


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    ["chainwake_bt_subnet_identity", "chainwake_bt_validator_identity"],
)
def test_structured_identity_tools_advertise_only_on_change(name):
    tool = _find_tool(build_tools("bt"), name)
    assert tool is not None
    condition = tool.inputSchema["properties"]["condition"]
    assert condition["$ref"].endswith("/OnChangeCondition")
    assert "on-change" in tool.description


@pytest.mark.unit
def test_commission_tool_advertises_numeric_fraction_targets():
    tool = _find_tool(build_tools("bt"), "chainwake_bt_validator_commission")
    assert tool is not None
    schema_text = json.dumps(tool.inputSchema)
    assert "CommissionChangesToCondition" in schema_text
    assert '"minimum": 0' in schema_text
    assert '"maximum": 1' in schema_text
