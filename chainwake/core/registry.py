"""Resource and observable registry.

Entries are keyed by ``(chain, path_template)`` so chains may expose the same
friendly path without sharing provider-specific observation metadata.
Bittensor entries are explicit; EVM entries are generated from chain profiles
against the same primitive/runtime contracts.

Appendix B friendly-event mapping lives in `FRIENDLY_EVENT_MAP` — a module-level
constant dict from friendly name to one or more Substrate `Module.Event` strings.
Storage bindings and friendly event mappings are chain-scoped to prevent an
Ethereum entry from inheriting Substrate metadata merely because paths match.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from chainwake.chains import ChainAlias
from chainwake.providers.base import Cadence

PrimitiveName = Literal["threshold", "delta", "event", "liveness", "state", "tx"]
ObservableType = Literal["numeric", "event", "state-bytes", "bool", "tx-status"]

VALID_PRIMITIVES: frozenset[str] = frozenset(
    {"threshold", "delta", "event", "liveness", "state", "tx"}
)
VALID_OBSERVABLE_TYPES: frozenset[str] = frozenset(
    {"numeric", "event", "state-bytes", "bool", "tx-status"}
)
_EXTERNAL_PRICE_POLL_SECONDS = 60.0
_EXTERNAL_PRICE_READ_COST = 2


class ObservationDriver(StrEnum):
    """Runtime mechanism that decides when an observable is evaluated."""

    STORAGE_CHANGE = "storage_change"
    BEST_HEAD = "best_head"
    SUBNET_EPOCH = "subnet_epoch"
    EVENT_STREAM = "event_stream"
    TX_STATUS = "tx_status"
    TIMER_POLL = "timer_poll"


@dataclass(frozen=True, slots=True)
class StorageBinding:
    """Exact storage key backing a change-driven observable."""

    module: str
    storage_function: str
    path_params: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ObservationPolicy:
    """Declarative mapping from primitive/window semantics to a runtime driver."""

    natural_cadence: Cadence
    primitive_drivers: tuple[tuple[PrimitiveName, ObservationDriver], ...]
    fallback_driver: ObservationDriver | None
    default_poll_seconds: float | None = None
    storage_binding: StorageBinding | None = None
    storage_bindings: tuple[StorageBinding, ...] = ()

    def driver_for(
        self,
        primitive: str,
        *,
        window_unit: str | None = None,
    ) -> ObservationDriver:
        """Return the configured driver for one applicable primitive."""
        if primitive == "delta" and window_unit == "ever" and self.storage_bindings:
            return ObservationDriver.STORAGE_CHANGE
        for configured_primitive, driver in self.primitive_drivers:
            if configured_primitive == primitive:
                return driver
        raise ValueError(f"observation policy has no driver for primitive {primitive!r}")


_PATH_SEGMENT_RE = re.compile(r"^[a-z0-9_-]+$")
_TEMPLATE_PARAM_RE = re.compile(r"^\{[a-z_][a-z0-9_]*\}$")

# Appendix B: friendly event name → Substrate event(s).
#
# A list value means the friendly name covers multiple Substrate events
# (e.g. `swap` covers both swap directions). The provider resolves the list
# to an OR-filter when subscribing. This mapping is a maintenance commitment:
# runtime upgrades that rename events require updating here; `--type-raw`
# remains the escape hatch for anything not in this table.
#
FRIENDLY_EVENT_MAP: dict[str, list[str]] = {
    "transfer": ["Balances.Transfer"],
    "stake-added": ["SubtensorModule.StakeAdded"],
    "stake-removed": ["SubtensorModule.StakeRemoved"],
    "swap": ["SubtensorModule.StakeSwapped"],
    "neuron-registered": ["SubtensorModule.NeuronRegistered"],
    "subnet-registered": ["SubtensorModule.NetworkAdded"],
    "weights-set": ["SubtensorModule.WeightsSet"],
    "axon-served": ["SubtensorModule.AxonServed"],
    "validator-permit-changed": ["SubtensorModule.MaxAllowedValidatorsSet"],
    "child-keys-set": ["SubtensorModule.SetChildren"],
    "identity-set": ["SubtensorModule.ChainIdentitySet"],
}
_FRIENDLY_EVENT_MAPS: dict[ChainAlias, dict[str, list[str]]] = {
    "bt": FRIENDLY_EVENT_MAP,
    "eth": {},
    "base": {},
    "bsc": {},
}


_BITTENSOR_STORAGE_BINDINGS: dict[str, tuple[StorageBinding, ...]] = {
    "validator.{hotkey}.commission": (StorageBinding("SubtensorModule", "Delegates", ("hotkey",)),),
    "validator.{hotkey}.identity": (
        StorageBinding("SubtensorModule", "IdentitiesV2", ("hotkey",)),
    ),
    "account.{coldkey}.balance": (StorageBinding("System", "Account", ("coldkey",)),),
    "subnet.{netuid}.registration-cost": (StorageBinding("SubtensorModule", "Burn", ("netuid",)),),
    "subnet.{netuid}.pool.price": (
        StorageBinding("SubtensorModule", "SubnetTAO", ("netuid",)),
        StorageBinding("SubtensorModule", "SubnetAlphaIn", ("netuid",)),
    ),
    "subnet.{netuid}.burn-rate": (StorageBinding("SubtensorModule", "MinerBurned", ("netuid",)),),
    "network.subnet-registration-cost": (StorageBinding("SubtensorModule", "NetworkLastLockCost"),),
    "network.runtime-version": (StorageBinding("System", "LastRuntimeUpgrade"),),
    "network.subnet-count": (StorageBinding("SubtensorModule", "TotalNetworks"),),
}


def _cadence_driver(cadence: Cadence) -> ObservationDriver:
    if cadence == Cadence.PER_BLOCK:
        return ObservationDriver.BEST_HEAD
    if cadence == Cadence.PER_EPOCH:
        return ObservationDriver.SUBNET_EPOCH
    if cadence == Cadence.PER_EVENT:
        return ObservationDriver.EVENT_STREAM
    return ObservationDriver.TIMER_POLL


def _observation_policy(
    *,
    chain: ChainAlias,
    path_template: str,
    observable_type: ObservableType,
    cadence: Cadence,
    subscription_supported: bool,
    primitives: tuple[PrimitiveName, ...],
    default_poll_seconds: float | None,
) -> ObservationPolicy:
    """Build one explicit runtime policy from registry-owned metadata."""
    if observable_type == "tx-status":
        default_driver = ObservationDriver.TX_STATUS
    else:
        default_driver = _cadence_driver(cadence)

    storage_bindings = (
        _BITTENSOR_STORAGE_BINDINGS.get(path_template, ())
        if chain == "bt" and subscription_supported
        else ()
    )
    primitive_drivers = tuple(
        (
            primitive,
            ObservationDriver.STORAGE_CHANGE
            if storage_bindings and primitive in {"threshold", "state"}
            else default_driver,
        )
        for primitive in primitives
    )
    uses_storage = bool(storage_bindings) and any(
        primitive in {"threshold", "state", "delta"} for primitive in primitives
    )
    fallback = (
        default_driver
        if uses_storage
        else ObservationDriver.TIMER_POLL
        if default_driver in {ObservationDriver.BEST_HEAD, ObservationDriver.SUBNET_EPOCH}
        else None
    )
    return ObservationPolicy(
        natural_cadence=cadence,
        primitive_drivers=primitive_drivers,
        fallback_driver=fallback,
        default_poll_seconds=default_poll_seconds,
        storage_binding=storage_bindings[0] if len(storage_bindings) == 1 else None,
        storage_bindings=storage_bindings,
    )


def _validate_path_template(template: str) -> tuple[str, ...]:
    """Validate a dotted-path template and return the parameter names in order.

    Templates are dot-separated. Each segment is either a literal (lowercase
    alphanumerics, underscore, hyphen) or a `{param_name}` placeholder.
    """

    if not template:
        raise ValueError("path_template must be non-empty")
    params: list[str] = []
    for segment in template.split("."):
        if _TEMPLATE_PARAM_RE.match(segment):
            params.append(segment[1:-1])
            continue
        if not _PATH_SEGMENT_RE.match(segment):
            raise ValueError(
                f"invalid path_template segment {segment!r} in {template!r} "
                "(expect lowercase alphanumerics, underscore, hyphen, or {param})"
            )
    return tuple(params)


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    """Per-spec §6.5 metadata for a single observable.

    Attributes:
        path_template: canonical dotted-path template, e.g.
            `subnet.{netuid}.pool.price`. `{name}` placeholders bind to the
            CLI's resource ID and computed_args at lookup time.
        resource: the parent resource name (subnet, validator, neuron, ...).
        type: numeric / event / state-bytes / bool / tx-status.
        natural_cadence: per_block / per_epoch / per_event / other.
        subscription_supported: whether the provider can subscribe via WS.
        applicable_primitives: which primitives can watch this observable.
        description: human-readable description (for docs and MCP schema).
        computed: True for computed observables (combine multiple raw reads).
        computed_args: additional CLI-required args for computed observables
            (e.g. `--size`, `--max-bps` for `subnet.depth-for-trade`).
        read_cost: approximate number of provider storage reads per
            ``read_observable`` call. Defaults to 1; entries whose handler
            issues multiple reads (computed observables, or simple ones that
            chain a uid lookup with a list query) should set it explicitly so
            the spec §9.5 RU/day banner reflects real cost. Cost is in RPC
            reads, not compute — math on a single fetched value stays at 1.
    """

    path_template: str
    resource: str
    type: ObservableType
    natural_cadence: Cadence
    subscription_supported: bool
    applicable_primitives: tuple[PrimitiveName, ...]
    description: str
    chain: ChainAlias = "bt"
    computed: bool = False
    computed_args: tuple[str, ...] = ()
    read_cost: int = 1
    default_poll_seconds: float | None = None
    path_params: tuple[str, ...] = field(init=False)
    observation_policy: ObservationPolicy = field(init=False)

    def __post_init__(self) -> None:
        if self.type not in VALID_OBSERVABLE_TYPES:
            raise ValueError(f"invalid observable type {self.type!r}")
        if not self.applicable_primitives:
            raise ValueError("applicable_primitives must be non-empty")
        for prim in self.applicable_primitives:
            if prim not in VALID_PRIMITIVES:
                raise ValueError(f"unknown primitive {prim!r} in applicable_primitives")
        params = _validate_path_template(self.path_template)
        if self.default_poll_seconds is not None and (
            self.default_poll_seconds <= 0 or not math.isfinite(self.default_poll_seconds)
        ):
            raise ValueError("default_poll_seconds must be finite and greater than zero")
        object.__setattr__(self, "path_params", params)
        object.__setattr__(
            self,
            "observation_policy",
            _observation_policy(
                chain=self.chain,
                path_template=self.path_template,
                observable_type=self.type,
                cadence=self.natural_cadence,
                subscription_supported=self.subscription_supported,
                primitives=self.applicable_primitives,
                default_poll_seconds=self.default_poll_seconds,
            ),
        )
        if self.computed and not self.computed_args:
            raise ValueError(
                f"computed observable {self.path_template!r} must declare computed_args"
            )
        if self.read_cost < 1:
            raise ValueError(
                f"read_cost must be >= 1 for {self.path_template!r}, got {self.read_cost}"
            )

    def render_path(self, params: dict[str, str]) -> str:
        """Substitute `{name}` placeholders with caller-provided values."""

        missing = [p for p in self.path_params if p not in params]
        if missing:
            raise KeyError(f"missing path params {missing} for {self.path_template!r}")
        rendered = self.path_template
        for name, value in params.items():
            if name not in self.path_params:
                raise KeyError(f"unknown path param {name!r} for {self.path_template!r}")
            rendered = rendered.replace(f"{{{name}}}", value)
        return rendered

    def bind_rendered_path(self, path: str) -> dict[str, str]:
        """Extract this entry's named parameters from a concrete path."""
        template_parts = self.path_template.split(".")
        rendered_parts = path.split(".")
        if len(template_parts) != len(rendered_parts):
            raise ValueError(f"path {path!r} does not match {self.path_template!r}")
        bound: dict[str, str] = {}
        for template_part, rendered_part in zip(template_parts, rendered_parts, strict=True):
            if _TEMPLATE_PARAM_RE.fullmatch(template_part):
                bound[template_part[1:-1]] = rendered_part
            elif template_part != rendered_part:
                raise ValueError(f"path {path!r} does not match {self.path_template!r}")
        return bound


class ObservableRegistry:
    """Chain-scoped observable catalogue."""

    def __init__(self) -> None:
        self._entries: dict[tuple[ChainAlias, str], RegistryEntry] = {}

    def register(self, entry: RegistryEntry) -> None:
        key = (entry.chain, entry.path_template)
        if key in self._entries:
            raise ValueError(
                f"duplicate registry entry {entry.path_template!r} for chain {entry.chain!r}"
            )
        self._entries[key] = entry

    def lookup(self, chain: ChainAlias, path_template: str) -> RegistryEntry:
        try:
            return self._entries[(chain, path_template)]
        except KeyError as exc:
            raise KeyError(f"no registry entry for {path_template!r} on chain {chain!r}") from exc

    def lookup_rendered(self, chain: ChainAlias, path: str) -> RegistryEntry:
        for (entry_chain, _), entry in self._entries.items():
            if entry_chain != chain:
                continue
            pattern_parts = [
                r"[^.]+" if _TEMPLATE_PARAM_RE.fullmatch(part) else re.escape(part)
                for part in entry.path_template.split(".")
            ]
            if re.fullmatch(r"\.".join(pattern_parts), path):
                return entry
        if path.startswith("event."):
            return self.lookup(chain, "event.--type-raw")
        raise KeyError(f"no registry entry for rendered path {path!r} on chain {chain!r}")

    def all_entries(self, chain: ChainAlias | None = None) -> tuple[RegistryEntry, ...]:
        if chain is None:
            return tuple(self._entries.values())
        return tuple(
            entry for (entry_chain, _), entry in self._entries.items() if entry_chain == chain
        )

    def clear(self) -> None:
        self._entries.clear()


_REGISTRY = ObservableRegistry()


def register(entry: RegistryEntry) -> None:
    """Register an entry in its chain catalogue."""

    _REGISTRY.register(entry)


def lookup(path_template: str, *, chain: ChainAlias = "bt") -> RegistryEntry:
    """Look up an entry by chain and canonical path template."""

    return _REGISTRY.lookup(chain, path_template)


def lookup_rendered(path: str, *, chain: ChainAlias = "bt") -> RegistryEntry:
    """Resolve a concrete observable path within one chain catalogue."""

    return _REGISTRY.lookup_rendered(chain, path)


def lookup_friendly_event(
    friendly_name: str,
    *,
    chain: ChainAlias = "bt",
) -> list[str]:
    """Return the provider event(s) for a chain-scoped friendly event name.

    Raises KeyError if the name is not in the curated mapping.
    """

    event_map = _FRIENDLY_EVENT_MAPS[chain]
    if friendly_name not in event_map:
        raise KeyError(
            f"unknown friendly event name {friendly_name!r} for chain {chain!r}; "
            "use the chain's raw event filter outside its curated mapping"
        )
    return event_map[friendly_name]


def all_entries(*, chain: ChainAlias = "bt") -> tuple[RegistryEntry, ...]:
    return _REGISTRY.all_entries(chain)


def reset_for_testing() -> None:
    """Clear the registry — only for unit tests."""

    _REGISTRY.clear()
    _seed_core_entries()
    _seed_bittensor_entries()
    _seed_evm_entries()


def _seed_core_entries() -> None:
    """Register the shared Bittensor numeric and transaction observables."""

    register(
        RegistryEntry(
            path_template="subnet.{netuid}.pool.price",
            resource="subnet",
            type="numeric",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=True,
            applicable_primitives=("threshold", "delta"),
            description="Alpha price in TAO, computed from dTAO pool reserves.",
            # Timestamp.Now + NetworksAdded + dynamic-info runtime call.
            read_cost=3,
        )
    )
    register(
        RegistryEntry(
            path_template="tx.{tx_hash}",
            resource="tx",
            type="tx-status",
            natural_cadence=Cadence.OTHER,
            subscription_supported=True,
            applicable_primitives=("tx",),
            description="Transaction finality wait.",
            # Steady-state pending observation when one block advances: head
            # hash/number, prior-cursor canonicality, new block
            # hash/extrinsics, and Timestamp.Now. The one-time bounded
            # historical scan is bootstrap work outside the estimate guard.
            read_cost=6,
        )
    )


def _seed_subnet_entries() -> None:
    """Register the remaining Bittensor subnet observables."""

    register(
        RegistryEntry(
            path_template="subnet.{netuid}.pool.tao-depth",
            resource="subnet",
            type="numeric",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=False,
            applicable_primitives=("threshold", "delta"),
            description="TAO reserve depth of the dTAO pool for this subnet.",
            # Timestamp.Now + NetworksAdded + dynamic-info runtime call.
            read_cost=3,
        )
    )
    register(
        RegistryEntry(
            path_template="subnet.{netuid}.pool.alpha-depth",
            resource="subnet",
            type="numeric",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=False,
            applicable_primitives=("threshold", "delta"),
            description="Alpha reserve depth of the dTAO pool for this subnet.",
            read_cost=3,
        )
    )
    register(
        RegistryEntry(
            path_template="subnet.{netuid}.pool.depth-for-trade",
            resource="subnet",
            type="numeric",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=False,
            applicable_primitives=("threshold",),
            description=(
                "Margin (in bps) for a trade of --size TAO within --max-bps slippage. "
                "Positive when feasible; use --above 0 to fire when the trade becomes viable."
            ),
            computed=True,
            computed_args=("size", "max-bps"),
            read_cost=3,
        )
    )
    register(
        RegistryEntry(
            path_template="subnet.{netuid}.pool.alpha-supply",
            resource="subnet",
            type="numeric",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=False,
            applicable_primitives=("threshold", "delta"),
            description=(
                "Total alpha token supply circulating outside the dTAO pool for this subnet."
            ),
            read_cost=3,
        )
    )
    register(
        RegistryEntry(
            path_template="subnet.{netuid}.pool.moving-price",
            resource="subnet",
            type="numeric",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=False,
            applicable_primitives=("threshold", "delta"),
            description=(
                "Exponential moving average of the dTAO pool price for this subnet "
                "(U64F64 fixed-point converted to float)."
            ),
            read_cost=3,
        )
    )
    register(
        RegistryEntry(
            path_template="subnet.{netuid}.pool.volume",
            resource="subnet",
            type="numeric",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=False,
            applicable_primitives=("threshold", "delta"),
            description="Cumulative swap volume (TAO) recorded for this subnet's dTAO pool.",
            read_cost=3,
        )
    )
    register(
        RegistryEntry(
            path_template="subnet.{netuid}.registration-cost",
            resource="subnet",
            type="numeric",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=True,
            applicable_primitives=("threshold",),
            description="Current neuron registration cost in TAO for this subnet.",
            # Timestamp.Now + NetworksAdded + Burn.
            read_cost=3,
        )
    )
    register(
        RegistryEntry(
            path_template="subnet.{netuid}.emission-share",
            resource="subnet",
            type="numeric",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=False,
            applicable_primitives=("threshold", "delta"),
            description=(
                "Fraction of the current block's total TAO emission routed to this subnet "
                "for pool injection and chain buybacks."
            ),
            # Timestamp.Now + NetworksAdded + SubnetTaoInEmission + SubnetExcessTao +
            # SubnetInfoRuntimeApi.get_block_emission.
            read_cost=5,
        )
    )
    register(
        RegistryEntry(
            path_template="subnet.{netuid}.burn-rate",
            resource="subnet",
            type="numeric",
            natural_cadence=Cadence.PER_EPOCH,
            subscription_supported=True,
            applicable_primitives=("threshold", "delta"),
            description=(
                "Fraction of the last tempo's miner emission withheld because it was "
                "routed to subnet-owner hotkeys (burned or recycled)."
            ),
            # Timestamp.Now + NetworksAdded + MinerBurned.
            read_cost=3,
        )
    )
    register(
        RegistryEntry(
            path_template="subnet.{netuid}.ema-tao-flow",
            resource="subnet",
            type="numeric",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=False,
            applicable_primitives=("threshold", "delta"),
            description=(
                "EMA of TAO inflow/outflow for this subnet, in TAO. "
                "Positive = net TAO entering the subnet; negative = net TAO leaving."
            ),
            # Timestamp.Now + NetworksAdded + SubnetEmaTaoFlow.
            read_cost=3,
        )
    )
    register(
        RegistryEntry(
            path_template="subnet.{netuid}.hyperparams",
            resource="subnet",
            type="state-bytes",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=False,
            applicable_primitives=("state",),
            description=(
                "Snapshot of all key hyperparameters for this subnet. "
                "Includes ActivityCutoffFactorMilli and the effective activity_cutoff "
                "in blocks computed from the same pinned tempo. Match.observed includes "
                "changed_keys listing which params changed."
            ),
            # Timestamp.Now + NetworksAdded + one batched query_multi call.
            read_cost=3,
        )
    )
    register(
        RegistryEntry(
            path_template="subnet.{netuid}.identity",
            resource="subnet",
            type="state-bytes",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=False,
            applicable_primitives=("state",),
            description=(
                "Full SubnetIdentitiesV3 record plus the subnet owner hotkey and coldkey, "
                "read from the current dynamic-info runtime API. Structured value: the "
                "state primitive supports on-change only."
            ),
            read_cost=3,
        )
    )


def _seed_validator_entries() -> None:
    """Appendix A — validator resource."""

    register(
        RegistryEntry(
            path_template="validator.{netuid}.{hotkey}.dividends-alpha",
            resource="validator",
            type="numeric",
            natural_cadence=Cadence.PER_EPOCH,
            subscription_supported=False,
            applicable_primitives=("threshold", "delta"),
            description=(
                "Dividends earned by this validator hotkey in the last epoch, "
                "denominated in the specified subnet's alpha token."
            ),
            # Timestamp.Now + NetworksAdded + Uids + dividends.
            read_cost=4,
        )
    )
    register(
        RegistryEntry(
            path_template="validator.{netuid}.{hotkey}.stake-alpha",
            resource="validator",
            type="numeric",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=False,
            applicable_primitives=("threshold", "delta"),
            description=(
                "Total stake held by this validator hotkey on the specified subnet, "
                "denominated in that subnet's alpha token."
            ),
            # Timestamp.Now + NetworksAdded + Uids + stake.
            read_cost=4,
        )
    )
    register(
        RegistryEntry(
            path_template="validator.{hotkey}.commission",
            resource="validator",
            type="numeric",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=True,
            applicable_primitives=("state",),
            description=(
                "Commission fraction set by this validator hotkey. State transition "
                "targets are finite numeric fractions from 0 to 1."
            ),
            read_cost=2,
        )
    )
    register(
        RegistryEntry(
            path_template="validator.{hotkey}.weights",
            resource="validator",
            type="numeric",
            natural_cadence=Cadence.PER_EPOCH,
            subscription_supported=True,
            applicable_primitives=("liveness",),
            description=(
                "Weight-set liveness anchor for this validator on --netuid (default 1) "
                "and --mechid (default 0); "
                "silent-for fires if no weight commit is seen within the window."
            ),
            # Timestamp.Now + NetworksAdded + optional mechanism lookup + Uids + LastUpdate,
            # then historical block hash/timestamp/epoch for truthful
            # first-read liveness.
            read_cost=8,
        )
    )
    register(
        RegistryEntry(
            path_template="validator.{hotkey}.child-keys",
            resource="validator",
            type="state-bytes",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=False,
            applicable_primitives=("state",),
            description="Child-key delegation list for this validator hotkey.",
            # Timestamp.Now + TotalNetworks + one query_multi over ChildKeys.
            read_cost=3,
        )
    )
    register(
        RegistryEntry(
            path_template="validator.{hotkey}.identity",
            resource="validator",
            type="state-bytes",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=True,
            applicable_primitives=("state",),
            description=(
                "On-chain identity record for this validator hotkey. Structured value: "
                "the state primitive supports on-change only."
            ),
            read_cost=2,
        )
    )


def _seed_neuron_entries() -> None:
    """Appendix A — neuron resource."""

    register(
        RegistryEntry(
            path_template="neuron.{netuid}.{hotkey}.incentive",
            resource="neuron",
            type="numeric",
            natural_cadence=Cadence.PER_EPOCH,
            subscription_supported=False,
            applicable_primitives=("threshold", "delta"),
            description=(
                "Incentive score for this neuron in the last epoch on --mechid "
                "(default 0, the main mechanism)."
            ),
            # Timestamp.Now + NetworksAdded + optional mechanism existence + Uids + Incentive.
            read_cost=5,
        )
    )
    register(
        RegistryEntry(
            path_template="neuron.{netuid}.{hotkey}.dividends",
            resource="neuron",
            type="numeric",
            natural_cadence=Cadence.PER_EPOCH,
            subscription_supported=False,
            applicable_primitives=("threshold", "delta"),
            description="Dividends paid to this neuron hotkey in the last epoch.",
            # Timestamp.Now + NetworksAdded + Uids + Dividends.
            read_cost=4,
        )
    )
    register(
        RegistryEntry(
            path_template="neuron.{netuid}.{hotkey}.stake-alpha",
            resource="neuron",
            type="numeric",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=False,
            applicable_primitives=("threshold", "delta"),
            description=(
                "Total stake held by this neuron hotkey on the given subnet, "
                "denominated in that subnet's alpha token."
            ),
            # Timestamp.Now + NetworksAdded + Uids + stake.
            read_cost=4,
        )
    )
    register(
        RegistryEntry(
            path_template="neuron.{netuid}.{hotkey}.last-update",
            resource="neuron",
            type="numeric",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=False,
            applicable_primitives=("liveness",),
            description=(
                "Block number of last weight-set by this neuron on --mechid "
                "(default 0, the main mechanism); "
                "liveness anchor — silent-for triggers when the neuron goes stale."
            ),
            # Timestamp.Now + NetworksAdded + optional mechanism lookup + Uids + LastUpdate,
            # then historical block hash/timestamp/epoch.
            read_cost=8,
        )
    )
    register(
        RegistryEntry(
            path_template="neuron.{netuid}.{hotkey}.blocks-until-immunity-expires",
            resource="neuron",
            type="numeric",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=False,
            applicable_primitives=("threshold",),
            description="Countdown in blocks until this neuron's immunity period ends.",
            computed=True,
            computed_args=("netuid", "hotkey"),
            # Timestamp.Now + NetworksAdded + Uids + ImmunityPeriod + BlockAtRegistration.
            read_cost=5,
        )
    )


def _seed_account_entries() -> None:
    """Appendix A — account resource."""

    register(
        RegistryEntry(
            path_template="account.{coldkey}.balance",
            resource="account",
            type="numeric",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=True,
            applicable_primitives=("threshold", "delta", "state"),
            description="Free TAO balance for this coldkey account.",
            read_cost=2,
        )
    )
    register(
        RegistryEntry(
            path_template="account.{coldkey}.activity",
            resource="account",
            type="numeric",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=True,
            applicable_primitives=("liveness",),
            description=(
                "Activity liveness anchor for this coldkey; "
                "silent-for triggers when no on-chain activity is observed."
            ),
            # Timestamp.Now + LastTxBlock + historical block hash/timestamp.
            read_cost=4,
        )
    )


def _seed_network_entries() -> None:
    """Appendix A — network resource."""

    register(
        RegistryEntry(
            path_template="network.tao-price",
            resource="network",
            type="numeric",
            natural_cadence=Cadence.OTHER,
            subscription_supported=False,
            applicable_primitives=("threshold", "delta"),
            description="TAO aggregate USD price from CoinGecko.",
            read_cost=_EXTERNAL_PRICE_READ_COST,
            default_poll_seconds=_EXTERNAL_PRICE_POLL_SECONDS,
        )
    )
    register(
        RegistryEntry(
            path_template="network.subnet-registration-cost",
            resource="network",
            type="numeric",
            natural_cadence=Cadence.PER_EPOCH,
            subscription_supported=True,
            applicable_primitives=("threshold",),
            description="Network-wide cost in TAO to register a new subnet.",
            read_cost=2,
        )
    )
    register(
        RegistryEntry(
            path_template="network.runtime-version",
            resource="network",
            type="state-bytes",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=True,
            applicable_primitives=("state",),
            description="Current Substrate runtime version spec number.",
            read_cost=2,
        )
    )
    register(
        RegistryEntry(
            path_template="network.subnet-count",
            resource="network",
            type="numeric",
            natural_cadence=Cadence.PER_BLOCK,
            subscription_supported=True,
            applicable_primitives=("threshold", "delta"),
            description="Total number of registered subnets on the network.",
            read_cost=2,
        )
    )
    register(
        RegistryEntry(
            path_template="network.--on-runtime-upgraded",
            resource="network",
            type="event",
            natural_cadence=Cadence.PER_EVENT,
            subscription_supported=True,
            applicable_primitives=("event",),
            description="Fires on System.CodeUpdated when the runtime is upgraded.",
        )
    )


def _seed_event_entries() -> None:
    """Appendix A — event resource (chain-wide firehose) plus Appendix B friendly types.

    One registry entry per Appendix B friendly event. The `FRIENDLY_EVENT_MAP`
    constant holds the Substrate event string(s) each friendly name resolves to.
    The generic firehose entry covers `--type-raw` usage.
    """

    register(
        RegistryEntry(
            path_template="event.--type-raw",
            resource="event",
            type="event",
            natural_cadence=Cadence.PER_EVENT,
            subscription_supported=True,
            applicable_primitives=("event",),
            description=(
                "Chain-wide event firehose with --type-raw <Module.Event> filter. "
                "Use for Substrate events outside the 11 curated friendly names."
            ),
        )
    )
    for friendly_name, substrate_events in FRIENDLY_EVENT_MAP.items():
        register(
            RegistryEntry(
                path_template=f"event.{friendly_name}",
                resource="event",
                type="event",
                natural_cadence=Cadence.PER_EVENT,
                subscription_supported=True,
                applicable_primitives=("event",),
                description=(
                    f"Friendly event filter for '{friendly_name}'; resolves to {substrate_events}."
                ),
            )
        )


def _seed_bittensor_entries() -> None:
    """Register the full Bittensor observable catalogue."""

    _seed_subnet_entries()
    _seed_validator_entries()
    _seed_neuron_entries()
    _seed_account_entries()
    _seed_network_entries()
    _seed_event_entries()


def _seed_evm_entries() -> None:
    """Register fee and transaction observables from EVM chain profiles."""
    from chainwake.providers.evm import (  # noqa: PLC0415
        EVM_PROFILES,
        EvmFeeModel,
        EvmSubscription,
    )

    for profile in EVM_PROFILES.values():
        subscription_supported = EvmSubscription.NEW_HEADS in profile.subscription_capabilities
        register(
            RegistryEntry(
                path_template="token.{token}.price",
                resource="token",
                type="numeric",
                natural_cadence=Cadence.OTHER,
                subscription_supported=False,
                applicable_primitives=("threshold", "delta"),
                description=(
                    f"{profile.name} token aggregate USD price from CoinGecko, "
                    "resolved by chain-scoped symbol or contract address."
                ),
                read_cost=_EXTERNAL_PRICE_READ_COST,
                default_poll_seconds=_EXTERNAL_PRICE_POLL_SECONDS,
                chain=profile.alias,
            )
        )
        if profile.fee_model in {EvmFeeModel.EIP1559, EvmFeeModel.OP_STACK}:
            register(
                RegistryEntry(
                    chain=profile.alias,
                    path_template="network.base-fee",
                    resource="network",
                    type="numeric",
                    natural_cadence=Cadence.PER_BLOCK,
                    subscription_supported=subscription_supported,
                    applicable_primitives=("threshold", "delta"),
                    description=(
                        f"{profile.name} EIP-1559 execution base fee, denominated in gwei."
                    ),
                    read_cost=1,
                )
            )
        if profile.fee_model == EvmFeeModel.OP_STACK:
            for path_template, description in (
                (
                    "network.l1-base-fee",
                    f"Ethereum L1 base fee observed by {profile.name}, in gwei.",
                ),
                (
                    "network.l1-blob-base-fee",
                    f"Ethereum L1 blob base fee observed by {profile.name}, in gwei.",
                ),
            ):
                register(
                    RegistryEntry(
                        chain=profile.alias,
                        path_template=path_template,
                        resource="network",
                        type="numeric",
                        natural_cadence=Cadence.PER_BLOCK,
                        subscription_supported=subscription_supported,
                        applicable_primitives=("threshold", "delta"),
                        description=description,
                        read_cost=2,
                    )
                )
        if profile.fee_model == EvmFeeModel.GAS_PRICE:
            register(
                RegistryEntry(
                    chain=profile.alias,
                    path_template="network.gas-price",
                    resource="network",
                    type="numeric",
                    natural_cadence=Cadence.PER_BLOCK,
                    subscription_supported=subscription_supported,
                    applicable_primitives=("threshold", "delta"),
                    description=f"{profile.name} suggested gas price, denominated in gwei.",
                    read_cost=2,
                )
            )
        register(
            RegistryEntry(
                path_template="tx.{tx_hash}",
                resource="tx",
                type="tx-status",
                natural_cadence=Cadence.OTHER,
                subscription_supported=subscription_supported,
                applicable_primitives=("tx",),
                description=(f"{profile.name} transaction receipt, confirmations, and finality."),
                # Receipt, canonical inclusion block, canonical head, and
                # (when requested) the safe/finalized head.
                read_cost=4,
                chain=profile.alias,
            )
        )


_seed_core_entries()
_seed_bittensor_entries()
_seed_evm_entries()


__all__ = [
    "FRIENDLY_EVENT_MAP",
    "VALID_OBSERVABLE_TYPES",
    "VALID_PRIMITIVES",
    "ObservableRegistry",
    "ObservableType",
    "PrimitiveName",
    "RegistryEntry",
    "all_entries",
    "lookup",
    "lookup_friendly_event",
    "register",
    "reset_for_testing",
]
