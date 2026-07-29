# chainwake — historical initial design proposal

**Status:** archived historical design proposal.
**Audience:** mixed — engineering (primary), strategy, comms, agentic implementers.
**Author:** Mark, with contributions from internal review.
**Date:** 2026-05-05.
**Document version:** 1.0.

> **Archived:** This document records the original product rationale and design
> decisions. It is preserved for provenance, so command examples, output
> samples, implementation status, and version claims below may no longer match
> the shipped release.
>
> Do not use this document as the current command or behavior reference.
> Use [`docs/cli-reference.md`](docs/cli-reference.md) for
> commands, [`docs/concepts.md`](docs/concepts.md) for runtime behavior, and
> [`docs/mcp.md`](docs/mcp.md) for the shipped MCP interface.

---

## Table of contents

1. [Summary](#1-summary)
2. [Problem statement and positioning](#2-problem-statement-and-positioning)
3. [Naming and brand](#3-naming-and-brand)
4. [Users and use cases](#4-users-and-use-cases)
5. [Command surface](#5-command-surface)
6. [Resources, primitives, and observables](#6-resources-primitives-and-observables)
7. [Output contract](#7-output-contract)
8. [Output adapters](#8-output-adapters)
9. [Polling, finality, and natural cadence](#9-polling-finality-and-natural-cadence)
10. [Authentication, providers, and configuration](#10-authentication-providers-and-configuration)
11. [Error handling and exit codes](#11-error-handling-and-exit-codes)
12. [Architecture and implementation](#12-architecture-and-implementation)
13. [Plugin interfaces (anticipated, not implemented)](#13-plugin-interfaces-anticipated-not-implemented)
14. [MCP wrapper](#14-mcp-wrapper)
15. [Out of scope for the initial release](#15-out-of-scope-for-the-initial-release)
16. [Test and launch criteria](#16-test-and-launch-criteria)
17. [Distribution and release](#17-distribution-and-release)
18. [Open questions](#18-open-questions)
19. [Appendix A: full Bittensor resource and observable registry](#appendix-a-full-bittensor-resource-and-observable-registry)
20. [Appendix B: friendly event name mapping](#appendix-b-friendly-event-name-mapping)
21. [Appendix C: example commands by use case](#appendix-c-example-commands-by-use-case)
22. [Appendix D: JSON output schema examples](#appendix-d-json-output-schema-examples)

---

## 1. Summary

`chainwake` is an open-source Python CLI that suspends until a chain-state condition fires, then exits with a structured JSON result. It is deliberately scoped to observation only — it never holds keys, never signs transactions, and never submits writes to chain. Its primary consumer is autonomous agents that need a sleep/wake primitive so they can stop polling in expensive loops; a secondary audience is humans setting up notification-driven workflows.

Bittensor is the launch chain. Blockmachine is the default RPC provider for Bittensor at launch. The architecture explicitly anticipates multi-chain support — Ethereum is the planned next backend — and a clean provider abstraction is part of the initial release even though only the Bittensor implementation ships.

The decision this spec implements: build the initial release over roughly four weeks of focused engineering time, ship publicly as standalone open-source infrastructure with blockmachine as the default but not the only provider, and position the product as the standard agent-shaped chain-observation primitive — not as a blockmachine product.

---

## 2. Problem statement and positioning

### 2.1 The structural gap

Two CLI tools exist in the Bittensor ecosystem that agents already use:

- **btcli** — the official Opentensor CLI. Broad-purpose: wallet operations, staking, registration, subnet info, weight setting. Designed for human operators.
- **agcli** — Const's agent-friendly CLI. Designed explicitly for LLM-driven invocation: structured JSON output, batch flags, spending limits. Covers the *write* side — staking, unstaking, transfers, registration.

Both tools assume the agent knows when to act. Neither tells the agent *when something has happened*. An agent that wants to react to chain state — a price drop, a new subnet registration, a validator going silent — has only two options today: poll in a loop (burns LLM tokens on every "still nothing"), or write a custom polling script in Python or JavaScript (high friction, fragile, reinvented by every agent author).

The same gap exists on Ethereum. Tenderly handles dapp monitoring well but is a hosted SaaS product configured via web UI, not a CLI an agent invokes as a subprocess. Polygon's Agent CLI ships agent-friendly action tooling (wallets, swaps, identity) but no observation tooling. viem and ethers expose `watchEvent` as a library function but not as a standalone CLI.

The category — agent-shaped, suspend-and-exit, structured-output chain observation — is genuinely empty. Whoever ships the credible first implementation of it owns the workflow even after competitors copy the features later. This is the same dynamic that produced curl, jq, ripgrep, and fzf as de facto standards.

### 2.2 Why blockmachine is building this

Four reasons in priority order:

1. **Distribution.** Free-tier signups from a high-intent audience (Bittensor builders, agent authors). Watchers consume RU continuously while running, sitting in `bm_standard` to `bm_pro` plan ranges for non-trivial use cases.

2. **Strategic positioning.** "Managed RPC" is a weak narrative against the Bittensor community's retail-heavy expectations. "The substrate for agentic systems on Bittensor" is stronger. `chainwake` is a concrete artifact that supports it without overclaiming.

3. **Complementarity, not competition.** agcli does write-side actions; chainwake does read-side observation. The two pair naturally in any agent loop. Positioning is honest — we're shipping the missing piece, not competing with Const.

4. **Multi-chain runway.** The Ethereum equivalent of this gap is also empty. The architecture should anticipate eventual ETH/Solana/L2 support so we're prototyping a category, not a Bittensor-only tool.

### 2.3 What chainwake is not

This list matters because scope creep is the single biggest risk to the timeline.

- **Not an execution layer.** No signing, no transaction submission, no key handling. agcli's job. Phrased as a security claim: chainwake never holds keys, never signs, never submits extrinsics. This is structural, not policy — there is no signing code in the codebase to be misused.
- **Not a rules engine.** No `A AND B WITHIN W` boolean composition. Each watcher is one observable, one condition. Composition is the agent's job, achievable in three lines of bash.
- **Not a backtesting framework.** Watchers run forward in time against live chain state. Historical replay is taostats' job.
- **Not a hosted service.** No dashboard, no central server, no telemetry. The CLI is the product.
- **Not a notification platform.** Output adapters exist (via apprise integration) but the framework is observation-first. Users who want notification-platform features should consume chainwake's output and pipe it into their own systems.

---

## 3. Naming and brand

The product name is **chainwake**. Lowercase, one word, no separators.

The metaphor is precise: an agent sleeps, the watcher trips, the agent wakes. This is not just marketing — it describes the product's structural primitive (suspend and exit) and distinguishes it from continuous-monitoring alternatives.

### 3.1 Why chainwake over alternatives

`chainwatch` was the obvious alternative but is heavily contested — at least five active products and academic projects use it, including a multi-chain wallet monitor, an on-chain events subscription product, an AML compliance tool, an agent-safety tool, and an ML anomaly-detection paper. Adopting `chainwatch` would mean immediate brand confusion in the agent-tooling space.

`chainwake` is uncontested across web search and GitHub. The .com is owned but parked; alternative TLDs (.dev, .io, .sh) are available.

`obscli` was the working name in earlier proposals. Rejected because it's an acronym that requires explanation, doesn't carry the agent metaphor, and reads as an internal Blockmachine project rather than a category-defining tool.

### 3.2 Brand posture

chainwake is positioned as standalone open-source infrastructure. Blockmachine is named in the README as the default RPC provider for Bittensor and the company that built the tool, but blockmachine branding does not dominate the chainwake identity. The CLI does not include `bm_` prefixes or blockmachine-specific terminology in its core surface.

This separation matters strategically — if chainwake reads as "Blockmachine's house tool," it cannot become the canonical agent-observation primitive across chains where Blockmachine isn't the relevant provider.

### 3.3 Tagline

Working tagline: **"Sleep your agent until chain state changes."**

Six words. States what the product does and who it's for. Used at the top of the README and in launch comms.

---

## 4. Users and use cases

This section enumerates concrete user needs that drove the design. Use cases are stated in the user's voice ("I want X") rather than as features. The mapping from use cases to primitives is in §6 and the full command examples are in Appendix C.

### 4.1 Bittensor users

**Alpha holders / retail speculators.** Want price-driven signals delivered to phones. Don't write code.

- Email/Telegram/Discord ping when SN19 alpha price rises or falls more than X% in an hour
- Alert when SN19 alpha price drops below specific TAO value
- Notification when daily trading volume on a watched subnet spikes
- Alert when SN19 pool depth drops below painful-exit level
- Alert when one of my watched subnets receives an unusually large single trade

**Subnet owners / operators.** Care about their subnet's health, reputation, and economic dynamics.

- Notification when emission share on my subnet changes by X% over N epochs
- Alert when a hyperparameter on my subnet changes (whether or not I made the change)
- Alert when a new validator with significant stake starts validating my subnet
- Alert when an existing validator on my subnet stops setting weights
- Alert when a registered miner hasn't been seen for N epochs (about to deregister)
- Alert when burn rate on my subnet crosses a threshold either way

**Miners.** Care about staying registered, optimising emissions, avoiding deregistration.

- Wake me when I'm within X blocks of being deregistered from a subnet
- Wake me when registration cost on a subnet I want to mine drops below my budget
- Wake me when my incentive on a subnet drops more than X% epoch-over-epoch
- Wake me when my own neuron's last_update is stale (my node has stopped responding)
- Wake me when emission rate on a subnet I mine changes meaningfully

**Validators.** Care about their own validation health and stakers leaving them.

- Wake me when I miss a weight-set window
- Wake me when my dividends drop more than X% over an epoch
- Alert when a competitor validator's commission/take changes
- Alert when a delegator removes a large stake from me
- Alert when child keys are set or changed on validators I'm tracking

**Stakers / delegators.** Want validator-quality monitoring.

- Alert when the validator I'm staked to stops setting weights
- Alert when their commission/take rate increases
- Alert when their dividends drop sharply

**Subnet evaluators / Bittensor-twitter analysts.** Producing content; want signals that make threads.

- Notification when a new subnet is registered
- Notification when a subnet's burn rate changes significantly
- Notification when a coldkey I track makes any move
- Notification when a hyperparameter changes anywhere on the network (governance signal)
- Notification when subnet registration cost crosses round numbers (1000, 500, 100 TAO)

**OTC desks / treasuries.** Position-sized actors with execution and risk concerns.

- Alert when pool depth on subnet N can absorb a trade of size S without exceeding X bps slippage
- Alert when a counterparty coldkey moves stake
- Compliance-style alerts when transfers above a threshold touch addresses we monitor

**Bittensor-native developers.** Building on Bittensor.

- Alert when a subnet I'm building on changes a hyperparameter
- Alert when my deployed neurons go offline
- Wait for a transaction I submitted to be finalised
- Alert when storage at a specific key changes (debugging)

**Agentic systems on Bittensor.** Const's stack and the next wave of similar systems.

- Wake my agent when SN19's alpha price drops 5% so it can decide whether to buy
- Wake my agent when a new subnet registers so it can analyse and tweet about it
- Wake my agent when my staked validator goes silent for 3 epochs so it can rebalance
- Wake my agent when registration cost drops below a threshold so it can register
- Wait for a transaction my agent submitted to finalise before continuing

### 4.2 Ethereum and EVM users (planned for a future release; informs the initial release architecture)

These use cases are not addressed in the initial release (only the Bittensor backend ships) but inform the resource and primitive design so the architecture is honestly multi-chain.

**DeFi traders.** Token price thresholds on Uniswap, gas-price triggers, lending-position health monitoring, whale-wallet transfer alerts.

**Smart contract developers.** Transaction finality waits in deployment scripts, function-call alerts, storage-slot change detection in CI pipelines.

**DAO governance participants.** Proposal-created notifications, voting-window alerts, treasury transfer tracking.

**Security researchers.** Dormant contract activity, phishing-address fund movements, contract upgrade detection, flash-loan threshold alerts.

**Cross-chain agents.** Bridge-transfer landing detection, finality waits on optimistic rollups, cross-chain liquidity monitoring.

### 4.3 Communities not addressed in the initial release

The use case enumeration deliberately omits some communities to keep scope tight. Notably:

- **NFT collectors** (floor-price alerts) — requires off-chain marketplace data; out of the initial release.
- **MEV searchers** (mempool-pattern detection) — mempool observation is heavier and narrower; deferred.
- **Casual price-only traders** without RPC access — chainwake's value depends on having a chain RPC connection; not the right tool for users who just want CEX price alerts.

---

## 5. Command surface

### 5.1 Top-level shape

```
chainwake <chain> <resource> [<id>] [<sub-resource>] [flags]
```

Where:
- `<chain>` is the chain identifier (`bt` / `bittensor`, `eth` / `ethereum` later)
- `<resource>` is a domain-specific noun aligned with the chain's vocabulary
- `<id>` is the resource identifier (subnet number, address, hotkey, etc.)
- `<sub-resource>` is an optional further qualifier (`pool`, `weights`, `commission`, etc.)
- flags express the watch condition and any options

### 5.2 Resource-first design rationale

The original design considered primitive-first commands (`chainwake bt threshold subnet.19.pool.price ...`). Resource-first was chosen for three reasons:

1. **Resources gate primitives.** A `subnet` resource has prices, depths, registration costs — these are watchable as numeric thresholds, deltas, or state changes. A `tx` resource is inherently a finality wait. Tying primitives to resources prevents nonsense combinations and makes auto-complete genuinely informative at every step.

2. **Familiarity with btcli.** Bittensor users already know `btcli stake add`, `btcli subnet register`, `btcli wallet new_coldkey`. Resource-first commands inherit that mental model. The README framing: "if you know btcli, you'll recognise the resources. The verbs are different because chainwake watches rather than acts."

3. **Auto-complete value.** Once the user types `chainwake bt subnet 19`, auto-complete can show only the sub-resources and flags that apply to subnets. Primitive-first would show all six primitives regardless of context, which is less useful.

### 5.3 Chain selection and aliases

Every command starts with a chain selector. Two forms accepted, equivalent in all behaviour:

```
chainwake bt subnet 19 price --drop-pct 5 --window-time 1h
chainwake bittensor subnet 19 price --drop-pct 5 --window-time 1h
```

Short forms (`bt`, `eth`) are the default in documentation because shorter wins in scripts and agent-generated commands. Long forms work identically.

### 5.4 Verbs in the CLI

The product is `chainwake`; every subcommand is implicitly a watch. Therefore:

- The word `watch` does not appear in subcommands.
- The primitive type (threshold, delta, event, liveness, state, tx) is implicit in the flags chosen, not in the command shape.

This keeps commands short and resource-natural. Examples:

```
chainwake bt subnet 19 price --drop-pct 5 --window-time 1h    # delta
chainwake bt subnet 19 registration-cost --below 500          # threshold
chainwake bt event --type subnet-registered                           # event
chainwake bt validator 5Fxxx weights --silent-for 3epochs     # liveness
chainwake bt validator 5Fxxx commission --on-change           # state
chainwake bt tx 0xabababababababababababababababababababababababababababababababab --finality finalized  # tx
```

The flag set per resource is deliberately constrained so the user cannot accidentally attempt a nonsensical combination.

### 5.5 Flag conventions

A small, consistent flag vocabulary that maps to the underlying primitives:

| Flag pattern | Primitive | Semantics |
|---|---|---|
| `--below <value>` | threshold | Fire when observable < value |
| `--above <value>` | threshold | Fire when observable > value |
| `--rise-pct <n>` + `--window-*` | delta | Fire when observable rises by n% within window |
| `--drop-pct <n>` + `--window-*` | delta | Fire when observable drops by n% within window |
| `--move-pct <n>` + `--window-*` | delta | Fire on either-direction move of n% within window |
| `--on-change` | state | Fire on any change |
| `--changes-to <value>` | state | Fire on transition to specific value |
| `--changes-from <value>` | state | Fire on transition from specific value |
| `--silent-for <duration>` | liveness | Fire after no activity for duration |
| `--on-<event-name>` | event | Resource-specific event subscriptions |
| `--finality <level>` | tx | Wait for inclusion or finalisation |
| `--confirmations <n>` | tx | Wait for N confirmations (Ethereum) |

Common cross-cutting flags:

| Flag | Purpose |
|---|---|
| `--out <uri>` | Output adapter (default: exit with JSON to stdout). Repeatable. |
| `--name <string>` | Human-readable label for this watcher (visible in process tooling) |
| `--max-runtime <duration>` | Hard upper bound on runtime; exits with status `timeout` if reached |
| `--max-ru <integer>` | Registry-estimated observation budget; not a provider billing cap |
| `--rpc-url <url>` | Override default RPC endpoint |
| `--api-key <string>` | Override env-var API key |

### 5.6 Window units

Three mutually exclusive flags for time windows in delta watchers:

```
--window-time 1h       # 1h, 30m, 5d, 90s — universal time syntax
--window-blocks 50     # 50 blocks — chain-native, works on any chain
--window-epochs 5      # 5 epochs — Bittensor-specific, rejected on chains without epochs
```

Exactly one must be specified for primitives that require a window. Specifying multiple is an error.

### 5.7 Duration syntax

A single `<n><unit>` shape is used everywhere a duration is expressed:

- `30s`, `5m`, `2h`, `7d` — time
- `100blocks`, `5epochs` — chain-native units (where applicable)

Used in `--silent-for`, `--max-runtime`, and similar.

### 5.8 Auto-complete

chainwake ships with shell auto-complete from day one. The CLI is built on `typer`, which provides `--install-completion` for bash, zsh, fish, and PowerShell as a built-in.

Completion is context-aware — after `chainwake bt subnet`, completion suggests valid subnet IDs (or accepts any integer). After `chainwake bt subnet 19`, completion suggests valid sub-resources (`price`, `pool`, `registration-cost`, etc.). After `chainwake bt subnet 19 price`, completion suggests applicable flags (`--below`, `--above`, `--rise-pct`, etc.).

This is built on the registry described in §6; the registry is the single source of truth for what's valid and what auto-completes.

---

## 6. Resources, primitives, and observables

### 6.1 The six primitive shapes

Every chainwake watcher is one of six structural shapes. These are internal categories — users don't type primitive names — but they determine the watcher's state, polling, and exit semantics.

1. **Threshold** — observable crosses an absolute target. Stateless within a tick. Fires on first match. Examples: `price < 0.05`, `registration_cost < 500`.

2. **Delta** — observable moves by N% within a window. Stateful — the watcher holds a sliding window of historical values. Examples: `price drops 5% in 1h`.

3. **Event** — filtered emission of a named event with arguments matching a filter. Subscription-based where supported. Default exits on first match. Examples: `subnet_registered`, `swap to_subnet=19 tao_min=10000`.

4. **Liveness** — fires when an observable's last-activity timestamp exceeds a duration. Examples: `validator silent for 3 epochs`, `oracle update older than 10m`.

5. **State** — value at a path changed (in any direction, or to/from a specific value). Stateless on each tick (compares current vs previous). Examples: `commission changed`, `hyperparameter set`.

6. **Tx** — wait for a specific transaction hash to reach a specified finality level. Narrow but important for write-then-wait agent patterns.

### 6.2 Resource set at launch (Bittensor)

Seven resources, mirroring btcli's vocabulary where possible:

| Resource | Description | Example IDs |
|---|---|---|
| `subnet` | A Bittensor subnet (netuid) | `19`, `64`, `1` |
| `validator` | A validator identified by hotkey | `5Fxxx...` |
| `neuron` | A registered neuron (subnet × hotkey) | `19 5Fxxx...` |
| `account` | A coldkey account (balance, activity) | `5Fxxx...` |
| `network` | Chain-wide values (registration cost, runtime version) | (no ID) |
| `event` | The chain-wide event firehose for filtered subscriptions | (no ID) |
| `tx` | Transaction finality wait | `0xabc...` |

Each resource exposes a defined set of sub-resources and observables. Anything not explicitly declared is rejected at parse time with a useful error.

The full registry is in [Appendix A](#appendix-a-full-bittensor-resource-and-observable-registry).

### 6.3 Computed observables

Some observables are derived from chain state rather than read directly. They are documented and behave identically to native observables, but their values are computed at evaluation time from one or more underlying chain reads.

Two computed observables ship in the initial release:

**`subnet.<netuid>.pool.depth-for-trade`** — given parameters `--size <amount>` and `--max-bps <basis-points>`, computes whether the pool can absorb a trade of the given size at the given slippage budget. Returns a numeric value (positive when the trade is feasible, indicating margin; non-positive when not). Used with `--above 0` to fire when the trade becomes feasible.

**`neuron.<netuid>.<hotkey>.blocks-until-immunity-expires`** — exact block
countdown until immunity ends. It derives the expiry from `ImmunityPeriod` and
`BlockAtRegistration`; Chainwake does not convert this block rule into epochs.

The principle: where a use case requires combining multiple raw reads to express what the user actually cares about, the combination is encapsulated as a computed observable rather than forcing the user to write the math themselves. The list at launch is small (two) and grows in response to actual user requests rather than speculative addition.

Current Subtensor does not expose a pruning score or deterministic future
deregistration block. Replacement is selected only when a full subnet receives
a registration, using relative emission, registration age, immunity, and owner
protections. A countdown would therefore make a promise the chain does not make.

### 6.4 The path namespace

Internally, every observable has a canonical dotted path:

```
subnet.19.pool.price
subnet.19.registration-cost
neuron.19.5Fxxx.incentive
validator.5Fxxx.commission
validator.5Fxxx.weights
account.5Fxxx.balance
account.5Fxxx.activity
network.subnet-registration-cost
network.runtime-version
```

Paths are lowercased and hyphen-separated within segments, dot-separated between segments. The CLI accepts both the resource-first command form (`subnet 19 price`) and the dotted-path form (where exposed in tooling). The dotted form is the canonical reference in docs, JSON output, and the registry.

### 6.5 Per-observable metadata

Each observable in the registry declares:

```python
{
    "path_template": "subnet.{netuid}.pool.price",
    "resource": "subnet",
    "type": "numeric",                # numeric | event | state-bytes | bool | tx-status
    "natural_cadence": "per_block",   # per_block | per_epoch | other
    "subscription_supported": False,  # whether WS subscription is available
    "applicable_primitives": ["threshold", "delta", "state"],
    "description": "Alpha price in TAO, computed from dTAO pool reserves.",
    "computed": False,                # True for derived observables
    "computed_args": [],              # required CLI args for computed observables
}
```

This metadata drives:
- CLI parse-time validation (which flags are valid for which observable)
- Auto-complete (what sub-resources and flags to suggest)
- Polling default (how often to read by default)
- MCP tool schema generation (for the MCP wrapper, see §14)
- Documentation generation (the registry is the docs source-of-truth)

---

## 7. Output contract

### 7.1 The sacred contract

The JSON output emitted on a matched event is the most consequential public interface chainwake exposes. Once committed, breaking it requires a major version bump.

Every watcher, on a successful match, emits to stdout (or the configured adapter) a single JSON document of this shape:

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
    "window": {"unit": "time", "value": "1h"}
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

Non-match exits use the same envelope with different `status`:

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

```json
{
  "status": "budget_exhausted",
  "reason": "max_ru_reached",
  "watcher": { ... },
  "condition": { ... },
  "observed": null,
  "budget": { ... },
  "process": { ... }
}
```

### 7.2 Why the payload is verbose

The decision to include `invocation` (the full original CLI args) and full `condition` and `observed` blocks was deliberate. Reasons:

1. **Agent re-invocation.** An agent that wakes on a chainwake event needs full context to decide what to do next. Original invocation lets the agent reconstruct or relaunch the watcher trivially.

2. **Audit trail.** Logged JSON is self-describing. A user grepping yesterday's logs doesn't need to cross-reference what the watcher was watching.

3. **Debugging.** When something fires unexpectedly, the payload contains enough information to diagnose without re-running the watcher.

The payload is small even when verbose (typically <2KB), so the cost is negligible.

### 7.3 Output contract

`schemas/output.json` is the sole current output contract. Consumers must
validate payloads against it and reject unknown fields. Output shape changes
update the Pydantic models and this schema atomically. The schema is published
as part of the release artefact.

### 7.4 Validation in implementation

Internally, every output payload is constructed via Pydantic models. Pydantic enforces the schema at build time and serialises consistently. The published JSON schema is generated from the Pydantic models, so the contract and the implementation cannot drift.

---

## 8. Output adapters

### 8.1 Default behaviour: exit with JSON

When no `--out` flag is specified, chainwake writes the JSON payload to stdout and exits. This is the default agent contract — the most predictable, most Unix-shaped behaviour.

### 8.2 The `--out` flag

`--out <uri>` configures an output adapter. The flag is repeatable; multiple adapters receive the same payload on each match.

```bash
# Default: exit, JSON to stdout
chainwake bt subnet 19 price --drop-pct 5 --window-time 1h

# Single notification destination
chainwake bt subnet 19 price --drop-pct 5 --window-time 1h --out tgram://bot_token/chat_id

# Multiple destinations
chainwake bt subnet 19 price --drop-pct 5 --window-time 1h \
  --out tgram://bot_token/chat_id \
  --out discord://webhook_id/webhook_token

# Streaming (don't exit on first match)
chainwake bt event --type subnet-registered --out stream

# Streaming + notification
chainwake bt event --type subnet-registered --out stream --out tgram://bot_token/chat_id
```

### 8.3 The apprise integration

chainwake uses [apprise](https://github.com/caronc/apprise) for notification adapter URIs. apprise is an MIT-licensed Python library that supports ~100 notification destinations behind a unified URI scheme.

The decision to adopt apprise rather than build adapters ourselves:

- **Coverage** — Telegram, Discord, Slack, Mattermost, ntfy, Pushover, Gotify, Matrix, PagerDuty, email, generic webhooks, and ~90 others, all on day one.
- **Maintenance** — apprise is actively maintained by a substantial team. We inherit their adapter improvements without doing work.
- **Familiarity** — apprise URIs are already known to users of self-hosted alerting (Sonarr, Radarr, etc.). Lower learning curve.
- **License compatibility** — MIT.

apprise URIs are passed through to the apprise library mostly verbatim. chainwake's contribution is wrapping the JSON payload into a notification-appropriate format (subject + body) before passing it to apprise.

Documented adapters in the initial release (covered by apprise):

| URI scheme | Destination |
|---|---|
| `tgram://botoken/chatid` | Telegram |
| `discord://webhook_id/webhook_token` | Discord webhook |
| `slack://...` | Slack |
| `mailto://user:pass@host` | Email (SMTP) |
| `mattermost://...` | Mattermost |
| `ntfy://...` | ntfy.sh self-hosted notification |
| `gotify://...` | Gotify self-hosted |
| `pover://...` | Pushover |
| `webhook://...` / `json://...` | Generic JSON POST |

The full apprise URI reference is linked from chainwake's documentation.

### 8.4 chainwake-native adapters

Two adapters are not from apprise and are implemented directly in chainwake:

**`stream`** — keeps the watcher process alive and emits one JSON line per match (newline-delimited JSON). Used for continuous-monitoring use cases. Combinable with notification adapters.

**`file:///path/to/log.jsonl`** — appends the JSON payload to a file. Useful for run logging.

Both are trivial to implement and don't fit apprise's model.

### 8.5 Default exit-on-first-match semantics

Without `--out stream`, every watcher exits on first match. This is the agent contract — predictable, structurally simple, no hidden state.

A user who wants continuous monitoring opts in via `--out stream`. There is no `--continuous` flag — streaming is just an adapter.

This keeps the architecture clean: every watcher invocation either exits on first match (default) or runs forever emitting matches (with `--out stream`). No third mode.

### 8.6 Credential handling for adapters

Notification adapters often require credentials (Telegram bot tokens, Discord webhook URLs, Slack tokens). The honest approach: credentials live in environment variables.

```
# .env or shell config
TELEGRAM_BOT_TOKEN=...
DISCORD_WEBHOOK_URL=...

# Then in commands:
chainwake bt subnet 19 price --drop-pct 5 --window-time 1h \
  --out tgram://${TELEGRAM_BOT_TOKEN}/chatid
```

apprise URIs can also include credentials inline (e.g. `tgram://botoken/chatid`). chainwake supports both forms. Documentation strongly recommends env-var separation for anything sensitive.

---

## 9. Polling, finality, and natural cadence

### 9.1 Natural cadence per observable

Each observable in the registry declares its natural cadence — the rate at which the underlying chain state can change:

- **`per_block`** — value can change every block (~12s on Bittensor mainnet). Examples: balance, pool price, emission share.
- **`per_epoch`** — value can only change at epoch boundaries. Examples: burn rate, weights, validator dividends.
- **`per_event`** — observable is event-driven; subscribe and wait for one
  required friendly-name or raw `Module.Event` filter.
- **`other`** — observable has unusual cadence; documented per-case.

Each registry entry owns an observation policy which maps its applicable
primitives to a storage-change, best-head, subnet-epoch, event-stream,
transaction-status, or timer driver. Callers do not choose a polling interval.

### 9.2 Polling and subscriptions

Exact and composite storage-backed threshold/state watchers use WebSocket
storage subscriptions. Baseline deltas without an explicit rolling window use
the same path. The watcher evaluates one baseline and then re-reads the
canonical observable at each notified block. Pool price subscribes to both
`SubnetTAO` and `SubnetAlphaIn`; burn rate subscribes to `MinerBurned`.

Remaining `per_block` observables subscribe to new best-block headers and read
at the pinned notified block. The Bittensor provider prefers
`chainHead_v1_follow`, bootstraps one finalized height, derives later heights
from ordered `newBlock` parent links, and consumes direct `bestBlockChanged`
hashes. It unpins obsolete initialized blocks immediately, batches finalized
and pruned block unpins, and unfollows on close. If the method is unavailable,
it falls back to `chain_subscribeNewHeads` and one hash lookup per notified
block.

Remaining `per_epoch` observables use the same stream to inspect their
chain-owned epoch marker, reading the observable only when that marker
advances. An epoch-sized rolling window changes the comparison horizon, not
the observable's natural sampling cadence: per-block values are still sampled
per block and annotated with epoch state. Computed observables without declared
storage dependencies retain block-sampling semantics. Transaction finality and
unusual cadences use their dedicated drivers.

Event watchers reuse the same direct best-head `BlockRef`, fetch and decode
that block's events and chain timestamp, then apply exactly one curated
friendly-name or raw canonical `Module.Event` filter. The direct path avoids
separate hash and block-number lookups.

An event-subscription failure exits with
`provider_error: subscription_failed`; Chainwake does not silently change transport.
This keeps the failure and its RU implications explicit.

Storage subscriptions reconnect after transient interruption. They fall back
to polling only when the provider explicitly reports that the observable has
no supported storage mapping.

### 9.3 Boundary alignment

For subnet-epoch-driven observables, the watcher inspects the subnet's
chain-owned epoch state once per block and reads the observable only when
`SubnetEpochIndex`/`LastEpochBlock` advances. Tempo is subnet-specific and
mutable, and owners can trigger epochs early, so block-modulo arithmetic is
not a valid boundary detector.

### 9.4 Connection failure handling

RPC providers go down. Networks blip. The watcher must reconnect with backoff:

- **Transient connection failures** (network errors, 5xx responses, WebSocket drops): exponential backoff starting at 500ms, capped at 30s, retry indefinitely until `--max-runtime` expires.
- **Authentication failures** (`-32021` invalid API key): exit immediately with status `auth_error`. No retry.
- **Rate limiting** (`-32029`): backoff 250ms × 2^attempt for up to 8
  consecutive attempts. Every wait is bounded by `--max-runtime` and shutdown.
  A persistent ninth response is terminal `provider_error: rate_limited`, with
  guidance to wait, poll less often, or sign up for higher limits. The runtime
  never switches to a zero-delay retry loop.
- **Insufficient compute units** (`-32030`): exit with status `budget_exhausted` and a message pointing the user to upgrade their plan.

Authentication and CU failures are terminal regardless of the estimate guard.

The runtime preflights each provider read against its registry `read_cost`.
Pinned epoch-state reads cost four RU. A call is not issued when its full
declared cost would exceed `--max-ru`; `rpc_calls` counts successful
top-level provider calls while `estimated_ru_consumed` records their declared
underlying chain-read cost. A read whose cost lands exactly on the cap is
evaluated; only a subsequent read is blocked.

`--max-ru` is a registry-estimated observation budget, not a provider billing
cap. It does not meter the transport boundary, so connection bootstrap,
transient retries, batched request weighting, and RPCs hidden inside the SDK
are excluded. Provider-side compute-unit exhaustion remains independently
terminal.

### 9.5 RU estimation

At startup, every watcher prints a registry-estimated RU/day to stderr based on
the observable's natural cadence and the watcher's primitive type:

```
$ chainwake bt subnet 19 price --drop-pct 5 --window-time 1h
Registry-estimated RU: ~7,200/day · cadence per_block · poll 12s ·
1 read/tick x 1 RU/read · runtime unbounded · max_ru estimate unset ·
excludes bootstrap/retries/SDK RPCs
```

The estimate describes declared observation work only. It is not guaranteed to
be conservative relative to provider billing because transport-level work is
excluded.

---

## 10. Authentication, providers, and configuration

### 10.1 Provider trait

The Python implementation defines a small `ChainProvider` interface plus
optional head, event, storage, and epoch capability interfaces (see §13).
`chainwake/chains.py` binds each public chain alias to a provider factory and
its runtime timing/cost profile. The observable catalogue is independently
scoped by `(chain, path)`. The initial release ships one implementation: Bittensor, targeting
`rpc.blockmachine.io` by default.

Adding Ethereum requires an ETH provider, catalogue entries, CLI tree, and
integration harness. Existing primitives, watcher lifecycle, durable jobs, and
adapters remain shared. A new output shape is a separate, explicit schema
version rather than an incidental consequence of adding a backend.

### 10.2 Authentication

The default Blockmachine RPC has an anonymous free tier. **No API key is required**
to install Chainwake, run a first watcher, or use the default endpoint. Start
without credentials; if the free tier returns a rate limit, wait or reduce the
polling cadence. For higher limits, sign up with Blockmachine and add an optional
API key.

When an API key is configured, it is resolved with strict precedence:

1. **CLI flag** `--api-key <key>` (highest precedence)
2. **Environment variable** `CHAINWAKE_BT_API_KEY` (per-chain) or `CHAINWAKE_API_KEY` (global fallback)
3. **Anonymous access** — no credential is sent

There is no config file in the initial release. (See §15.)

Per-chain env vars (`CHAINWAKE_BT_API_KEY`, `CHAINWAKE_ETH_API_KEY` later) allow users to set credentials for multiple chains without conflict.
An alternative RPC endpoint may still require authentication; its authentication
failure is reported as an `auth_error`.

### 10.3 RPC URL override

Defaults to `wss://rpc.blockmachine.io` for `bt`. Override via:

1. **CLI flag** `--rpc-url <url>`
2. **Environment variable** `CHAINWAKE_BT_RPC_URL`

This is the seam by which users could in principle point chainwake at a non-Blockmachine Bittensor RPC. The initial release doesn't actively support this (Blockmachine is the only documented provider), but the architecture doesn't prevent it.

### 10.4 Why no config file in the initial release

Considered and rejected for scope reasons. A config file adds: parsing, validation, location ambiguity (project-local vs user-global), env-var-override semantics, secret handling debate. None of these are essential to the initial release.

After the initial release, a config file at `~/.chainwake/config.toml` is the natural addition — it's a one-week feature that doesn't require any architectural change. Listed in the future roadmap.

---

## 11. Error handling and exit codes

Five exit codes, deliberately simple. Agents parse JSON for detail; exit codes are for shell-level control flow.

| Code | Status field | Meaning |
|---|---|---|
| `0` | `matched` | Condition fired; payload on stdout describes the match |
| `1` | `stopped` / `timeout` / `budget_exhausted` | Watcher stopped or ran to completion without firing |
| `2` | `user_error` | Invalid command, unparseable args, unknown resource, etc. |
| `3` | `provider_error` | RPC auth failed, RPC unreachable beyond retry limit |
| `4` | `internal_error` | Bug in chainwake; please file an issue |

Every watcher exit in JSON mode emits a JSON payload on stdout. Non-match exits
include a `reason` describing the timeout, budget limit, shutdown, or error
class. Configuration helpers, help/version output, and parser failures before
watcher selection are outside the watcher payload contract.

Stderr is reserved for human-readable progress messages, warnings, and connection diagnostics. Stderr is not part of the stable contract; it changes between versions.

---

## 12. Architecture and implementation

### 12.1 Language: Python

Python 3.13. Distributed via `pip install chainwake` and `uv tool install chainwake`. Single entry point: `chainwake`.

The choice of Python over Rust (considered earlier) is justified by:

1. **Bittensor ecosystem alignment** — `substrate-interface`, `bittensor` SDK, and most agent tooling on Bittensor is Python.
2. **Faster iteration for the initial release** — we want to learn from real users, not perfect a binary.
3. **Lower contributor barrier** — the community can extend chainwake without learning Rust.
4. **`apprise` is Python** — adopting it is trivial in Python, bridge work in Rust.
5. **Latency is not a concern** — chainwake watchers run for minutes to days, not milliseconds; Python's startup cost is amortised over the watcher's lifetime.

A Rust rewrite is a v3+ consideration if Python performance becomes a real constraint, which is not expected for this product shape.

### 12.2 Core dependencies

| Package | Purpose |
|---|---|
| `typer` | CLI framework with auto-complete |
| `pydantic` | Schema validation for output contract and config |
| `httpx[http2]` | HTTP client |
| `websockets` | WebSocket client |
| `substrate-interface` | SCALE codec, Substrate JSON-RPC, runtime metadata |
| `apprise` | Notification adapters |
| `structlog` | Structured logging to stderr |
| `tenacity` | Retry/backoff handling |

All MIT or Apache-2.0 licensed. No GPL dependencies.

`bittensor-wallet` is intentionally not depended on — chainwake never holds keys.

### 12.3 Module layout

```
chainwake/
├── __main__.py                 # Entry point; delegates to cli
├── chains.py                   # Chain aliases, provider factories, runtime profiles
├── cli/
│   ├── __init__.py
│   ├── app.py                  # Typer app construction
│   ├── chains/
│   │   ├── bittensor.py        # bt subcommand tree
│   │   └── (ethereum.py later)
│   └── completion.py           # Auto-complete logic
├── core/
│   ├── primitives/             # Implementations of threshold, delta, event, etc.
│   ├── registry.py             # Resource and observable registry
│   ├── runtime.py              # Watcher lifecycle, subscription, polling
│   ├── retry.py                # Backoff and reconnect logic
│   └── budget.py               # Registry RU estimation and estimate guard
├── providers/
│   ├── base.py                 # Base provider and optional capability interfaces
│   └── bittensor.py            # Bittensor implementation
├── output/
│   ├── schema.py               # Pydantic models for output payload
│   ├── adapters.py             # Adapter dispatch (default, stream, file, apprise)
│   └── apprise_bridge.py       # apprise URI handling
└── tests/
    ├── unit/
    └── integration/
```

### 12.4 The registry as source of truth

The resource and observable registry (see Appendix A for the full Bittensor
list) declares chain-scoped observables, their metadata, and their applicable
primitives. Provider-specific storage/event metadata is scoped by chain too.
This single source of truth drives:

- CLI argument validation
- Auto-complete suggestions
- MCP tool schema generation
- Documentation generation
- Type inference for output payloads

The registry is code, not config. Adding an observable requires a code change and a test, which is the right friction level — observables are public interface and shouldn't be added casually.

### 12.5 Concurrency model

Each watcher invocation is a single process running an asyncio event loop. The loop concurrently:

- Maintains the RPC connection (HTTP or WebSocket)
- Polls or receives subscription updates
- Evaluates the watcher's condition on each tick
- Handles signals (SIGTERM, SIGINT) for clean exit

There is no internal multi-watcher process. One chainwake invocation = one watcher = one observable = one condition. Multi-watcher orchestration is the agent's or shell's job.

### 12.6 Multi-watcher patterns (out of process)

Agents and humans wanting to run multiple watchers spawn multiple chainwake processes. Three patterns:

```bash
# Bash parallel — wait for any to fire
chainwake bt subnet 19 price --drop-pct 5 --window-time 1h &
chainwake bt subnet 64 price --drop-pct 5 --window-time 1h &
wait -n  # exits when first child exits

# Python asyncio — wait for any to fire
import asyncio, subprocess
procs = [
    asyncio.create_subprocess_exec("chainwake", "bt", "subnet", str(n), "price", ...)
    for n in (19, 64, 1)
]
done, _ = await asyncio.wait(procs, return_when=asyncio.FIRST_COMPLETED)

# systemd / supervisord — long-lived watcher fleet
# (Out of the initial release chainwake's concern; user manages with their tool of choice)
```

### 12.7 Testing strategy

Three layers:

**Unit tests** for pure logic — primitive evaluation, registry lookups, schema validation, retry policies, condition matching.

**Integration tests** against a recorded RPC fixture — replay captured Substrate responses to validate end-to-end watcher behaviour without requiring live network access.

**Smoke tests** against `rpc.blockmachine.io` for CI — a small set of cheap watchers run against the live network on every release to validate the real provider integration.

Coverage target: 85% for unit, full smoke pass at release.

---

## 13. Plugin interfaces (anticipated, not implemented)

The initial release ships zero plugins. The interfaces are described here so the architecture is honest about its anticipated extension points. Implementations are subject to refinement during the initial release.

### 13.1 Chain backend and capability interfaces

Every chain backend implements the small common surface:

```python
class ChainProvider(Protocol):
    name: str                      # "bittensor"
    short_alias: str               # "bt"

    async def connect(self, config: ProviderConfig) -> None: ...
    async def disconnect(self) -> None: ...

    async def read_observable(
        self,
        path: str,
        args: dict,
        at_block: BlockRef | None = None,
    ) -> ObservableValue:
        """Read a single observable value at the given block (or head)."""

    async def get_block_finality(self, tx_hash: str) -> TxFinalityStatus: ...
```

Transport and chain concepts are optional capabilities:

```python
class HeadSubscriptionProvider(Protocol):
    def subscribe_heads(...) -> AsyncIterator[BlockRef]: ...

class EventSubscriptionProvider(Protocol):
    def subscribe_events(...) -> AsyncIterator[Event]: ...

class StorageSubscriptionProvider(Protocol):
    def subscribe_storage(...) -> AsyncIterator[StorageUpdate]: ...

class EpochProvider(Protocol):
    def epoch_netuid_for(...) -> int | None: ...
    async def get_epoch_state(...) -> EpochState: ...
```

Each `ChainBackend` registers a provider factory and `ChainRuntimeConfig`
(natural block interval and observation-cost constants). The runtime checks
capabilities structurally and follows the observable policy's declared
fallback when a capability is absent. Bittensor implements all four optional
capabilities; Ethereum need only implement those its catalogue selects.

### 13.2 `ObservableProvider` — off-chain observable plugin

A plugin that exposes off-chain values as observables under a path prefix:

```python
class ObservableProvider(Protocol):
    path_prefix: str               # e.g. "taostats", "coingecko"

    async def read(self, path: str, args: dict) -> ObservableValue: ...

    @property
    def natural_cadence(self) -> Cadence: ...
```

A USD-conversion plugin would expose `taostats.subnet.{netuid}.price-usd` and behave identically to a native observable. Watchers don't distinguish between native and plugin-provided observables.

### 13.3 What's deliberately not defined

**Notification adapter plugin.** apprise covers ~100 destinations and the URI scheme is established. We do not define a parallel plugin interface for adapters — extension happens by extending apprise (via PR upstream) or by users post-processing chainwake's stdout. This is documented as the supported pattern.

**Watcher primitive plugin.** The initial release ships six primitives; we do not expose a plugin interface for new primitives. New primitives, if needed, are added to chainwake itself with a major package-version review.

### 13.4 Plugin discovery

If plugins are enabled later, discovery is via Python entry points. Plugins declare themselves in their `pyproject.toml`:

```toml
[project.entry-points."chainwake.chain_providers"]
solana = "chainwake_solana.provider:SolanaProvider"

[project.entry-points."chainwake.observable_providers"]
taostats = "chainwake_taostats.provider:TaostatsProvider"
```

chainwake at startup enumerates entry points and registers discovered providers. No central registry, no manual configuration.

---

## 14. MCP wrapper

### 14.1 Scope

The `chainwake` package includes an MCP server. It exposes the wired,
exit-oriented watcher commands as tools so MCP-compatible clients can invoke
them without managing subprocess output themselves. There is no second package
or executable to install.

### 14.2 Tool generation

The MCP server enumerates the chainwake registry at startup and generates one MCP tool per (chain, resource, observable) combination. Each tool's input schema is derived from:

- The applicable primitives for the observable
- The flags supported by each primitive
- The required and optional CLI arguments

A shortened excerpt of the generated subnet-price schema looks like:

```json
{
  "name": "chainwake_bt_subnet_price",
  "description": "Watch alpha price for a Bittensor subnet's pool. Suspends until threshold or delta condition fires.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "netuid": {"type": "integer", "description": "Subnet ID"},
      "condition": {
        "oneOf": [
          {
            "type": "object",
            "properties": {
              "kind": {"const": "below"},
              "value": {"type": "number"}
            },
            "required": ["value"]
          },
          {
            "type": "object",
            "properties": {
              "kind": {"const": "drop-pct"},
              "pct": {"type": "number", "exclusiveMinimum": 0},
              "window_time": {"type": ["string", "null"]},
              "window_blocks": {"type": ["integer", "null"]},
              "window_epochs": {"type": ["integer", "null"]}
            },
            "required": ["pct"]
          }
        ]
      },
      "max_runtime": {"type": "string"},
      "max_ru": {
        "type": "integer",
        "description": "Registry-estimated observation budget; not a provider billing cap. Excludes bootstrap, retries, and hidden SDK RPCs."
      }
    },
    "required": ["netuid", "condition"]
  }
}
```

The complete generated schema uses a `kind` discriminator. Threshold variants
carry `value`; percentage-move variants carry `pct` and zero or one window
field. Omitting every window field selects the watcher-start baseline.

### 14.3 Execution

When an MCP tool is invoked, the MCP server constructs the equivalent CLI arguments, executes `chainwake` as a subprocess, and returns the exit JSON to the MCP caller. The subprocess inherits the user's environment for credentials.

### 14.4 Stdio and HTTP modes

The MCP server supports both:

- **Stdio mode** (`chainwake mcp serve --stdio`) for local MCP clients
- **Loopback HTTP mode** (`chainwake mcp serve --port 8080`) for local
  Streamable HTTP clients. Non-loopback binding is rejected because v0.1 does
  not implement HTTP authentication.

### 14.5 Why MCP is in the initial release scope

The original spec deferred this to later. Reconsidered because the work is minimal (~2 days):

- The registry already provides everything needed for tool schema generation
- The MCP Python SDK handles transport and protocol
- No new chainwake-side logic is required — the wrapper is a thin shell

Shipping this in the initial release means agentic users (Claude users, Cursor users, MCP-aware harnesses) get native integration on day one rather than waiting for a fast-follower.

---

## 15. Out of scope for the initial release

Listed explicitly so scope creep is visible. Each item is a fast-follower or a future release candidate, not abandoned.

### 15.1 Further subscription coverage

Exact/composite storage wakes, best-head wakes, subnet-epoch wakes, filtered
events, and transaction status now have registry-selected observation drivers.
Future coverage can add more computed or runtime-specific subscriptions behind
that policy without adding transport knobs to every CLI and MCP method.

### 15.2 Process management commands

`chainwake list`, `chainwake stop`, `chainwake logs` were considered for the initial release. Deferred as a fast-follower (the first follow-up release).

The pattern: each watcher writes a small JSON file to `~/.chainwake/runs/<pid>.json` on startup with its config and removes it on exit. `chainwake list` reads the directory; `chainwake stop` is sugar for `kill`; `chainwake logs` tails the watcher's stderr.

The existing `--name` flag (in the initial release) is the seed for this — it's stored in the run file and surfaces in `chainwake list` once that command exists.

### 15.3 Daemon mode

Not in the initial release. A daemon that hosts multiple watchers in one process is genuinely a different product (IPC, lifecycle, crash recovery, log aggregation).

When users routinely have 50+ watchers and connection-multiplexing matters, `chainwake daemon` is the natural addition. The existing process-management commands work the same way against the daemon as against the subprocess pool.

### 15.4 Config file

`~/.chainwake/config.toml` for default flags, named credential profiles, etc. Fast-follower (later releases).

### 15.5 Ethereum backend

The cross-chain foundation is implemented: provider selection, observable
lookup, optional capabilities, and runtime timing/costs are chain-scoped.
Ethereum commands, catalogue, provider, and Anvil integration remain the next
vertical slice.

### 15.6 Plugin implementations

The plugin interfaces are described (§13) but no plugins ship in the initial release. First plugin implementation likely the taostats off-chain observable provider for USD conversion, later.

### 15.7 Aggregate / rank operations

Watchers like "wake when SN19 drops out of the top-10 by emission share" require holding the full set of subnets in memory each evaluation. The use case is real (analysts especially); the engineering is heavier than the initial release supports.

### 15.8 Mempool / pre-finality observation

`author_pendingExtrinsics` is heavier per call and the audience (MEV searchers, front-run defence) is narrower.

### 15.9 Scheduled digests

"Send me a daily summary at 9am UTC" is a different shape from suspend-and-exit — long-running, scheduled, aggregating. Different watcher type, deferred.

### 15.10 Custom expression evaluation

Users writing arbitrary Python or DSL expressions for computed observables. The five built-in computed observables in the initial release cover the named use cases; custom expressions expand surface significantly.

### 15.11 GUI / dashboard

Not in the initial release, no plan for later releases. The CLI is the product. If we ever build a dashboard it's a separate Blockmachine product, not part of chainwake.

### 15.12 Telemetry

Zero in the initial release. No opt-in, no opt-out. The product earns trust by being silent. Re-evaluated only if there's a clear user benefit.

---

## 16. Test and launch criteria

Explicit "done" criteria so the initial release ships when it's actually complete and not before.

### 16.1 Functional criteria

- All six primitives (threshold, delta, event, liveness, state, tx) are implemented and tested for the Bittensor backend
- All seven Bittensor resources (subnet, validator, neuron, account, network, event, tx) expose their declared observables and accept the documented flag combinations
- Both computed observables resolve correctly against live Bittensor mainnet
- All 11 friendly event types in the curated mapping resolve to the correct underlying Substrate events
- The current output contract validates against every payload emitted in the smoke tests
- All five exit codes are emitted in the documented circumstances
- The `--name` flag is accepted and reflected in the output payload

### 16.2 Adapter criteria

- Default exit-with-JSON works for all watcher types
- `stream` adapter works for all watcher types and emits NDJSON
- `file://` adapter writes correctly without buffering issues
- At least three apprise destinations are smoke-tested: Telegram, Discord webhook, generic JSON webhook
- Multiple `--out` flags receive the same payload simultaneously

### 16.3 MCP server criteria

- `pip install chainwake` installs the built-in MCP server and
  `chainwake mcp serve --stdio` starts it
- All chainwake watchers are exposed as MCP tools with correct input schemas
- Stdio mode integrates with at least one real MCP client (Claude Desktop or equivalent) end-to-end
- Tool execution returns the chainwake exit JSON as the tool result

### 16.4 Distribution criteria

- `pip install chainwake` works on Python 3.13 across Linux, macOS, and Windows
- `uv tool install chainwake` works on the same platforms
- Shell completion installs cleanly on bash, zsh, and fish via `chainwake --install-completion`
- The package is published to PyPI under the `chainwake` name (assuming available; `chain-wake` as fallback)

### 16.5 Documentation criteria

- README has the tagline, install instructions, and three working examples within the first screen
- Every resource has a one-page docs entry covering its observables, applicable primitives, and example commands
- The output schema is published as a JSON schema document at `schemas/output.json`
- The full apprise URI reference is linked from the adapters documentation
- A "first watcher in 60 seconds" quickstart is on the front page
- An "agent integration" section covers the subprocess contract, exit codes, and the JSON payload structure with concrete examples

### 16.6 Performance criteria

Loose because performance is not the differentiator:

- A single watcher consumes <100MB RSS at steady state
- Cold start (process launch to first RPC call) is under 2 seconds on typical hardware
- WebSocket reconnect after network interruption completes within 5 seconds at the next attempt

### 16.7 Launch criteria

In addition to the functional criteria above:

- A blog post on blockmachine.io introducing chainwake and the agent-observation thesis
- A launch tweet/thread with at least one demo GIF of an agent loop using chainwake
- Outreach to Const (agcli) and the Bittensor Discord with a "what is this" framing
- Outreach to at least 3 Bittensor agent-builders in private to get early feedback before public

---

## 17. Distribution and release

### 17.1 Repo

Public repository at `github.com/taostat/chainwake`, branded and maintained as
a Blockmachine product. MIT-licensed.

### 17.2 PyPI

One package, `chainwake`, contains the CLI, library, and built-in MCP server.
The distribution has one version and one release workflow.

### 17.3 Versioning

Semantic versioning. `1.0.0` at first public release. Patch bumps cover bug
fixes, minor bumps cover additive features, and major bumps cover incompatible
package or CLI changes. Output changes replace the sole current
`schemas/output.json` contract atomically.

### 17.4 Release cadence

the initial public release ships when §16 criteria are met. After that, fortnightly minor releases as features land, ad-hoc patches as bugs surface.

### 17.5 Documentation site

`chainwake.dev` (assuming domain available) hosts documentation, the live registry, the JSON schema, and the apprise URI reference. Built with mdBook or VitePress; hosted on GitHub Pages or Vercel.

### 17.6 Support and feedback

- Issues on GitHub
- A `#chainwake` channel in the Bittensor Discord
- Direct outreach to early users for the first month

---

## 18. Open questions

Genuine open questions that don't block implementation but should be resolved before launch:

1. **PyPI name availability.** `chainwake` may already be reserved. If unavailable, fallback options in order of preference: `chain-wake`, `chainwake-cli`, `wake-chain`. Worth claiming on PyPI as soon as the spec is approved.

2. **Domain.** `.com` is parked but not in use. `.dev`, `.io`, `.sh` are available. Recommend `.dev` for the documentation site.

3. **Coordination with Const on agcli.** Worth a direct conversation before launch — chainwake positions as the read-side counterpart to agcli, and an explicit endorsement (or at least non-objection) from Const would help the launch narrative. Risk: he might announce a competing watch feature on agcli, which would be fine but worth knowing about.

4. **Initial blog post tone.** Two options: (a) introduce chainwake as a Blockmachine product, (b) introduce it as standalone OSS infrastructure that Blockmachine built. Recommend (b) for brand reasons; needs sign-off.

5. **First non-Blockmachine endorsement.** Worth identifying 1-2 high-credibility Bittensor builders to use chainwake privately before public launch and provide a quote or RT. Not blocking but improves launch lift.

---

## Appendix A: full Bittensor resource and observable registry

This is the complete set of resources, sub-resources, and observables that ship in the initial Chainwake release. Each entry includes natural cadence and applicable primitives.

### Resource: `subnet`

| Sub-resource / Observable | Type | Cadence | Primitives |
|---|---|---|---|
| `price` | numeric (TAO per alpha) | per_block | threshold, delta |
| `pool.tao-depth` | numeric (TAO) | per_block | threshold, delta |
| `pool.alpha-depth` | numeric (alpha) | per_block | threshold, delta |
| `pool.alpha-supply` | numeric (alpha) | per_block | threshold, delta |
| `pool.moving-price` | numeric (TAO per alpha) | per_block | threshold, delta |
| `pool.volume` | numeric (TAO, cumulative) | per_block | threshold, delta |
| `pool.depth-for-trade` (computed) | numeric (margin in bps) | per_block | threshold |
| `registration-cost` | numeric (TAO) | per_block | threshold |
| `emission-share` | numeric (fraction) | per_block | threshold, delta |
| `burn-rate` | numeric (fraction) | per_epoch | threshold, delta |
| `ema-tao-flow` | numeric (TAO, signed) | per_block | threshold, delta |
| `hyperparams` (includes factor-derived activity cutoff) | state-bytes | per_block | state |
| `identity` (full SubnetIdentitiesV3 + owners) | state-bytes | per_block | state |
`event --type subnet-registered` watches new-subnet registrations chain-wide.

### Resource: `validator`

| Sub-resource / Observable | Type | Cadence | Primitives |
|---|---|---|---|
| `dividends-alpha` (`--netuid`) | numeric (subnet alpha) | per_epoch | threshold, delta |
| `stake-alpha` (`--netuid`) | numeric (subnet alpha) | per_block | threshold, delta |
| `commission` | state | per_block | state |
| `weights` (`--netuid`, `--mechid`) | liveness anchor | per_epoch | liveness |
| `child-keys` | state-bytes | per_block | state |
| `identity` | state-bytes | per_block | state |

### Resource: `neuron`

| Sub-resource / Observable | Type | Cadence | Primitives |
|---|---|---|---|
| `incentive` (`--mechid`) | numeric | per_epoch | threshold, delta |
| `dividends` | numeric | per_epoch | threshold, delta |
| `stake-alpha` | numeric (subnet alpha) | per_block | threshold, delta |
| `last-update` (`--mechid`) | liveness anchor | per_block | liveness |
| `blocks-until-immunity-expires` (computed) | numeric (blocks) | per_block | threshold |

Spec-440 mechanism-indexed vectors use
`storage_index = mechid * 4096 + netuid`. CLI and MCP callers may select the
mechanism explicitly for incentive and last-update reads; validator weights also
accepts the subnet because mechanism ids are subnet-local. The compatibility
default is mechanism `0`. Non-zero ids are checked against
`MechanismCountCurrent` at the same pinned block.

### Resource: `account`

| Sub-resource / Observable | Type | Cadence | Primitives |
|---|---|---|---|
| `balance` | numeric (TAO) | per_block | threshold, delta, state |
| `activity` | liveness anchor | per_block | liveness |

### Resource: `network`

| Sub-resource / Observable | Type | Cadence | Primitives |
|---|---|---|---|
| `subnet-registration-cost` | numeric (TAO) | per_epoch | threshold |
| `runtime-version` | state | per_block | state |
| `subnet-count` | numeric (count) | per_block | threshold, delta |
| `--on-runtime-upgraded` | event | per_event | event |

### Resource: `event`

The chain-wide event firehose. Filters via `--type` and event-specific argument flags.

| Argument flag | Filter |
|---|---|
| `--type <name>` | Required; one of the friendly names in Appendix B |
| `--type-raw <Module.Event>` | Escape hatch for events outside the curated mapping |
| Event-specific flags | Per-event (e.g. `--from`, `--to`, `--amount-min`) |

### Resource: `tx`

| Argument | Description |
|---|---|
| ID (positional) | Transaction hash |
| `--finality <level>` | `included` or `finalized` |
| `--timeout <duration>` | Maximum wait |

---

## Appendix B: friendly event name mapping

Curated list of 11 friendly event names that ship in the initial release. Each maps to an underlying Substrate event verified against the runtime. The `--type-raw` escape hatch supports any Substrate event not in this list.

| Friendly name | Substrate event | Notes |
|---|---|---|
| `transfer` | `Balances.Transfer` | TAO transfers between coldkeys |
| `stake-added` | `SubtensorModule.StakeAdded` | |
| `stake-removed` | `SubtensorModule.StakeRemoved` | |
| `swap` | `SubtensorModule.StakeSwapped` | |
| `neuron-registered` | `SubtensorModule.NeuronRegistered` | |
| `subnet-registered` | `SubtensorModule.NetworkAdded` | |
| `weights-set` | `SubtensorModule.WeightsSet` | |
| `axon-served` | `SubtensorModule.AxonServed` | |
| `validator-permit-changed` | `SubtensorModule.MaxAllowedValidatorsSet` | |
| `child-keys-set` | `SubtensorModule.SetChildren` | |
| `identity-set` | `SubtensorModule.ChainIdentitySet` | |

The runtime does not expose a standalone neuron-deregistered event or a generic hyperparameter-changed event, so neither is advertised. The mapping is a maintenance commitment: runtime upgrades that change underlying event names require updating the registry. The `--type-raw` escape hatch ensures users are never blocked by missing or stale mappings — they can always specify a canonical event name exposed by the connected runtime.

---

## Appendix C: example commands by use case

A representative subset, drawn from the user-driven enumeration in §4. The complete set is in the documentation.

### Alpha holders

```bash
# Telegram alert when SN19 alpha price moves 10% in either direction in an hour
chainwake bt subnet 19 price --move-pct 10 --window-time 1h \
  --out tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID} \
  --out stream

# Email when SN64 pool depth drops below painful-exit level
chainwake bt subnet 64 pool tao-depth --below 5000 \
  --out mailto://${SMTP_USER}:${SMTP_PASS}@smtp.example.com
```

### Subnet owners

```bash
# Discord notification when a hyperparameter on my subnet changes
chainwake bt subnet 19 hyperparams --on-change \
  --out discord://${DISCORD_WEBHOOK_ID}/${DISCORD_WEBHOOK_TOKEN} \
  --out stream

# Alert when a validator with >1000 TAO stake starts validating my subnet
chainwake bt event --type stake-added --subnet 19 --amount-min 1000 \
  --out tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID} \
  --out stream
```

### Miners

```bash
# Wake when registration cost on SN64 drops below my budget
chainwake bt subnet 64 registration-cost --below 0.5

# Wake when my own neuron has gone stale on mechanism 1
chainwake bt neuron 19 5Fxxx last-update --mechid 1 --silent-for 10blocks
```

### Validators

```bash
# Wake if I miss a weight-set window on SN19 mechanism 1
chainwake bt validator 5Fyyy weights --netuid 19 --mechid 1 --silent-for 1epoch

# Alert when a competitor validator changes their commission
chainwake bt validator 5Fzzz commission --on-change \
  --out tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}
```

### Stakers

```bash
# Email alert when my staked validator goes silent for 3 epochs
chainwake bt validator 5Fyyy weights --silent-for 3epochs \
  --out mailto://${SMTP_USER}:${SMTP_PASS}@smtp.example.com
```

### Analysts

```bash
# Stream all new subnet registrations as JSON for downstream tooling
chainwake bt event --type subnet-registered --out stream

# Tweet about new subnet registrations (LLM in the middle)
chainwake bt event --type subnet-registered | \
  jq -r '.observed.netuid' | \
  while read netuid; do
    llm-tweet --about-subnet "$netuid"
  done
```

### Developers

```bash
# Wait for my submitted transaction to finalise
chainwake bt tx 0xabababababababababababababababababababababababababababababababab --finality finalized

# CI: wait for a contract storage value to update before running tests
chainwake bt account 5Fxxx balance --on-change --max-runtime 10m
```

### Agentic systems

```bash
# Agent suspends until SN19 price drops 5%, then makes a decision
result=$(chainwake bt subnet 19 price --drop-pct 5 --window-time 1h)
new_price=$(echo "$result" | jq -r '.observed.value')
agent-decide --about-price "$new_price"

# Multi-watcher fan-out via bash parallel
chainwake bt subnet 19 price --drop-pct 5 --window-time 1h &
chainwake bt subnet 64 price --drop-pct 5 --window-time 1h &
wait -n  # exits when first child fires
```

### MCP-driven (via the built-in server)

The MCP client sees tools like `chainwake_bt_subnet_price` with structured
input schemas. The agent invokes them as it would any MCP tool. Behind the
scenes, the MCP server built into `chainwake` executes the equivalent CLI.

---

## Appendix D: JSON output schema examples

### Match for a delta watcher

```json
{
  "status": "matched",
  "watcher": {
    "chain": "bt",
    "resource": "subnet",
    "resource_id": "19",
    "sub_resource": "price",
    "name": null,
    "primitive": "delta",
    "invocation": ["chainwake", "bt", "subnet", "19", "price", "--drop-pct", "5", "--window-time", "1h"]
  },
  "condition": {
    "operator": "drop-pct",
    "target": 5.0,
    "window": {"unit": "time", "value": "1h"}
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

### Match for an event watcher

```json
{
  "status": "matched",
  "watcher": {
    "chain": "bt",
    "resource": "event",
    "resource_id": null,
    "sub_resource": "subnet-registered",
    "name": "subnet-registration-watcher",
    "primitive": "event",
    "invocation": ["chainwake", "bt", "event", "--type", "subnet-registered", "--name", "subnet-registration-watcher"]
  },
  "condition": {
    "event_type": "subnet-registered",
    "filters": {}
  },
  "observed": {
    "event_type": "subnet-registered",
    "raw_event": "SubtensorModule.NetworkAdded",
    "args": {"netuid": 130},
    "block": 8119156,
    "block_hash": "0xdef...",
    "timestamp": "2026-05-05T19:48:32Z",
    "extrinsic_hash": "0x789..."
  },
  "budget": {
    "runtime_ms": 712450,
    "rpc_calls": 1,
    "estimated_ru_consumed": 1
  },
  "process": {
    "pid": 12891,
    "started_at": "2026-05-05T19:36:40Z"
  }
}
```

### Match for a tx watcher

```json
{
  "status": "matched",
  "watcher": {
    "chain": "bt",
    "resource": "tx",
    "resource_id": "0xabc...",
    "sub_resource": null,
    "name": null,
    "primitive": "tx",
    "invocation": ["chainwake", "bt", "tx", "0xabc...", "--finality", "finalized"]
  },
  "condition": {
    "tx_hash": "0xabababababababababababababababababababababababababababababababab",
    "finality_level": "finalized"
  },
  "observed": {
    "tx_hash": "0xabababababababababababababababababababababababababababababababab",
    "status": "finalized",
    "block": 8119145,
    "block_hash": "0xghi...",
    "timestamp": "2026-05-05T19:46:08Z",
    "events_emitted": ["Balances.Transfer", "SubtensorModule.StakeAdded"]
  },
  "budget": {
    "runtime_ms": 24315,
    "rpc_calls": 8,
    "estimated_ru_consumed": 8
  },
  "process": {
    "pid": 13042,
    "started_at": "2026-05-05T19:45:44Z"
  }
}
```

### Timeout (no match)

```json
{
  "status": "timeout",
  "reason": "max_runtime_reached",
  "watcher": {
    "chain": "bt",
    "resource": "subnet",
    "resource_id": "19",
    "sub_resource": "price",
    "name": null,
    "primitive": "threshold",
    "invocation": ["chainwake", "bt", "subnet", "19", "price", "--below", "0.01", "--max-runtime", "1h"]
  },
  "condition": {
    "operator": "below",
    "target": 0.01
  },
  "observed": null,
  "budget": {
    "runtime_ms": 3600000,
    "rpc_calls": 300,
    "estimated_ru_consumed": 300
  },
  "process": {
    "pid": 13201,
    "started_at": "2026-05-05T18:46:08Z"
  }
}
```

### Auth error

```json
{
  "status": "auth_error",
  "reason": "invalid_api_key",
  "watcher": {
    "chain": "bt",
    "resource": "subnet",
    "resource_id": "19",
    "sub_resource": "price",
    "name": null,
    "primitive": "threshold",
    "invocation": ["chainwake", "bt", "subnet", "19", "price", "--below", "0.01"]
  },
  "condition": {
    "operator": "below",
    "target": 0.01
  },
  "observed": null,
  "budget": {
    "runtime_ms": 1240,
    "rpc_calls": 1,
    "estimated_ru_consumed": 0
  },
  "process": {
    "pid": 13301,
    "started_at": "2026-05-05T19:50:00Z"
  }
}
```

---

**End of specification.**
