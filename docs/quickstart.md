# Quickstart

All examples work against the default Blockmachine Bittensor RPC
(`wss://rpc.blockmachine.io`) with no configuration. No API key is required
for the anonymous free tier. Set `CHAINWAKE_BT_RPC_URL` only when you want to
use another endpoint.

TAO/USD watches also work without configuration:

```sh
chainwake bt network tao-price --below 180 --max-runtime 24h
chainwake bt network tao-price --move-pct 5 --window-time 1h --max-runtime 24h
```

This is CoinGecko's aggregate TAO/USD quote, checked every 60 seconds.

Ethereum base-fee watches also work without configuration:

```sh
chainwake eth network base-fee --below 10 --max-runtime 1h
```

The value is gwei. Chainwake subscribes to Ethereum `newHeads` and performs
exact hash-pinned reads rather than polling on a timer.

Base and BSC also have anonymous WebSocket defaults:

```sh
chainwake base tx 0x0123...abcd --finality safe --max-runtime 1h
chainwake base network l1-base-fee --below 20 --max-runtime 1h
chainwake base network l1-blob-base-fee --below 2 --max-runtime 1h
chainwake bsc tx 0x0123...abcd --confirmations 12 --max-runtime 5m
chainwake bsc network gas-price --above 0.1 --max-runtime 1h
```

Token prices use the same command on all three EVM chains:

```sh
chainwake eth token DAI price --below 0.995 --max-runtime 1h
chainwake base token DAI price --above 1.005 --max-runtime 1h
chainwake bsc token GRAM price --rise-pct 10 --window-time 1h --max-runtime 6h
```

These are CoinGecko aggregate USD prices, checked every 60 seconds. Symbols are
chain-scoped; pass a contract address when a symbol is unavailable or
ambiguous. Anonymous access needs no setup. If it is rate limited, create a
free CoinGecko Demo key and set `CHAINWAKE_COINGECKO_API_KEY`.

Watchers are unbounded unless you set `--max-runtime`. Always set a bounded
runtime for agent calls so the caller can distinguish a normal no-match timeout
from a stalled process.

---

## 1. Threshold — wait until subnet 19 price rises above 0.10 TAO

```sh
chainwake bt subnet 19 price --above 0.10 --max-runtime 5m
```

chainwake polls subnet 19's alpha price at its natural chain cadence (once per
block, about 12 seconds on Bittensor mainnet). When the price rises above 0.10
TAO it exits with code `0` and writes to stdout:

```json
{
  "status": "matched",
  "watcher": {
    "chain": "bt",
    "resource": "subnet",
    "resource_id": "19",
    "sub_resource": "pool.price",
    "name": null,
    "primitive": "threshold",
    "invocation": ["chainwake", "bt", "subnet", "19", "price", "--above", "0.10", "--max-runtime", "5m"]
  },
  "condition": { "operator": "above", "target": 0.10 },
  "observed": {
    "path": "subnet.19.pool.price",
    "value": 0.1042,
    "block": 4291820,
    "block_hash": "0xabc...",
    "timestamp": "2026-05-06T10:00:00Z"
  },
  "budget": { "runtime_ms": 12300, "rpc_calls": 12, "estimated_ru_consumed": 12 },
  "process": { "pid": 12345, "started_at": "2026-05-06T09:59:47Z" }
}
```

`estimated_ru_consumed` is declared registry observation work. It excludes
connection bootstrap, retries, and hidden SDK RPCs, so it is not a provider
billing total.

If the condition does not fire within 5 minutes, exit code is `1` with
`"status": "timeout"` and `"observed": null`.

---

## 2. Delta — wait until price rises 5% within a 1-hour window

```sh
chainwake bt subnet 19 price --rise-pct 5 --window-time 1h --max-runtime 2h
```

chainwake holds a rolling 1-hour price window. It fires when the oldest sample
in that window is more than 5% below the current price. The `--max-runtime`
guards against running indefinitely.

Use `--window-blocks 300` for a block-count window, or `--window-epochs 1` for
an epoch-aligned window. Or omit the window to compare every later successful
observation against the first one captured since watcher start:

```sh
chainwake bt subnet 28 burnrate --move-pct 1
```

That baseline does not expire. Use an explicit window when you want rolling
behavior instead.

---

## 3. Event — wait for a new subnet to be registered

```sh
chainwake bt event --type subnet-registered --max-runtime 24h
```

Subscribes to `SubtensorModule.NetworkAdded` events. Exits on the first match
with the new subnet's details in `observed.args`. If no subnet registers within
24 hours, exits with code `1`.

This watcher uses the event primitive — no condition flags are needed because
any subnet registration is a match.

---

## 4. Transaction finality — wait for your transaction to finalise

```sh
chainwake bt tx 0xabababababababababababababababababababababababababababababababab \
  --finality finalized --max-runtime 5m
```

Polls the RPC until the transaction with that hash reaches `finalized` status.
Use `--finality included` if you only need block inclusion (faster). Exits `0`
on success with the block number and hash in `observed`.

Chainwake performs one bounded historical scan, then checks only newly produced
blocks. If the transaction is already included, its block metadata is cached
while Chainwake waits for the finalized head to catch up.

This is useful after submitting a transaction with another tool (agcli, btcli,
a custom script) when you need to confirm the result before continuing.

---

## 5. Adapter — send a Telegram alert on price move

```sh
chainwake bt subnet 19 price --move-pct 10 --window-time 1h \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --out stream \
  --max-runtime 48h
```

This combines two adapters:

- `tgram://...` sends a Telegram notification when the price moves 10% in either
  direction within the past hour.
- `stream` keeps the watcher alive and emits one NDJSON line to stdout per
  match, so you can also pipe matches to a log or downstream process.

Without `--out stream` the watcher would exit after the first match. With it,
the watcher runs continuously and notifies on every subsequent 10% move.

See [adapters.md](adapters.md) for Telegram bot setup instructions and the full
list of supported notification destinations.

---

## 6. Ethereum — wait for a receipt or cheaper transaction inclusion

```sh
chainwake eth network base-fee --below 10 --max-runtime 1h
chainwake eth tx 0x0123...abcd --confirmations 3 --max-runtime 1h
```

This exits when the EIP-1559 block base fee is below 10 gwei. Use
`--move-pct 20` to compare later blocks with the first observed block, or add
`--window-time 1h` / `--window-blocks 300` for a rolling baseline.
The transaction form wakes after three canonical confirmations and reports
whether execution was `success` or `reverted`. Use `--finality finalized`
instead when protocol finality matters.

---

## 7. Base and BSC — use chain-native finality and fee signals

```sh
chainwake base tx 0x0123...abcd --finality safe --max-runtime 5m
chainwake base tx 0x0123...abcd --finality finalized --max-runtime 30m
chainwake base network base-fee --above 0.01 --max-runtime 1h
chainwake base network l1-base-fee --below 20 --max-runtime 1h
chainwake base network l1-blob-base-fee --below 2 --max-runtime 1h

chainwake bsc tx 0x0123...abcd --confirmations 12 --max-runtime 5m
chainwake bsc tx 0x0123...abcd --finality finalized --max-runtime 5m
chainwake bsc network gas-price --above 0.1 --max-runtime 1h
```

Base `safe` means the L2 block is derivable from canonical L1 data;
`finalized` means its L1 data is finalized. Base fees have separate L2
execution plus L1 base-fee and blob-base-fee inputs, so Chainwake exposes all
three signals.

BSC accepts confirmation depth for probabilistic confidence or `finalized`
for its fast-finality head. Its blocks report a zero base fee, so the useful
network fee signal is the suggested gas price.

---

## 8. EVM tokens — watch DAI or GRAM prices

```sh
chainwake eth token DAI price --below 0.995 --max-runtime 24h
chainwake base token DAI price --move-pct 1 --window-time 1h --max-runtime 24h
chainwake bsc token GRAM price --above 0.01 --max-runtime 24h
```

`DAI` resolves independently on Ethereum, Base, and BSC. `GRAM` currently
resolves on Ethereum and BSC. The match payload records the resolved contract,
symbol, decimals, quote timestamp, `quote_currency: usd`, and
`source: coingecko`. Use `token <contract-address> price` to pin an exact
contract or resolve an ambiguous symbol.

---

## Exit codes

| Code | Meaning |
|------|---------|
| `0`  | Condition fired. Parse stdout for the match payload. |
| `1`  | Stopped, timed out, or budget exhausted without a match. |
| `2`  | User error (bad flags, unknown resource). |
| `3`  | Provider or auth error (`provider_error` / `auth_error`). |
| `4`  | Internal error (bug). |

For watcher automation, pass `--json`; every watcher exit then writes a JSON
payload to stdout. An interactive TTY otherwise uses human-readable output.
See [concepts.md](concepts.md) for the full watcher schema.

---

## Next steps

- [concepts.md](concepts.md) — the six primitive types, observables, output schema
- [cli-reference.md](cli-reference.md) — complete flag reference per resource
- [adapters.md](adapters.md) — Telegram, Discord, Slack, file, stream
- [agent-integration.md](agent-integration.md) — await a wake from an agent
