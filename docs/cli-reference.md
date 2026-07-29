# CLI Reference

Command shapes:

```
chainwake bt <resource> [<id>] <sub-resource> [flags]
chainwake eth <resource> <sub-resource> [flags]
chainwake base <resource> <sub-resource> [flags]
chainwake bsc <resource> <sub-resource> [flags]
```

`bt` / `bittensor`, `eth` / `ethereum`, `base`, and `bsc` are chain selectors.

---

## Common flags

These flags are accepted by every leaf command:

| Flag | Default | Description |
|---|---|---|
| `--rpc-url <url>` | chain profile | Override RPC endpoint. Also read from `CHAINWAKE_<CHAIN>_RPC_URL`. |
| `--api-key <key>` | (none) | Optional key for an authenticated endpoint. Also read from `CHAINWAKE_<CHAIN>_API_KEY`, then `CHAINWAKE_API_KEY`. |
| `--out <uri>` | (none) | Output adapter URI. Repeatable. Default: JSON to stdout + exit. |
| `--name <string>` | (none) | Human-readable watcher label, stored in output payload. |
| `--max-runtime <duration>` | (unbounded) | Hard upper bound on runtime. Set explicitly for agent calls. |
| `--max-ru <integer>` | (none) | Registry-estimated observation budget; not a provider billing cap. |
| `--durable` | off | Persist the watcher, start it in the background, and return a job id. |
| `--context <text>` | (none) | Context returned by `jobs wait`; requires `--durable`. |

Multi-read observables and pinned epoch-state checks consume their declared
registry cost, not merely one unit for the top-level provider call. Chainwake
checks that declared cost before issuing an observation, so the estimate guard
is not overshot. An exactly funded observation is still evaluated; the next is
blocked. This estimate excludes connection bootstrap, transient retries, and
RPCs hidden inside the SDK.

Duration syntax: `30s`, `5m`, `2h`, `1d`, `100blocks`, `3epochs`.

`--out` is described in detail in [adapters.md](adapters.md).

---

## Durable jobs

Durable mode uses the same watcher grammar and validation as a direct wait:

```sh
chainwake --json --durable \
  --context "Report the observed subnet price and block." \
  bt subnet 19 price --below 0.05
```

The command returns immediately with a persistent job record. Manage it with:

```sh
chainwake --json jobs list
chainwake --json jobs show <job-id>
chainwake --json jobs wait <job-id>
chainwake --json jobs cancel <job-id>
chainwake --json daemon status
```

`jobs wait` waits on local SQLite state; it does not poll the chain. Its
completion event contains `context` alongside the unchanged watcher output
result.

Do not combine `--durable` with `--out`. Attach the host to `jobs wait`
instead. Durable jobs reject literal `--api-key` and `--rpc-url` values because
arguments are persisted. Configure the chain-specific
`CHAINWAKE_<CHAIN>_API_KEY` and `CHAINWAKE_<CHAIN>_RPC_URL` values in the
supervisor environment instead.

---

## Ethereum network

Ethereum watcher flags use `CHAINWAKE_ETH_RPC_URL` and
`CHAINWAKE_ETH_API_KEY`. The anonymous default endpoint is
`wss://rpc-eth.blockmachine.io`.

### `eth network base-fee` — EIP-1559 base fee

Observable path: `network.base-fee`

Value: gwei

Primitives: threshold, delta

Cadence: per_block, driven by `newHeads`

```sh
chainwake eth network base-fee --below 10
chainwake eth network base-fee --above 100 --max-runtime 1h
chainwake eth network base-fee --move-pct 20
chainwake eth network base-fee --drop-pct 10 --window-time 30m
chainwake eth network base-fee --rise-pct 25 --window-blocks 100
```

Threshold and delta flags have the same meaning as Bittensor numeric
observables. Ethereum supports time and block windows; it does not advertise
epoch windows. Without a window, the first successful observation remains the
baseline. Chainwake subscribes to `newHeads`, keeps that WebSocket alive, and
reads each notified block by hash. A provider without subscription support
falls back to the chain-owned 12-second cadence.

### `eth tx <hash>` — transaction receipt and confidence

Observable path: `tx.{tx_hash}`

Primitive: tx

Cadence: on startup and each subscribed `newHeads` notification

```sh
chainwake eth tx 0x0123...abcd
chainwake eth tx 0x0123...abcd --confirmations 3
chainwake eth tx 0x0123...abcd --finality finalized
```

The default is inclusion with one canonical confirmation. Use
`--confirmations N` for confirmation depth, `--finality safe` for Ethereum's
safe head, or `--finality finalized` for its finalized head; confirmation
depth cannot be combined with safe/finalized. A match
reports `success` or `reverted`, confirmation count, gas used, and effective
gas price in wei using the output schema. Receipt `null` means only
pending/unknown—Chainwake does not claim that a transaction was dropped or
replaced.

---

## EVM token prices

Ethereum, Base, and BSC all expose:

```text
chainwake <eth|base|bsc> token <symbol-or-address> price CONDITION [OPTIONS]
```

Observable path: `token.{token}.price`

Value: aggregate USD price

Primitives: threshold, delta

Cadence: timer polling every 60 seconds

```sh
chainwake eth token DAI price --below 0.995
chainwake base token DAI price --move-pct 1 --window-time 1h
chainwake bsc token GRAM price --above 0.01
```

Use one of `--below`, `--above`, `--drop-pct`, `--rise-pct`, or `--move-pct`.
Delta conditions may use `--window-time`; without a window, the first
successful observation is the baseline. Block windows are intentionally not
available because this price source is timer-sampled rather than block-native.

Symbols are resolved against CoinGecko's token list for the selected chain, so
the same symbol can resolve to a different contract on each chain. Ambiguous
symbols fail closed and list their matching contracts; pass a 20-byte contract
address to select one explicitly. The output identifies the resolved contract,
token metadata, source, USD quote currency, and source timestamp.

CoinGecko anonymous access is the default. If it returns a rate limit, set a
free Demo key in `CHAINWAKE_COINGECKO_API_KEY`. This key is separate from
`CHAINWAKE_<CHAIN>_API_KEY`, which authenticates the chain RPC.

---

## Base network

Base uses `CHAINWAKE_BASE_RPC_URL` / `CHAINWAKE_BASE_API_KEY`; its anonymous
default is `wss://rpc-base.blockmachine.io`. Blocks arrive about every 2 seconds.

```sh
chainwake base network base-fee --above 0.01
chainwake base network l1-base-fee --below 20
chainwake base network l1-blob-base-fee --below 2
chainwake base tx 0x0123...abcd --finality safe
chainwake base tx 0x0123...abcd --finality finalized
```

`base-fee` is the EIP-1559 L2 execution fee. `l1-base-fee` and
`l1-blob-base-fee` read `GasPriceOracle.l1BaseFee()` and
`GasPriceOracle.blobBaseFee()` at
`0x420000000000000000000000000000000000000F`; together they track the
Ethereum fee inputs to Base's L1 data-cost calculation. All values are gwei
and support the same threshold/delta flags as Ethereum.

Transaction waits support `included`, `safe`, and `finalized`. `safe` means
the L2 block is derivable from canonical L1 data; `finalized` means that L1
data is finalized. `--confirmations N` is available only with `included`.

## BSC network

BSC uses `CHAINWAKE_BSC_RPC_URL` / `CHAINWAKE_BSC_API_KEY`; its anonymous
default is `wss://rpc-bsc.blockmachine.io`. Its current target block cadence is
about 0.45 seconds.

```sh
chainwake bsc network gas-price --above 0.1
chainwake bsc tx 0x0123...abcd --confirmations 12
chainwake bsc tx 0x0123...abcd --finality finalized
```

`gas-price` watches the suggested `eth_gasPrice` in gwei. BSC block headers
currently report a zero `baseFeePerGas`, so Chainwake does not expose the
misleading EIP-1559 base-fee command for this chain.

BSC supports `included` plus confirmation depth, or `finalized` for its
fast-finality head. `safe` is intentionally rejected because it is not in the
BSC profile.

---

## subnet

Watch a Bittensor subnet. Subnet is identified by its netuid (integer).

### `bt subnet <netuid> price` — alpha price

Observable path: `subnet.{netuid}.pool.price`
Primitives: threshold, delta
Cadence: per_block

```sh
chainwake bt subnet 19 price --below 0.05
chainwake bt subnet 19 price --above 0.10
chainwake bt subnet 19 price --drop-pct 5 --window-time 1h
chainwake bt subnet 19 price --rise-pct 3 --window-blocks 300
chainwake bt subnet 19 price --move-pct 10 --window-epochs 1
chainwake bt subnet 28 burnrate --move-pct 1
```

**Threshold flags** (mutually exclusive):
- `--below <value>` — fire when price < value (TAO)
- `--above <value>` — fire when price > value (TAO)

**Delta flags** (pick exactly one):
- `--drop-pct <n>` — fire when price drops by n% within the window
- `--rise-pct <n>` — fire when price rises by n% within the window
- `--move-pct <n>` — fire when price moves by n% in either direction within the window

**Window flags** (optional with delta; mutually exclusive):
- `--window-time <duration>` — e.g. `1h`, `30m`, `5d`
- `--window-blocks <n>` — e.g. `300`
- `--window-epochs <n>` — e.g. `1`

Omit all window flags to measure the change since watcher start. The first
successful observation becomes the baseline and remains so until the watcher
exits. Provider retries and rate-limit responses do not replace that baseline.
An explicit window keeps the rolling-window behavior.

Observation timing is selected automatically from the wake's registry policy.
Use `--max-runtime` to bound elapsed time and `--max-ru` to bound the
registry-estimated observation cost.

Example — wait until SN19 price drops below 0.05, timeout after 1 hour:

```sh
chainwake bt subnet 19 price --below 0.05 --max-runtime 1h
```

The following pool and subnet metrics use the same threshold/delta flags and
optional watcher-start or rolling-window baseline as `price`.

### `bt subnet <netuid> tao-depth` — TAO pool reserve

Observable path: `subnet.{netuid}.pool.tao-depth`
Primitives: threshold, delta
Cadence: per_block

```sh
chainwake bt subnet 64 tao-depth --below 5000
```

The value is the TAO reserve in the subnet's dTAO pool.

### `bt subnet <netuid> alpha-depth` — alpha pool reserve

Observable path: `subnet.{netuid}.pool.alpha-depth`
Primitives: threshold, delta
Cadence: per_block

```sh
chainwake bt subnet 19 alpha-depth --drop-pct 10 --window-time 1h
```

The value is the alpha-token reserve in that subnet's dTAO pool.

### `bt subnet <netuid> alpha-supply` — alpha supply outside the pool

Observable path: `subnet.{netuid}.pool.alpha-supply`
Primitives: threshold, delta
Cadence: per_block

```sh
chainwake bt subnet 19 alpha-supply --rise-pct 5 --window-epochs 1
```

### `bt subnet <netuid> moving-price` — moving alpha price

Observable path: `subnet.{netuid}.pool.moving-price`
Primitives: threshold, delta
Cadence: per_block

```sh
chainwake bt subnet 19 moving-price --above 0.1
```

The value is the subnet's on-chain EMA price in TAO per alpha.

### `bt subnet <netuid> volume` — cumulative swap volume

Observable path: `subnet.{netuid}.pool.volume`
Primitives: threshold, delta
Cadence: per_block

```sh
chainwake bt subnet 19 volume --rise-pct 20 --window-blocks 300
```

The value is the cumulative dTAO swap volume in TAO.

### `bt subnet <netuid> emission-share` — TAO emission share

Observable path: `subnet.{netuid}.emission-share`
Primitives: threshold, delta
Cadence: per_block

```sh
chainwake bt subnet 19 emission-share --drop-pct 10 --window-blocks 300
```

The value is the fraction of the current block's total TAO emission routed to
the subnet.

### `bt subnet <netuid> burn-rate` — miner-emission burn fraction

Observable path: `subnet.{netuid}.burn-rate`
Primitives: threshold, delta
Cadence: per_epoch

```sh
chainwake bt subnet 28 burn-rate --move-pct 1
```

The value is the last-tempo fraction of miner emission withheld for subnet
owner hotkeys. Thresholds and deltas without an explicit window subscribe
directly to `MinerBurned`; explicit rolling windows retain epoch scheduling.
`burnrate` is a command alias.

### `bt subnet <netuid> ema-tao-flow` — signed EMA TAO flow

Observable path: `subnet.{netuid}.ema-tao-flow`
Primitives: threshold, delta
Cadence: per_block

```sh
chainwake bt subnet 19 ema-tao-flow --below 0
```

Positive values indicate inflow and negative values indicate outflow.

### `bt subnet <netuid> depth-for-trade` — slippage feasibility

Observable path: `subnet.{netuid}.pool.depth-for-trade`
Primitive: threshold
Cadence: per_block

```sh
chainwake bt subnet 64 depth-for-trade --size 100 --max-bps 50 --above 0
```

`--size <tao>` and `--max-bps <basis-points>` are required. The observed value
is the remaining slippage margin in basis points; a positive value means the
trade fits the requested budget.

### `bt subnet <netuid> registration-cost` — registration cost

Observable path: `subnet.{netuid}.registration-cost`
Primitives: threshold
Cadence: per_block

```sh
chainwake bt subnet 64 registration-cost --below 0.5
chainwake bt subnet 64 registration-cost --above 5.0
```

Flags: `--below`, `--above` (same as price; no delta flags).

The subnet burn can decay or adjust on every block, so this watcher samples
per block rather than waiting for the subnet's next epoch.

### `bt subnet <netuid> hyperparams` — supported hyperparameter changes

Observable path: `subnet.{netuid}.hyperparams`
Primitive: state
Cadence: per_block

```sh
chainwake bt subnet 19 hyperparams --on-change
```

The snapshot includes the current spec-440 activity-cutoff fields:

- `activity_cutoff_factor_milli` — raw tempo-relative factor stored on chain
- `activity_cutoff` — effective block count, computed as
  `max(1, activity_cutoff_factor_milli * tempo / 1000)`

It also includes:

- `adjustment-interval` — block interval between difficulty/burn adjustments
- `immunity-period` — blocks of registration immunity new neurons receive
- `kappa` — Yuma consensus kappa parameter
- `max-allowed-uids` — maximum total UIDs allowed on this subnet
- `max-allowed-validators` — maximum validators with permits on this subnet
- `max-weights-limit` — per-weight cap when neurons set weights
- `min-allowed-weights` — minimum non-zero weights a neuron must set
- `rho` — Yuma consensus rho parameter
- `tempo` — subnet tempo (blocks per epoch)
- `weights-set-rate-limit` — minimum blocks between weight commits per neuron
- `weights-version-key` — subnet weights version key

All fields come from one batch pinned to the same block. Chainwake does not read
the deprecated absolute `ActivityCutoff` storage item.

### `bt subnet <netuid> identity` — subnet identity changes

```sh
chainwake bt subnet 19 identity --on-change
```

The observed value contains the full `SubnetIdentitiesV3` record from the
dynamic-info runtime API plus `owner_hotkey` and `owner_coldkey`. This includes
the subnet name, repository, contact, URL, Discord, description, logo URL, and
additional field. Because the value is a structured record, only `--on-change`
is supported.

### `bt event --type subnet-registered` — new subnet event

Observable path: `event.subnet-registered`
Primitive: event
Cadence: per_event

```sh
chainwake bt event --type subnet-registered --max-runtime 24h
chainwake bt event --type subnet-registered --out stream --max-runtime 7d
```

No condition flags — fires on any new subnet registration. Subscribes to
`SubtensorModule.NetworkAdded`. The `observed.args` block in the match payload
contains `netuid`. Fetch `bt subnet <netuid> identity` separately when owner or
identity details are needed.

---

## validator

Watch a Bittensor validator. Identified by hotkey (SS58 address).

### `bt validator <hotkey> weights` — weight-setting liveness

Observable path: `validator.{hotkey}.weights`
Primitive: liveness
Cadence: per_epoch

```sh
chainwake bt validator 5Fxxx... weights --silent-for 1epoch
chainwake bt validator 5Fxxx... weights --netuid 19 --mechid 1 \
  --silent-for 3epochs --max-runtime 4epochs
```

Flags: `--silent-for <duration>` (required), `--netuid <id>` (default `1`),
and `--mechid <0-15>` (default `0`). Fire when no weight commit has been seen
for this validator on that exact subnet mechanism within the duration.
Time and epoch durations use the marker block's historical timestamp/epoch and
can therefore fire on the first read when the validator was already stale.

### `bt validator <hotkey> commission` — commission changes

Observable path: `validator.{hotkey}.commission`
Primitive: state
Cadence: per_block

```sh
chainwake bt validator 5Fxxx... commission --on-change
chainwake bt validator 5Fxxx... commission --changes-to 0.18
```

State flags: `--on-change`, `--changes-to <fraction>`, and
`--changes-from <fraction>`. Target fractions must be finite values from `0`
to `1`; they are compared numerically with the commission returned by the
chain.

### `bt validator <hotkey> dividends-alpha --netuid <n>` — subnet dividends

Observable path: `validator.{netuid}.{hotkey}.dividends-alpha`
Primitives: threshold, delta
Cadence: per_epoch

```sh
chainwake bt validator 5Fxxx... dividends-alpha --netuid 19 --below 100
chainwake bt validator 5Fxxx... dividends-alpha --netuid 19 \
  --drop-pct 20 --window-epochs 3
```

The value is the last-epoch dividend in subnet 19's alpha token. Alpha tokens
from different subnets are different currencies and are never added together.

### `bt validator <hotkey> stake-alpha --netuid <n>` — subnet stake

Observable path: `validator.{netuid}.{hotkey}.stake-alpha`
Primitives: threshold, delta
Cadence: per_block

```sh
chainwake bt validator 5Fxxx... stake-alpha --netuid 19 --below 10000
chainwake bt validator 5Fxxx... stake-alpha --netuid 19 \
  --drop-pct 5 --window-time 1h
```

The value is the hotkey's stake in the selected subnet's alpha token, not TAO.
There is no cross-subnet numeric value because alpha currencies are not
interchangeable.

### `bt validator <hotkey> child-keys` — child key delegation

Observable path: `validator.{hotkey}.child-keys`
Primitive: state
Cadence: per_block

```sh
chainwake bt validator 5Fxxx... child-keys --on-change
```

Only `--on-change` is supported.

### `bt validator <hotkey> identity` — validator identity changes

Observable path: `validator.{hotkey}.identity`
Primitive: state
Cadence: per_block

```sh
chainwake bt validator 5Fxxx... identity --on-change
```

Only `--on-change` is supported because the observed identity is a structured
record, not a scalar string.

---

## neuron

Watch a registered neuron. Identified by both netuid and hotkey.

### `bt neuron <netuid> <hotkey> last-update` — liveness

Observable path: `neuron.{netuid}.{hotkey}.last-update`
Primitive: liveness
Cadence: per_block

```sh
chainwake bt neuron 19 5Fxxx... last-update --silent-for 10blocks
chainwake bt neuron 19 5Fxxx... last-update --mechid 1 --silent-for 2epochs
```

Flags: `--silent-for <duration>` (required) and `--mechid <0-15>` (default
`0`). A non-zero mechanism is checked against the subnet's current
`MechanismCountCurrent`; nonexistent mechanisms fail clearly.
The marker block's historical timestamp and epoch index let already-stale
neurons match immediately.

### `bt neuron <netuid> <hotkey> blocks-until-immunity-expires`

Observable path: `neuron.{netuid}.{hotkey}.blocks-until-immunity-expires`
Primitive: threshold
Cadence: per_block

```sh
chainwake bt neuron 19 5Fxxx... blocks-until-immunity-expires --below 100
```

This is the exact remaining block interval defined by `ImmunityPeriod` and
`BlockAtRegistration`. Dynamic tempo and owner-triggered epochs do not affect
this block-based value.

### `bt neuron <netuid> <hotkey> incentive` — incentive score

Observable path: `neuron.{netuid}.{hotkey}.incentive`
Primitives: threshold, delta
Cadence: per_epoch

```sh
chainwake bt neuron 19 5Fxxx... incentive --below 0.01
chainwake bt neuron 19 5Fxxx... incentive --mechid 1 \
  --drop-pct 30 --window-epochs 2
```

`--mechid <0-15>` selects the mechanism-indexed incentive vector. Mechanism `0`
is the default.

### `bt neuron <netuid> <hotkey> stake-alpha` — subnet stake

Observable path: `neuron.{netuid}.{hotkey}.stake-alpha`
Primitives: threshold, delta
Cadence: per_block

```sh
chainwake bt neuron 19 5Fxxx... stake-alpha --below 1000
chainwake bt neuron 19 5Fxxx... stake-alpha --drop-pct 5 --window-time 1h
```

The value is denominated in the selected subnet's alpha token.

### `bt neuron <netuid> <hotkey> dividends` — neuron dividends

Observable path: `neuron.{netuid}.{hotkey}.dividends`
Primitives: threshold, delta
Cadence: per_epoch

```sh
chainwake bt neuron 19 5Fxxx... dividends --below 100
chainwake bt neuron 19 5Fxxx... dividends \
  --drop-pct 20 --window-epochs 3
```

The value is the neuron's last-epoch dividend on the selected subnet.

---

## account

Watch a coldkey account. Identified by SS58 address.

### `bt account <coldkey> balance` — TAO balance

Observable path: `account.{coldkey}.balance`
Primitives: threshold, delta, state
Cadence: per_block

```sh
chainwake bt account 5Fxxx... balance --below 10.0
chainwake bt account 5Fxxx... balance --drop-pct 20 --window-time 1h
chainwake bt account 5Fxxx... balance --on-change
```

Threshold, delta, and state flags are all accepted.

### `bt account <coldkey> activity` — liveness

Observable path: `account.{coldkey}.activity`
Primitive: liveness
Cadence: per_block

```sh
chainwake bt account 5Fxxx... activity --silent-for 7d
```

Flag: `--silent-for <duration>` (required). Time durations use the
`LastTxBlock` marker's historical timestamp and can match immediately.

---

## network

Watch chain-wide network values. No resource ID.

### `bt network tao-price` — aggregate TAO/USD price

Observable path: `network.tao-price`

Value: aggregate TAO/USD price

Primitives: threshold, delta

Cadence: 60-second timer polling

```sh
chainwake bt network tao-price --below 180
chainwake bt network tao-price --above 250 --max-runtime 24h
chainwake bt network tao-price --move-pct 5 --window-time 1h
```

The quote comes from CoinGecko's `bittensor` coin ID. The output records the
coin ID, symbol, source, quote currency, and source timestamp. Delta conditions
accept `--window-time` or the watcher-start baseline; block and epoch windows
are intentionally unavailable because this price is timer-sampled rather than
chain-native.

### `bt network subnet-registration-cost` — cost to register a subnet

Observable path: `network.subnet-registration-cost`
Primitive: threshold
Cadence: per_epoch

```sh
chainwake bt network subnet-registration-cost --below 500
chainwake bt network subnet-registration-cost --above 1000
```

Threshold flags: `--below`, `--above`.

### `bt network runtime-version` — runtime upgrade detection

Observable path: `network.runtime-version`
Primitive: state
Cadence: per_block

```sh
chainwake bt network runtime-version --on-change
```

Only `--on-change` is supported. Chainwake reads the initial runtime version,
then uses a WebSocket storage subscription and fires when a changed version is
confirmed at the notified block.

### `bt network subnet-count` — registered subnet count

Observable path: `network.subnet-count`
Primitives: threshold, delta
Cadence: per_block

```sh
chainwake bt network subnet-count --above 128
chainwake bt network subnet-count --rise-pct 5 --window-time 7d
```

### `bt network on-runtime-upgraded` — runtime upgrade event

Observable path: `network.--on-runtime-upgraded`
Primitive: event
Cadence: per_event

```sh
chainwake bt network on-runtime-upgraded --max-runtime 30d
```

This subscribes to `System.CodeUpdated`. Use `runtime-version --on-change`
instead when the returned old and new spec-version values are important.

---

## event

Subscribe to chain-wide events. No resource ID.

### `bt event --type <name>` — friendly event name

```sh
chainwake bt event --type transfer --max-runtime 1h
chainwake bt event --type subnet-registered --out stream
chainwake bt event --type stake-added
```

`--type` accepts one of the 11 runtime-verified friendly names. See
[concepts.md](concepts.md#curated-event-names---type) for the full list.

### `bt event --type-raw <Module.Event>` — raw Substrate event

```sh
chainwake bt event --type-raw "Balances.Transfer"
chainwake bt event --type-raw "SubtensorModule.NeuronRegistered"
```

Escape hatch for any Substrate event not in the curated list.

### Event argument filters

Friendly and raw event watches accept the same optional filters.
The `--from`, `--to`, and `--amount-min` flags filter decoded event arguments;
`--direction` interprets transfer direction relative to `--address`.

| Flag | Applies when | Effect |
|---|---|---|
| `--from <address>` | The event has a decoded `from` field | Require an exact SS58 address match |
| `--to <address>` | The event has a decoded `to` field | Require an exact SS58 address match |
| `--amount-min <rao>` | The event has a numeric decoded `amount` or `value` field | Require a value greater than or equal to the non-negative rao amount |
| `--direction in\|out\|both` | The event has decoded transfer-style `from` and `to` fields | Match received or sent events relative to `--address`; `both` does not narrow the event stream |
| `--address <address>` | Used with `--direction` | Set the SS58 address whose direction is evaluated |

`--direction` requires `--address`. `in` compares the event's decoded `to`
field, while `out` compares its decoded `from` field. `both` is a no-op kept
for command symmetry, but still requires `--address`. A filter does not invent
missing event arguments: an event without a field needed by a filter does not
match.

```sh
chainwake bt event --type transfer --from 5Alice... --amount-min 1000000000
chainwake bt event --type transfer --direction in --address 5Bob...
chainwake bt event --type-raw "Balances.Transfer" --to 5Bob...
```

---

## tx

Wait for a transaction to reach a finality level.

### `bt tx <hash>` — transaction finality

```sh
chainwake bt tx 0xabababababababababababababababababababababababababababababababab --finality finalized
chainwake bt tx 0xabababababababababababababababababababababababababababababababab \
  --finality included --max-runtime 2m
```

Flags:
- `--finality included|finalized` (required)

The match payload's `observed` block contains `tx_hash`, `finality`, `block`,
`block_hash`, and `timestamp`.

The first observation performs one bounded historical scan. A miss remains
pending; later observations scan only blocks produced after the previous head.
Once included, Chainwake caches the block metadata and checks only the
finalized head while waiting for `finalized`.

---

## Implementation status

The commands in this reference are provider-backed and emit schema-valid
payloads. Unsupported flag/primitive combinations fail before contacting the
RPC. Absolute-marker liveness is historical-state aware; use a block duration
when the selected endpoint cannot serve the marker's old state.

---

## Further reading

- [concepts.md](concepts.md) — primitive types and observable metadata
- [adapters.md](adapters.md) — the `--out` flag and notification setup
- [agent-integration.md](agent-integration.md) — await a wake from an agent
