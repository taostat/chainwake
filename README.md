<p align="center">
  <img src="https://raw.githubusercontent.com/taostat/chainwake/main/docs/assets/blockmachine-logo.svg" alt="Blockmachine pulse block logo" width="112">
</p>

# Blockmachine Chainwake

<!-- mcp-name: io.github.taostat/chainwake -->

**Set a hook on chain state. Put your agent to sleep. Wake it the moment the
chain moves.**

Chain monitoring for AI agents across Bittensor, Ethereum, Base, and BNB Smart Chain.
Chainwake is a watcher CLI and MCP server. One invocation watches one condition —
a price threshold, a transaction reaching finality, an on-chain event — then
exits with a single structured JSON payload. It is observation-only: no keys,
no signing, no transaction submission.

![An agent sets a chainwake hook on subnet 19's price, sleeps at zero token cost, then wakes when the price breaks 0.01 TAO, fetches TAO's USD price, and sends a Telegram report](https://raw.githubusercontent.com/taostat/chainwake/main/docs/assets/demo.gif)

## Why

An agent that needs to react to chain state has two bad options today:

- **Poll in a loop.** Every empty check burns model tokens and context, and
  the agent still reacts late.
- **Stay out of it.** A human watches a dashboard and re-prompts the agent.

Chainwake is the third option. The agent starts a watcher and **ends its
turn**. No model runs while the watcher waits — the hook lives in a cheap
subprocess holding a WebSocket subscription. When the condition fires, the
watcher exits with JSON on stdout and the host wakes the agent with the
result. Tokens are spent only on the turns where something actually happened.

```
agent turn ──▶ chainwake watcher (no tokens) ──▶ condition fires ──▶ agent woken with JSON
```

## What you can watch

| Chain | Examples |
|-------|----------|
| `bt` (Bittensor) | TAO/USD and subnet prices, validator/neuron state, account balance and transfers, hyperparameter changes, chain events, epoch boundaries |
| `eth` (Ethereum) | Transaction receipt, confirmations, finality; EIP-1559 base fee; ERC-20 USD prices |
| `base` (Base) | Transaction finality (`safe`/`finalized`), L2 and L1 fee inputs; ERC-20 USD prices |
| `bsc` (BNB Smart Chain) | Transaction confirmations and finality, gas price, BEP-20 USD prices |

The full observable catalogue is in the
[CLI reference](https://github.com/taostat/chainwake/blob/main/docs/cli-reference.md).

## Install

```sh
uv tool install chainwake        # isolated install (recommended)
pip install chainwake            # or into the active environment
```

To try unreleased code: `uv tool install git+https://github.com/taostat/chainwake.git`.
Enable shell completion with `chainwake --install-completion`.

## Quickstart

Every command below blocks until its condition fires, then exits with one
JSON payload. All defaults are anonymous public endpoints — no configuration,
no API key needed for a first watcher.

Wait for a Bittensor subnet price to cross a threshold, or move 5% within an
hour:

```sh
chainwake bt subnet 19 price --above 0.10
chainwake bt subnet 19 price --rise-pct 5 --window-time 1h
```

Watch TAO/USD directly:

```sh
chainwake bt network tao-price --below 180
chainwake bt network tao-price --move-pct 5 --window-time 1h
```

TAO/USD is a CoinGecko aggregate quote sampled every 60 seconds. It supports
thresholds and percentage moves with a time window or watcher-start baseline.

The window controls which samples are compared; it does not impose a runtime limit.
Omit it to keep the first successful observation as an unexpiring baseline.
`--max-runtime` alone controls when an unmatched watcher exits with `timeout`.

Wait for an on-chain event:

```sh
chainwake bt event --type subnet-registered --max-runtime 24h
```

Wait for an Ethereum transaction, or for the EIP-1559 base fee (in gwei) to
get cheap:

```sh
chainwake eth tx 0x0123...abcd --finality finalized
chainwake eth network base-fee --below 10
```

Base and BSC use the same grammar with chain-specific finality and fees:

```sh
chainwake base tx 0x0123...abcd --finality safe
chainwake base network l1-blob-base-fee --below 2
chainwake bsc tx 0x0123...abcd --confirmations 12
```

Watch DAI by symbol on any supported EVM chain, or GRAM on Ethereum/BSC:

```sh
chainwake eth token DAI price --below 0.995
chainwake base token DAI price --above 1.005
chainwake bsc token GRAM price --rise-pct 10 --window-time 1h
```

Token symbols are resolved within the selected chain. If a symbol is ambiguous,
pass its contract address instead. Prices are CoinGecko aggregate USD quotes,
sampled every 60 seconds by default; set `CHAINWAKE_COINGECKO_API_KEY` only if
anonymous requests hit CoinGecko's rate limit.

Fee and transaction watchers subscribe to `newHeads` and pin reads to the
notified block.
Transaction watches report `success` or `reverted`, gas used, and effective
gas price; a missing receipt stays pending — Chainwake never guesses that a
transaction was dropped or replaced.

### The payload

A matched watcher exits `0` with stdout:

```json
{
  "status": "matched",
  "watcher": { "chain": "bt", "resource": "subnet", "resource_id": "19",
               "sub_resource": "pool.price", "name": null,
               "primitive": "threshold",
               "invocation": ["chainwake", "bt", "subnet", "19", "price",
                              "--above", "0.10"] },
  "condition": { "operator": "above", "target": 0.10 },
  "observed": { "path": "subnet.19.pool.price", "value": 0.1042,
                "block": 4291820, "block_hash": "0xabc...",
                "timestamp": "2026-05-06T10:00:00Z" },
  "budget": { "runtime_ms": 3210, "rpc_calls": 3, "estimated_ru_consumed": 3 },
  "process": { "pid": 12345, "started_at": "2026-05-06T09:59:57Z" }
}
```

The payload contract is
[`schemas/output.json`](https://github.com/taostat/chainwake/blob/main/schemas/output.json)
— the sole current output contract, regenerated from the Pydantic models and
CI-checked. Consumers should validate against it and reject unknown fields.

## Waking your agent

### MCP (Hermes, OpenClaw, Claude Desktop, Cursor)

The built-in MCP server exposes every watcher as an MCP tool:

```sh
chainwake mcp serve --stdio
chainwake mcp config hermes
chainwake mcp config openclaw
```

Hermes users install `taostat/chainwake` from the dashboard's Plugins page;
OpenClaw users run `openclaw skills install @blockmachine/chainwake --global`.
Both use native background-process completion notifications — no polling, no
long-held MCP request. See the
[MCP guide](https://github.com/taostat/chainwake/blob/main/docs/mcp.md).

### Process-exit wake

Tell your agent what to watch and what to do when it wakes:

> Watch subnet 19's price. If it rises above 0.10 TAO, explain what happened
> and tell me the observed block.

The agent launches one background command:

```sh
chainwake bt subnet 19 price --above 0.10 --max-runtime 5m --json
```

When it exits, the host resumes the agent with the complete result. The LLM
understands the watcher, condition, observed value, and block directly; no
shell parser or polling loop is needed.

See the
[agent integration guide](https://github.com/taostat/chainwake/blob/main/docs/agent-integration.md)
for parallel wakes, detached processes, and restart durability.

### Durable jobs

Add `--durable` when the watcher must outlive the shell or agent turn that
created it. Chainwake persists the watcher, starts a local supervisor, prints
the job id, and exits immediately:

```sh
chainwake --json --durable \
  --context "Tell me the observed price and block." \
  bt subnet 19 price --above 0.10

chainwake --json jobs wait <job-id>   # blocks without polling the chain
```

The completion carries your `context` plus the normal watcher result. Manage
jobs with `jobs list`, `jobs show`, `jobs cancel`. Because job arguments are
stored locally, durable mode rejects literal `--api-key`/`--rpc-url` values —
use `CHAINWAKE_BT_API_KEY` / `CHAINWAKE_BT_RPC_URL` in the supervisor
environment.

### Notifications (`--out`)

Route results anywhere apprise can deliver (~100 destinations: Telegram,
Discord, Slack, email, webhooks):

```sh
chainwake bt subnet 19 price --above 0.10 --out "tgram://bottoken/chatid"
chainwake bt subnet 19 price --above 0.10 --out stream            # NDJSON, keep running
chainwake bt subnet 19 price --above 0.10 --out file:///tmp/w.ndjson
```

`--out` is repeatable. See the
[apprise URI reference](https://github.com/caronc/apprise/wiki).

## Exit codes

Agents receive JSON for context; exit codes drive shell-level control flow.

| Code | `status` field | Meaning |
|------|----------------|---------|
| `0`  | `matched` | Condition fired |
| `1`  | `stopped` / `timeout` / `budget_exhausted` | Finished without a match |
| `2`  | `user_error` | Invalid args, unknown resource |
| `3`  | `provider_error` / `auth_error` | RPC unavailable or credentials required |
| `4`  | `internal_error` | Bug in chainwake |

For a watcher invocation, automation should always pass `--json`; every
watcher exit then emits one JSON envelope. Piped stdout also selects JSON, but
do not depend on TTY detection. Without `--json`, an interactive TTY uses
human-readable output. Help, version, and MCP configuration helpers are
outside the watcher-envelope contract. Stderr carries diagnostics and is not
part of the stable contract.

## Configuration

| Method | Precedence | Example |
|--------|-----------|---------|
| `--rpc-url` flag | Highest | `--rpc-url wss://my-node:9944` |
| Env var | Middle | `CHAINWAKE_BT_RPC_URL`, `CHAINWAKE_ETH_RPC_URL`, `CHAINWAKE_BASE_RPC_URL`, `CHAINWAKE_BSC_RPC_URL` |
| Anonymous default | Lowest | Blockmachine's free WebSocket endpoint for the selected chain |

[Blockmachine](https://blockmachine.io) is the default RPC provider for every
supported chain. Its anonymous free tier needs no API key:

| Chain | Default WebSocket endpoint |
|-------|----------------------------|
| Bittensor | `wss://rpc.blockmachine.io` |
| Ethereum | `wss://rpc-eth.blockmachine.io` |
| Base | `wss://rpc-base.blockmachine.io` |
| BSC | `wss://rpc-bsc.blockmachine.io` |

Any compatible WebSocket endpoint works. Pass `--api-key` (or the matching
`CHAINWAKE_<CHAIN>_API_KEY`) for higher-limit access.

`--max-runtime` accepts `30s`, `10m`, `2h`, `1d`. Default is unbounded.
Agent and automation calls should always set a bounded runtime.
`--max-ru` and `budget.estimated_ru_consumed` form a registry-estimated
observation budget, not a provider billing cap.

## Documentation

- [README](https://github.com/taostat/chainwake/blob/main/README.md)
- [Quickstart](https://github.com/taostat/chainwake/blob/main/docs/quickstart.md)
- [Concepts](https://github.com/taostat/chainwake/blob/main/docs/concepts.md) — primitives, registry, and observation cadence
- [CLI reference](https://github.com/taostat/chainwake/blob/main/docs/cli-reference.md)
- [Agent integration](https://github.com/taostat/chainwake/blob/main/docs/agent-integration.md)
- [MCP guide](https://github.com/taostat/chainwake/blob/main/docs/mcp.md)
- [Notification adapters](https://github.com/taostat/chainwake/blob/main/docs/adapters.md)
- [JSON output schema](https://github.com/taostat/chainwake/blob/main/schemas/output.json)
- [Changelog](https://github.com/taostat/chainwake/blob/main/CHANGELOG.md)
- [Historical design proposal (archived)](https://github.com/taostat/chainwake/blob/main/spec.md)

## Status

Chainwake is in active development. Report problems and propose features in
[GitHub Issues](https://github.com/taostat/chainwake/issues).

## License

MIT
