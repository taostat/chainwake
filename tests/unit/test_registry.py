"""Registry validation, lookup, and path-template parsing tests."""

from __future__ import annotations

from typing import cast

import pytest

from chainwake.core.registry import (
    FRIENDLY_EVENT_MAP,
    VALID_OBSERVABLE_TYPES,
    VALID_PRIMITIVES,
    ObservableRegistry,
    ObservableType,
    PrimitiveName,
    RegistryEntry,
    all_entries,
    lookup,
    lookup_friendly_event,
    register,
    reset_for_testing,
)
from chainwake.providers.base import Cadence

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    reset_for_testing()


# ---------------------------------------------------------------------------
# Core seed entries
# ---------------------------------------------------------------------------


def test_phase0_seeds_are_present() -> None:
    paths = {entry.path_template for entry in all_entries()}
    assert "subnet.{netuid}.pool.price" in paths
    assert "tx.{tx_hash}" in paths


def test_lookup_returns_entry() -> None:
    entry = lookup("subnet.{netuid}.pool.price")
    assert entry.resource == "subnet"
    assert entry.type == "numeric"
    assert entry.natural_cadence == Cadence.PER_BLOCK
    assert entry.applicable_primitives == ("threshold", "delta")


@pytest.mark.parametrize(
    "path",
    [
        "subnet.{netuid}.registration-cost",
        "network.subnet-registration-cost",
    ],
)
def test_registration_cost_registry_is_threshold_only(path: str) -> None:
    assert lookup(path).applicable_primitives == ("threshold",)


def test_tao_usd_price_uses_external_timer_policy() -> None:
    entry = lookup("network.tao-price")

    assert entry.resource == "network"
    assert entry.natural_cadence == Cadence.OTHER
    assert entry.subscription_supported is False
    assert entry.applicable_primitives == ("threshold", "delta")
    assert entry.observation_policy.default_poll_seconds == 60.0


def test_lookup_unknown_raises() -> None:
    with pytest.raises(KeyError, match="no registry entry"):
        lookup("subnet.does-not-exist")


def test_registry_scopes_identical_paths_by_chain() -> None:
    registry = ObservableRegistry()
    bittensor = RegistryEntry(
        chain="bt",
        path_template="network.height",
        resource="network",
        type="numeric",
        natural_cadence=Cadence.PER_BLOCK,
        subscription_supported=True,
        applicable_primitives=("threshold",),
        description="Bittensor height.",
    )
    ethereum = RegistryEntry(
        chain="eth",
        path_template="network.height",
        resource="network",
        type="numeric",
        natural_cadence=Cadence.PER_BLOCK,
        subscription_supported=True,
        applicable_primitives=("threshold",),
        description="Ethereum height.",
    )

    registry.register(bittensor)
    registry.register(ethereum)

    assert registry.lookup("bt", "network.height") is bittensor
    assert registry.lookup("eth", "network.height") is ethereum


def test_registry_rejects_path_registered_twice_for_same_chain() -> None:
    registry = ObservableRegistry()
    entry = RegistryEntry(
        chain="eth",
        path_template="network.height",
        resource="network",
        type="numeric",
        natural_cadence=Cadence.PER_BLOCK,
        subscription_supported=True,
        applicable_primitives=("threshold",),
        description="Ethereum height.",
    )
    registry.register(entry)

    with pytest.raises(ValueError, match="duplicate registry entry"):
        registry.register(entry)


def test_default_lookup_is_explicitly_bittensor_compatible() -> None:
    assert lookup("subnet.{netuid}.pool.price").chain == "bt"
    assert lookup("subnet.{netuid}.pool.price", chain="bt").chain == "bt"

    with pytest.raises(KeyError, match="no registry entry"):
        lookup("subnet.{netuid}.pool.price", chain="eth")


def test_ethereum_path_does_not_inherit_bittensor_storage_binding() -> None:
    entry = RegistryEntry(
        chain="eth",
        path_template="network.runtime-version",
        resource="network",
        type="state-bytes",
        natural_cadence=Cadence.PER_EVENT,
        subscription_supported=True,
        applicable_primitives=("state",),
        description="Ethereum client version.",
    )

    assert entry.observation_policy.storage_bindings == ()


def test_friendly_events_are_scoped_by_chain() -> None:
    assert lookup_friendly_event("transfer", chain="bt") == ["Balances.Transfer"]

    with pytest.raises(KeyError, match="unknown friendly event"):
        lookup_friendly_event("transfer", chain="eth")


@pytest.mark.parametrize(
    "path",
    [
        "neuron.{netuid}.{hotkey}.pruning-score",
        "neuron.{netuid}.{hotkey}.blocks-until-deregistration",
    ],
)
def test_current_subtensor_pruning_observables_are_not_registered(path: str) -> None:
    """Current pruning is replacement-time ranking, not a score or countdown."""
    with pytest.raises(KeyError, match="no registry entry"):
        lookup(path)


def test_register_rejects_duplicate() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        register(
            RegistryEntry(
                path_template="subnet.{netuid}.pool.price",
                resource="subnet",
                type="numeric",
                natural_cadence=Cadence.PER_BLOCK,
                subscription_supported=False,
                applicable_primitives=("threshold",),
                description="dup",
            )
        )


def test_path_template_extracts_params() -> None:
    entry = lookup("subnet.{netuid}.pool.price")
    assert entry.path_params == ("netuid",)


def test_render_path_substitutes_param() -> None:
    entry = lookup("subnet.{netuid}.pool.price")
    assert entry.render_path({"netuid": "19"}) == "subnet.19.pool.price"


def test_render_path_missing_param_raises() -> None:
    entry = lookup("subnet.{netuid}.pool.price")
    with pytest.raises(KeyError, match="missing path params"):
        entry.render_path({})


def test_render_path_unknown_param_raises() -> None:
    entry = lookup("subnet.{netuid}.pool.price")
    with pytest.raises(KeyError, match="unknown path param"):
        entry.render_path({"netuid": "19", "extra": "x"})


def test_event_entry_no_params() -> None:
    entry = lookup("network.--on-runtime-upgraded")
    assert entry.path_params == ()
    assert entry.render_path({}) == "network.--on-runtime-upgraded"


def test_invalid_observable_type_rejected() -> None:
    bogus_type = cast("ObservableType", "not-a-type")
    with pytest.raises(ValueError, match="invalid observable type"):
        RegistryEntry(
            path_template="subnet.{netuid}.junk",
            resource="subnet",
            type=bogus_type,
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=False,
            applicable_primitives=("threshold",),
            description="x",
        )


def test_empty_applicable_primitives_rejected() -> None:
    with pytest.raises(ValueError, match="applicable_primitives must be non-empty"):
        RegistryEntry(
            path_template="subnet.{netuid}.junk",
            resource="subnet",
            type="numeric",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=False,
            applicable_primitives=(),
            description="x",
        )


def test_unknown_primitive_rejected() -> None:
    bogus_primitives = cast("tuple[PrimitiveName, ...]", ("bogus",))
    with pytest.raises(ValueError, match="unknown primitive"):
        RegistryEntry(
            path_template="subnet.{netuid}.junk",
            resource="subnet",
            type="numeric",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=False,
            applicable_primitives=bogus_primitives,
            description="x",
        )


def test_computed_requires_args() -> None:
    with pytest.raises(ValueError, match="must declare computed_args"):
        RegistryEntry(
            path_template="subnet.{netuid}.depth-for-trade",
            resource="subnet",
            type="numeric",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=False,
            applicable_primitives=("threshold",),
            description="x",
            computed=True,
        )


def test_computed_with_args_ok() -> None:
    entry = RegistryEntry(
        path_template="subnet.{netuid}.depth-for-trade",
        resource="subnet",
        type="numeric",
        natural_cadence=Cadence.PER_BLOCK,
        subscription_supported=False,
        applicable_primitives=("threshold",),
        description="x",
        computed=True,
        computed_args=("size", "max-bps"),
    )
    assert entry.computed_args == ("size", "max-bps")


def test_read_cost_defaults_to_one() -> None:
    entry = RegistryEntry(
        path_template="subnet.{netuid}.junk-default",
        resource="subnet",
        type="numeric",
        natural_cadence=Cadence.PER_BLOCK,
        subscription_supported=False,
        applicable_primitives=("threshold",),
        description="x",
    )
    assert entry.read_cost == 1


def test_read_cost_explicit_value() -> None:
    entry = RegistryEntry(
        path_template="subnet.{netuid}.junk-explicit",
        resource="subnet",
        type="numeric",
        natural_cadence=Cadence.PER_BLOCK,
        subscription_supported=False,
        applicable_primitives=("threshold",),
        description="x",
        read_cost=4,
    )
    assert entry.read_cost == 4


def test_read_cost_zero_rejected() -> None:
    with pytest.raises(ValueError, match="read_cost must be >= 1"):
        RegistryEntry(
            path_template="subnet.{netuid}.junk-zero",
            resource="subnet",
            type="numeric",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=False,
            applicable_primitives=("threshold",),
            description="x",
            read_cost=0,
        )


def test_invalid_path_template_segment_rejected() -> None:
    with pytest.raises(ValueError, match="invalid path_template segment"):
        RegistryEntry(
            path_template="subnet.BadCase.x",
            resource="subnet",
            type="numeric",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=False,
            applicable_primitives=("threshold",),
            description="x",
        )


def test_empty_path_template_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        RegistryEntry(
            path_template="",
            resource="subnet",
            type="numeric",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=False,
            applicable_primitives=("threshold",),
            description="x",
        )


def test_constants_match_protocol_aliases() -> None:
    assert "threshold" in VALID_PRIMITIVES
    assert "tx" in VALID_PRIMITIVES
    assert "numeric" in VALID_OBSERVABLE_TYPES
    assert "tx-status" in VALID_OBSERVABLE_TYPES


# ---------------------------------------------------------------------------
# Supported observable coverage
# ---------------------------------------------------------------------------

# All supported canonical path templates that must be present.
_SUPPORTED_PATHS = [
    # subnet
    "subnet.{netuid}.pool.price",
    "subnet.{netuid}.pool.tao-depth",
    "subnet.{netuid}.pool.alpha-depth",
    "subnet.{netuid}.pool.depth-for-trade",
    "subnet.{netuid}.registration-cost",
    "subnet.{netuid}.emission-share",
    "subnet.{netuid}.burn-rate",
    "subnet.{netuid}.pool.alpha-supply",
    "subnet.{netuid}.pool.moving-price",
    "subnet.{netuid}.pool.volume",
    "subnet.{netuid}.hyperparams",
    "subnet.{netuid}.identity",
    # validator
    "validator.{netuid}.{hotkey}.dividends-alpha",
    "validator.{netuid}.{hotkey}.stake-alpha",
    "validator.{hotkey}.commission",
    "validator.{hotkey}.weights",
    "validator.{hotkey}.child-keys",
    "validator.{hotkey}.identity",
    # neuron
    "neuron.{netuid}.{hotkey}.incentive",
    "neuron.{netuid}.{hotkey}.dividends",
    "neuron.{netuid}.{hotkey}.stake-alpha",
    "neuron.{netuid}.{hotkey}.last-update",
    "neuron.{netuid}.{hotkey}.blocks-until-immunity-expires",
    # account
    "account.{coldkey}.balance",
    "account.{coldkey}.activity",
    # network
    "network.subnet-registration-cost",
    "network.tao-price",
    "network.runtime-version",
    "network.subnet-count",
    "network.--on-runtime-upgraded",
    # tx
    "tx.{tx_hash}",
    # event firehose
    "event.--type-raw",
]


@pytest.mark.parametrize("path_template", _SUPPORTED_PATHS)
def test_supported_entry_present(path_template: str) -> None:
    entry = lookup(path_template)
    assert entry.path_template == path_template


@pytest.mark.parametrize(
    "path_template",
    [
        "subnet.{netuid}.registration-cost",
        "validator.{hotkey}.commission",
        "validator.{hotkey}.identity",
        "account.{coldkey}.balance",
        "subnet.{netuid}.pool.price",
        "subnet.{netuid}.burn-rate",
        "network.subnet-registration-cost",
        "network.runtime-version",
        "network.subnet-count",
    ],
)
def test_storage_backed_paths_advertise_subscription(
    path_template: str,
) -> None:
    assert lookup(path_template).subscription_supported is True


@pytest.mark.parametrize("path_template", _SUPPORTED_PATHS)
def test_all_applicable_primitives_are_valid(path_template: str) -> None:
    entry = lookup(path_template)
    for prim in entry.applicable_primitives:
        assert prim in VALID_PRIMITIVES


@pytest.mark.parametrize("path_template", _SUPPORTED_PATHS)
def test_all_observable_types_are_valid(path_template: str) -> None:
    entry = lookup(path_template)
    assert entry.type in VALID_OBSERVABLE_TYPES


@pytest.mark.parametrize("path_template", _SUPPORTED_PATHS)
def test_computed_entries_declare_computed_args(path_template: str) -> None:
    entry = lookup(path_template)
    if entry.computed:
        assert entry.computed_args, f"{path_template} is computed but has no computed_args"


@pytest.mark.parametrize("path_template", _SUPPORTED_PATHS)
def test_path_template_parses(path_template: str) -> None:
    """Verify path_params are extracted without errors (done in __post_init__)."""
    entry = lookup(path_template)
    assert isinstance(entry.path_params, tuple)


# Verify the two computed entries explicitly.
@pytest.mark.parametrize(
    ("path_template", "expected_args"),
    [
        ("subnet.{netuid}.pool.depth-for-trade", ("size", "max-bps")),
        ("neuron.{netuid}.{hotkey}.blocks-until-immunity-expires", ("netuid", "hotkey")),
    ],
)
def test_computed_entry_args(path_template: str, expected_args: tuple[str, ...]) -> None:
    entry = lookup(path_template)
    assert entry.computed is True
    assert entry.computed_args == expected_args


# ---------------------------------------------------------------------------
# Appendix B — friendly event mapping
# ---------------------------------------------------------------------------

_APPENDIX_B_FRIENDLY_NAMES = list(FRIENDLY_EVENT_MAP)


def test_friendly_event_map_has_eleven_entries() -> None:
    assert len(FRIENDLY_EVENT_MAP) == 11


def test_unobservable_friendly_events_are_not_registered() -> None:
    assert "neuron-deregistered" not in FRIENDLY_EVENT_MAP
    assert "hyperparam-changed" not in FRIENDLY_EVENT_MAP
    with pytest.raises(KeyError):
        lookup("event.neuron-deregistered")
    with pytest.raises(KeyError):
        lookup("event.hyperparam-changed")


@pytest.mark.parametrize("friendly_name", _APPENDIX_B_FRIENDLY_NAMES)
def test_friendly_event_in_map(friendly_name: str) -> None:
    substrate_events = FRIENDLY_EVENT_MAP[friendly_name]
    assert isinstance(substrate_events, list)
    assert len(substrate_events) >= 1
    for event in substrate_events:
        assert "." in event, f"Expected Module.Event format, got {event!r}"


@pytest.mark.parametrize("friendly_name", _APPENDIX_B_FRIENDLY_NAMES)
def test_lookup_friendly_event(friendly_name: str) -> None:
    events = lookup_friendly_event(friendly_name)
    assert events == FRIENDLY_EVENT_MAP[friendly_name]


def test_lookup_friendly_event_unknown_raises() -> None:
    with pytest.raises(KeyError, match="unknown friendly event name"):
        lookup_friendly_event("does-not-exist")


@pytest.mark.parametrize("friendly_name", _APPENDIX_B_FRIENDLY_NAMES)
def test_friendly_event_has_registry_entry(friendly_name: str) -> None:
    entry = lookup(f"event.{friendly_name}")
    assert entry.type == "event"
    assert "event" in entry.applicable_primitives


def test_swap_maps_to_current_stake_swapped_event() -> None:
    events = lookup_friendly_event("swap")
    assert events == ["SubtensorModule.StakeSwapped"]


# ---------------------------------------------------------------------------
# Render-path coverage for multi-param templates
# ---------------------------------------------------------------------------


def test_render_neuron_path() -> None:
    entry = lookup("neuron.{netuid}.{hotkey}.incentive")
    rendered = entry.render_path({"netuid": "19", "hotkey": "5Fxxx"})
    assert rendered == "neuron.19.5Fxxx.incentive"


def test_render_account_path() -> None:
    entry = lookup("account.{coldkey}.balance")
    rendered = entry.render_path({"coldkey": "5Fxxx"})
    assert rendered == "account.5Fxxx.balance"


def test_render_network_no_params() -> None:
    entry = lookup("network.subnet-registration-cost")
    assert entry.path_params == ()
    assert entry.render_path({}) == "network.subnet-registration-cost"


# ---------------------------------------------------------------------------
# Global invariants over all entries
# ---------------------------------------------------------------------------


def test_all_entries_have_non_empty_description() -> None:
    for entry in all_entries():
        assert entry.description.strip(), f"{entry.path_template} has empty description"


def test_all_entries_have_valid_resource() -> None:
    valid_resources = {"subnet", "validator", "neuron", "account", "network", "event", "tx"}
    for entry in all_entries():
        assert entry.resource in valid_resources, (
            f"{entry.path_template} has unexpected resource {entry.resource!r}"
        )


def test_total_entry_count() -> None:
    # Pruning cleanup removed 2 observables; event cleanup removed 2 promises
    # that have no corresponding current-Subtensor event.
    assert len(all_entries()) == 44


# Computed and chained-read entries that must declare read_cost > 1 so the
# spec §9.5 RU/day banner reflects declared steady-state observation work.
# Transport bootstrap, retries, and hidden SDK calls are outside this estimate.
_ENTRIES_WITH_READ_COST = {
    "tx.{tx_hash}": 6,
    "subnet.{netuid}.pool.price": 3,
    "subnet.{netuid}.pool.tao-depth": 3,
    "subnet.{netuid}.pool.alpha-depth": 3,
    "subnet.{netuid}.pool.depth-for-trade": 3,
    "subnet.{netuid}.pool.alpha-supply": 3,
    "subnet.{netuid}.pool.moving-price": 3,
    "subnet.{netuid}.pool.volume": 3,
    "subnet.{netuid}.registration-cost": 3,
    "subnet.{netuid}.emission-share": 5,
    "subnet.{netuid}.burn-rate": 3,
    "subnet.{netuid}.ema-tao-flow": 3,
    "subnet.{netuid}.hyperparams": 3,
    "subnet.{netuid}.identity": 3,
    "validator.{netuid}.{hotkey}.dividends-alpha": 4,
    "validator.{netuid}.{hotkey}.stake-alpha": 4,
    "validator.{hotkey}.commission": 2,
    "validator.{hotkey}.weights": 8,
    "validator.{hotkey}.child-keys": 3,
    "validator.{hotkey}.identity": 2,
    "neuron.{netuid}.{hotkey}.incentive": 5,
    "neuron.{netuid}.{hotkey}.dividends": 4,
    "neuron.{netuid}.{hotkey}.stake-alpha": 4,
    "neuron.{netuid}.{hotkey}.last-update": 8,
    "neuron.{netuid}.{hotkey}.blocks-until-immunity-expires": 5,
    "account.{coldkey}.balance": 2,
    "account.{coldkey}.activity": 4,
    "network.subnet-registration-cost": 2,
    "network.tao-price": 2,
    "network.runtime-version": 2,
    "network.subnet-count": 2,
}


@pytest.mark.parametrize(("path_template", "expected_cost"), list(_ENTRIES_WITH_READ_COST.items()))
def test_registered_entry_read_cost(path_template: str, expected_cost: int) -> None:
    entry = lookup(path_template)
    assert entry.read_cost == expected_cost


def test_every_polled_bittensor_entry_declares_timestamp_inclusive_cost() -> None:
    """Every non-event, non-transaction observation reads Timestamp.Now."""
    for entry in all_entries():
        if entry.type != "event":
            assert entry.path_template in _ENTRIES_WITH_READ_COST, entry.path_template


def test_registration_cost_is_sampled_per_block() -> None:
    assert lookup("subnet.{netuid}.registration-cost").natural_cadence is Cadence.PER_BLOCK


def test_legacy_epoch_immunity_observable_is_not_registered() -> None:
    with pytest.raises(KeyError):
        lookup("neuron.{netuid}.{hotkey}.epochs-until-immunity-expires")
