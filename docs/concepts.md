# Concepts

---

## The six primitive shapes

Every chainwake watcher uses one of six structural types. The type is implicit
in the flags you provide — you never type the word "threshold" or "delta". The
primitives determine what state the watcher holds, when it fires, and what
appears in the output payload.

### Threshold

Fires when a numeric observable crosses an absolute value. Stateless — evaluated
independently on each poll.

```sh
chainwake bt subnet 19 price --below 0.05
chainwake bt subnet 19 price --above 0.10
```

Fires once, on the first sample that satisfies the condition.

### Delta

Fires when a numeric observable moves by N% (up, down, or either direction)
from a baseline. By default the first successful observation since watcher
start is the baseline:

```sh
chainwake bt subnet 28 burnrate --move-pct 1
chainwake bt subnet 19 price --drop-pct 5 --window-time 1h
chainwake bt subnet 19 price --rise-pct 3 --window-blocks 300
chainwake bt subnet 19 price --move-pct 10 --window-epochs 1
```

Omit all window flags for the unbounded, since watcher start baseline. The
baseline is captured only after a successful provider observation, so retries
and rate limits cannot consume or replace it. To use a rolling window, specify
exactly one of `--window-time`, `--window-blocks`, or `--window-epochs`.
`--drop-pct` fires on downward moves; `--rise-pct` on upward; `--move-pct` on
either direction.

### Event

Subscribes to named chain events and fires on the first match. No condition
flags needed — the event itself is the condition.

```sh
chainwake bt event --type subnet-registered --max-runtime 24h
chainwake bt event --type transfer --max-runtime 1h
chainwake bt event --type-raw "Balances.Transfer"
```

Event watchers subscribe via WebSocket where the provider supports it.
`--type` accepts the 11 verified friendly names listed below. `--type-raw`
accepts any `Module.Event` string from the Substrate runtime.

### Liveness

Fires when a liveness anchor has not been updated for longer than a specified
duration. Used to detect validators that have stopped setting weights, or
neurons that have gone offline.

```sh
chainwake bt validator 5Fxxx... weights --silent-for 3epochs
chainwake bt neuron 19 5Fxxx... last-update --silent-for 10blocks
chainwake bt account 5Fxxx... activity --silent-for 2h
```

Duration syntax: `30s`, `5m`, `2h`, `1d`, `100blocks`, `3epochs`.

`LastUpdate` and `LastTxBlock` are absolute activity-block markers. Chainwake
resolves their historical chain timestamp and, for subnet paths, historical
epoch index, so a validator or account that was already stale can match on the
first read. This requires historical state for time/epoch durations. If the RPC
has pruned that block, use an archive-capable endpoint or a block duration.
Block-duration matches remain exact without historical state; their
`last_seen_timestamp` is `null` rather than an invented watcher-start time.

### State

Fires when a value at a storage path changes. Used for hyperparameter changes,
commission updates, identity updates, and similar.

```sh
chainwake bt validator 5Fxxx... commission --on-change
chainwake bt validator 5Fxxx... commission --changes-to 0.10
chainwake bt validator 5Fxxx... commission --changes-from 0.05
```

`--on-change` fires on any transition. `--changes-to` fires when the value
reaches a specific target. `--changes-from` fires when the value departs from
a specific source. Commission targets are finite numeric fractions from `0`
to `1`. Structured observables such as subnet and validator identity support
only `--on-change`; scalar string targets cannot meaningfully match their
record values.

### Tx

Waits for a specific transaction hash to reach a finality level. Used after
submitting a transaction to know when it is safe to act on the result.

```sh
chainwake bt tx 0xabababababababababababababababababababababababababababababababab --finality included
chainwake bt tx 0xabababababababababababababababababababababababababababababababab --finality finalized
```

`included` fires when the transaction appears in a block. `finalized` fires
when the block containing it is finalised by the network. Lookup is stateful:
one bounded historical scan is followed by scans of new blocks only, and
included block metadata is cached during the finality wait.

---

## Observables and the registry

An observable is a named, typed, watchable value on the chain. The registry
is the single source of truth for what is available.

Each observable declares:

- **Path template** — dotted path with placeholders, e.g. `subnet.{netuid}.pool.price`
- **Type** — `numeric`, `event`, `state-bytes`, `bool`, or `tx-status`
- **Natural cadence** — `per_block`, `per_epoch`, `per_event`, or `other`
- **Applicable primitives** — which of the six shapes can watch it

### EVM observables

The `EvmProvider` is driven by immutable chain profiles. A profile owns the
chain ID, default RPC, block cadence, supported finality levels, fee model, and
subscription capabilities. Adding an EVM chain therefore does not require
copying provider or runtime logic.

| Chain | Fee observables | Token price | Transaction confidence |
|---|---|---|---|
| Ethereum | `network.base-fee` | `token.{token}.price` | included/confirmations, safe, finalized |
| Base | `network.base-fee`, `network.l1-base-fee`, `network.l1-blob-base-fee` | `token.{token}.price` | included/confirmations, safe, finalized |
| BSC | `network.gas-price` | `token.{token}.price` | included/confirmations, finalized |

All fee values are gwei and use threshold/delta primitives. `newHeads`
subscriptions drive fee and transaction evaluations when the selected endpoint
supports them; the profile's block cadence controls timer fallback. Token
prices are external CoinGecko aggregate USD observations with a registry-owned
60-second poll cadence. Every connection verifies the RPC's `eth_chainId`
before a watcher starts.

### Bittensor observables at launch

**subnet**
| Sub-resource | Type | Cadence | Primitives |
|---|---|---|---|
| `price` | numeric (TAO) | per_block | threshold, delta |
| `pool.tao-depth` | numeric (TAO) | per_block | threshold, delta |
| `pool.alpha-depth` | numeric (alpha) | per_block | threshold, delta |
| `pool.alpha-supply` | numeric (alpha) | per_block | threshold, delta |
| `pool.moving-price` | numeric (TAO per alpha) | per_block | threshold, delta |
| `pool.volume` | numeric (TAO, cumulative) | per_block | threshold, delta |
| `pool.depth-for-trade` | numeric (margin in bps, computed) | per_block | threshold |
| `registration-cost` | numeric (TAO) | per_block | threshold |
| `emission-share` | numeric (fraction) | per_block | threshold, delta |
| `burn-rate` | numeric (fraction) | per_epoch | threshold, delta |
| `ema-tao-flow` | numeric (TAO, signed) | per_block | threshold, delta |
| `hyperparams` | state snapshot (includes effective activity cutoff) | per_block | state |
| `identity` | full SubnetIdentitiesV3 + owners | per_block | state |

**validator**
| Sub-resource | Type | Cadence | Primitives |
|---|---|---|---|
| `dividends-alpha` (`--netuid`) | numeric (subnet alpha) | per_epoch | threshold, delta |
| `stake-alpha` (`--netuid`) | numeric (subnet alpha) | per_block | threshold, delta |
| `commission` | numeric fraction | per_block | state |
| `weights` (`--netuid`, `--mechid`) | liveness anchor | per_epoch | liveness |
| `child-keys` | state-bytes | per_block | state |
| `identity` | state-bytes | per_block | state |

**neuron** (identified by netuid + hotkey)
| Sub-resource | Type | Cadence | Primitives |
|---|---|---|---|
| `incentive` (`--mechid`) | numeric | per_epoch | threshold, delta |
| `dividends` | numeric | per_epoch | threshold, delta |
| `stake-alpha` | numeric (subnet alpha) | per_block | threshold, delta |
| `last-update` (`--mechid`) | liveness anchor | per_block | liveness |
| `blocks-until-immunity-expires` | numeric (blocks, computed) | per_block | threshold |

Subtensor defines neuron immunity as a strict block interval from
`BlockAtRegistration`; tempo changes and owner-triggered epochs do not change
its expiry. The former epoch countdown was removed because converting that
interval to epochs could misstate the remaining protection.

Current Subtensor does not expose a pruning score or deterministic deregistration
block. Neuron replacement is decided when a full subnet receives a registration,
using relative emission, registration age, immunity, and owner protections.

Subtensor spec 440 can expose more than one incentive mechanism per subnet.
Chainwake defaults to the main mechanism (`--mechid 0`) for compatibility and
uses the runtime's mechanism storage index for non-zero ids. A requested
mechanism that is not present fails explicitly instead of returning a fabricated
zero.

**account** (identified by coldkey SS58)
| Sub-resource | Type | Cadence | Primitives |
|---|---|---|---|
| `balance` | numeric (TAO) | per_block | threshold, delta, state |
| `activity` | liveness anchor | per_block | liveness |

**network** (no ID)
| Sub-resource | Type | Cadence | Primitives |
|---|---|---|---|
| `tao-price` | numeric (USD) | other (60-second timer) | threshold, delta |
| `subnet-registration-cost` | numeric (TAO) | per_epoch | threshold |
| `runtime-version` | state | per_block | state |
| `subnet-count` | numeric | per_block | threshold, delta |
| `on-runtime-upgraded` | event | per_event | event |

**event** — filtered chain-wide event stream. Exactly one of
`--type <friendly-name>` or `--type-raw <Module.Event>` is required.

**tx** — transaction hash, `--finality included|finalized`.

### Curated event names (`--type`)

| Friendly name | Substrate event |
|---|---|
| `transfer` | `Balances.Transfer` |
| `stake-added` | `SubtensorModule.StakeAdded` |
| `stake-removed` | `SubtensorModule.StakeRemoved` |
| `swap` | `SubtensorModule.StakeSwapped` |
| `neuron-registered` | `SubtensorModule.NeuronRegistered` |
| `subnet-registered` | `SubtensorModule.NetworkAdded` |
| `weights-set` | `SubtensorModule.WeightsSet` |
| `axon-served` | `SubtensorModule.AxonServed` |
| `validator-permit-changed` | `SubtensorModule.MaxAllowedValidatorsSet` |
| `child-keys-set` | `SubtensorModule.SetChildren` |
| `identity-set` | `SubtensorModule.ChainIdentitySet` |

Current Subtensor has no standalone neuron-deregistered event or generic
hyperparameter-changed event. Use `--type-raw` for a specific event exposed by
the connected runtime.

---

## Output payload structure

Every watcher invocation run with `--json` emits exactly one JSON object on
stdout when it exits, regardless of whether the condition matched, timed out,
or errored. An interactive TTY otherwise uses human-readable output. The
`status` field discriminates the JSON result. Non-watcher helpers and
help/version output use their own documented formats.

### Matched payload

```json
{
  "status": "matched",
  "watcher": {
    "chain": "bt",
    "resource": "subnet",
    "resource_id": "19",
    "sub_resource": "pool.price",
    "name": null,
    "primitive": "delta",
    "invocation": ["chainwake", "bt", "subnet", "19", "price", "--drop-pct", "5", "--window-time", "1h"]
  },
  "condition": {
    "operator": "drop-pct",
    "target": 5.0,
    "window": { "unit": "time", "value": "1h" }
  },
  "observed": {
    "path": "subnet.19.pool.price",
    "value": 0.0432,
    "previous_value": 0.0455,
    "delta": -0.0023,
    "delta_pct": -5.05,
    "block": 8119123,
    "block_hash": "0xabc...",
    "timestamp": "2026-05-05T19:42:10Z"
  },
  "budget": {
    "runtime_ms": 384201,
    "rpc_calls": 42,
    "estimated_ru_consumed": 42
  },
  "process": {
    "pid": 12453,
    "started_at": "2026-05-05T19:35:46Z"
  }
}
```

Fields:

- `status` — `matched`, `stopped`, `timeout`, `budget_exhausted`, `user_error`,
  `provider_error`, `auth_error`, or `internal_error`.
- `watcher` — describes the watcher: chain, resource, IDs, primitive, and the
  full original `invocation` argv for re-invocation or logging.
- `condition` — the condition that was being watched. Shape varies by primitive.
- `observed` — what the chain reported at the moment of match. `null` on
  non-match exits. Shape varies by primitive type.
- `budget` — actual runtime/top-level call counts plus registry-estimated
  observation work. `--max-ru` is a registry-estimated observation budget,
  not a provider billing cap; connection bootstrap, retries, and RPCs hidden
  inside the SDK are excluded.
- `process` — PID and start time.

### Non-match exits

Stopped, timeout, and budget-exhausted payloads follow the same envelope with
`"observed": null` and a `"reason"` field:

```json
{
  "status": "timeout",
  "reason": "max_runtime_reached",
  "watcher": { ... },
  "condition": { ... },
  "observed": null,
  "budget": { ... },
  "process": { ... }
}
```

Error payloads (`user_error`, `provider_error`, `auth_error`,
`internal_error`) include a human-readable `"message"` field and may have
`null` `watcher` and `condition` if the error occurred before the watcher could
be constructed.

---

## Exit codes

| Code | `status` field | Meaning |
|------|---------------|---------|
| `0` | `matched` | Condition fired. Parse stdout for the match payload. |
| `1` | `stopped` / `timeout` / `budget_exhausted` | No match; stopped or reached a runtime/estimated-observation limit. |
| `2` | `user_error` | Bad flags, unknown resource, invalid combination. |
| `3` | `provider_error` / `auth_error` | RPC unavailable, or credentials/access required. |
| `4` | `internal_error` | Bug in chainwake. Please file an issue. |

Exit codes are for shell-level control flow. Agents parse the JSON payload for
detail. Stderr carries human-readable diagnostics and is not part of the stable
contract.

---

## Polling vs subscription

Exact and composite storage-backed threshold and state watchers use WebSocket
storage subscriptions. Chainwake reads one baseline, sleeps until a dependency
changes, then
re-reads the normal observable at the notified block. This keeps decoding,
entity checks, chain timestamps, conditions, and output identical to the
polling path while removing idle reads.

The subscription path currently covers subnet registration cost, validator
commission and identity, account balance, network subnet-registration cost,
runtime version, subnet count, burn rate, and pool price. Price subscribes to
both `SubnetTAO` and `SubnetAlphaIn`; burn rate subscribes directly to
`MinerBurned`. A baseline delta with no explicit window also uses these
change-driven subscriptions. A rolling time, block, or epoch delta retains its
natural sampling schedule because an unchanged value at a window boundary is
meaningful.

Remaining `per_block` observables subscribe to new best-block headers. Chainwake
prefers `chainHead_v1_follow`: it reads the initialized finalized height once,
derives later heights from the ordered parent graph, and passes the direct
`bestBlockChanged` hash into the normal observable reader. It unpins older
initialized blocks immediately, batches finalized and pruned block unpins, and
calls `chainHead_v1_unfollow` when the watcher closes. Nodes without that API
fall back to `chain_subscribeNewHeads`, which requires one block-hash lookup per
notification.

Remaining `per_epoch` observables use the same head stream to inspect their
chain-owned epoch marker and read the observable only when that marker
advances. Liveness and computed observables without declared storage
dependencies retain their required block-sampling semantics without a timer.
Transaction finality and other unusual cadences remain polled. The RU banner
reports the selected change-driven or direct-hash estimate.

Event watchers reuse the same direct best-head `BlockRef`, fetch events and the
chain timestamp at that hash, then apply the required friendly-name or raw
`Module.Event` filter. They no longer resolve the block hash or number again.
A failed event subscription is reported as `provider_error`; it does not
silently switch that watcher to a different transport. Storage subscriptions
reconnect after transient interruption and fall back to polling only when the
provider reports that the path is unsupported.

The registry owns an observation policy for every wake. That policy maps each
applicable primitive to one transport driver:

- exact or composite storage-backed threshold/state wakes and baseline deltas
  use storage-change subscriptions
- `per_block` wakes evaluate once per notified best block
- `per_epoch` wakes are evaluated when the governing subnet's on-chain
  epoch marker advances. At each notified block, Chainwake inspects
  `SubnetEpochIndex` and
  `LastEpochBlock`; it does not derive boundaries from global block-number
  arithmetic. Paths with no single governing subnet fall back to per-block
  evaluation.
- `per_event` wakes subscribe and wait for matching filtered events
- transaction finality uses its dedicated status driver

The policy may vary by primitive and window for one observable. For example,
an unwindowed price delta follows its two reserve keys, while a rolling price
delta samples notified best blocks so its time window remains meaningful.
Callers do not choose a polling interval. `--max-runtime` and `--max-ru`
remain available as independent bounds on every wake.

---

## Output contract

[`schemas/output.json`](../schemas/output.json) is the sole current output
contract. Consumers must validate payloads against it and reject unknown
fields. Output shape changes update the Pydantic models and this schema
atomically.

---

## Further reading

- [cli-reference.md](cli-reference.md) — all commands and flags
- [adapters.md](adapters.md) — output adapters and notification setup
- [agent-integration.md](agent-integration.md) — run, wait, wake, and act
