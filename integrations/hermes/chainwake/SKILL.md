---
name: chainwake
description: Sleep a Hermes agent until a Bittensor, Ethereum, Base, or BSC condition matches, then wake it with structured context. Uses Hermes background terminal completion notifications without agent polling.
version: 0.5.0
author: Blockmachine
license: MIT
platforms: [macos, linux, windows]
metadata:
  hermes:
    tags: [bittensor, ethereum, base, bsc, blockchain, monitoring, automation]
    category: automation
---

# Blockmachine Chainwake

Use Chainwake when the user wants Hermes to wait for a Bittensor or EVM condition and
continue only when it fires. Chainwake is observation-only: it never needs
wallet keys and never submits extrinsics.

## Install

Install the Chainwake repository from Hermes' Plugins page using:

```text
taostat/chainwake
```

Hermes clones the plugin to `~/.hermes/plugins/blockmachine-chainwake` and
registers this skill as `blockmachine-chainwake:chainwake`. Restart the gateway
after installation.

Run watchers from the plugin checkout with `uv`. Keep uv's cache inside the
writable Hermes state directory. Hermes deliberately sandboxes `$HOME`; use
`$HERMES_HOME` for plugin and cache paths:

```sh
UV_CACHE_DIR="$HERMES_HOME/cache/uv" \
  env -u VIRTUAL_ENV uv run --no-dev --frozen --project \
  "$HERMES_HOME/plugins/blockmachine-chainwake" \
  chainwake --version
```

This creates an isolated environment on first use. Do not ask the operator to
install a second copy of Chainwake.

The anonymous Blockmachine free tier is the default. Do not ask for an API key
during onboarding. If Chainwake returns `auth_error` or a provider rate limit,
ask the operator to sign up with Blockmachine and set
`CHAINWAKE_BT_API_KEY` in the Hermes environment. Never paste a key into a
prompt or command.

## Choose the trigger

Do not search the filesystem or inspect environment variables to find
Chainwake. The common price forms are:

```text
bt subnet <netuid> price --help
bt subnet <netuid> price --below <TAO>
bt subnet <netuid> price --above <TAO>
bt subnet <netuid> price --move-pct <percent>
bt subnet <netuid> price --drop-pct <percent>
bt subnet <netuid> price --rise-pct <percent>
bt network tao-price --below <usd>
bt network tao-price --above <usd>
bt network tao-price --move-pct <percent> --window-time <duration>
```

If the user gives a subnet but no trigger, ask one concise question for the
threshold or percentage movement. Do not start a monitor until that value is
known. Use the correctly ordered `bt subnet <netuid> price --help` command when
more syntax detail is useful. A movement trigger compares against the value
when the wake starts, so it does not need a window unless the user explicitly
requests one.

TAO/USD is a CoinGecko aggregate quote checked every 60 seconds. It accepts
time windows or the watcher-start baseline, not block or epoch windows.

For Ethereum base fee (in gwei), use:

```text
eth network base-fee --help
eth network base-fee --below <gwei>
eth network base-fee --above <gwei>
eth network base-fee --move-pct <percent>
eth tx <hash>
eth tx <hash> --confirmations <count>
eth tx <hash> --finality finalized
```

Ethereum uses the anonymous public endpoint by default and follows new blocks
through a WebSocket subscription. Do not ask for an Ethereum API key during
onboarding. A transaction wake returns the confirmation count, `success` or
`reverted`, gas used, and effective gas price.

Base and BSC use the same shape with chain-native finality and fee signals:

```text
base tx <hash> --finality safe
base tx <hash> --finality finalized
base network base-fee --above <gwei>
base network l1-base-fee --below <gwei>
base network l1-blob-base-fee --below <gwei>
bsc tx <hash> --confirmations <count>
bsc tx <hash> --finality finalized
bsc network gas-price --above <gwei>
```

Token-price wakes work on every EVM chain:

```text
eth token DAI price --below 0.995
base token DAI price --move-pct 1 --window-time 1h
bsc token GRAM price --above <usd>
```

Prices are CoinGecko aggregate USD quotes checked every 60 seconds. A symbol is
resolved only within the selected chain; if Chainwake reports ambiguity, rerun
with the exact contract address. Anonymous requests need no key. Only after a
CoinGecko rate-limit response should you ask the operator to set
`CHAINWAKE_COINGECKO_API_KEY` in the Hermes supervisor environment.

## Arm a wake

Run one Chainwake watcher with Hermes' `terminal` tool:

```python
terminal(
    command=(
        'UV_CACHE_DIR="$HERMES_HOME/cache/uv" '
        'env -u VIRTUAL_ENV uv run --no-dev --frozen --project '
        '"$HERMES_HOME/plugins/blockmachine-chainwake" '
        "chainwake --json bt subnet 19 price --above 0.10"
    ),
    background=True,
    notify_on_complete=True,
)
```

`background=True` lets the current turn finish.
`notify_on_complete=True` registers an automatic completion notification for
the originating Hermes session. When Chainwake exits, Hermes wakes that agent
with the process output. Hermes' foreground command timeout does not govern a
managed background process; use Chainwake's `--max-runtime` only when the user
asked for a monitoring deadline.

The current Hermes dashboard/TUI uses a process-wide completion queue. Keep
only one live dashboard/TUI session while waiting; with concurrent TUI sessions
Hermes can deliver the completion to the wrong one. Native messaging gateways
carry routing metadata and do not have this limitation.

Always pass `--json`, and do not add `--out`: a streaming or notification
adapter can keep Chainwake alive after a match.

**Do not poll** Hermes' `process` tool, do not create cron checks, and do not
keep an MCP request pending. Process completion is the wake signal. No model or
agent loop runs while Chainwake is waiting.

Completion notifications require a Hermes surface backed by its process
watcher. Native messaging gateways support this flow. If the active surface
does not support `notify_on_complete`, stop and explain that limitation; never
silently replace the wake with polling.

## Handle the completion notification

Read the JSON payload from the completion event:

- `watcher` says exactly what was monitored.
- `condition` says what had to happen.
- `observed` contains the matching value or event and block context.
- `watcher.invocation` preserves the command that armed the wake.

Then handle `status`:

- `matched`: explain what fired and continue the user's requested action.
- `timeout`, `budget_exhausted`, or `stopped`: report that no match occurred.
  Arm another wake only when the user's requested monitoring period remains
  active.
- `provider_error`: retry only with bounded exponential backoff.
- `auth_error`: request operator action; do not retry unchanged credentials.
- `user_error`: correct the command once.
- `internal_error`: stop and report the failure.

## Example prompt

> Use Chainwake to wake me when subnet 19's price rises above 0.10 TAO. Do not
> poll. When it fires, tell me the observed price and block.

Run:

```text
UV_CACHE_DIR="$HERMES_HOME/cache/uv" env -u VIRTUAL_ENV \
  uv run --no-dev --frozen \
  --project "$HERMES_HOME/plugins/blockmachine-chainwake" \
  chainwake --json bt subnet 19 price --above 0.10
```

## Durability boundary

**No durable re-entry:** the wait depends on the live Hermes process watcher.
If the client or gateway restarts, the process session and its automatic
continuation can be lost.

Persist the watcher independently when required:

```text
UV_CACHE_DIR="$HERMES_HOME/cache/uv" env -u VIRTUAL_ENV \
  uv run --no-dev --frozen \
  --project "$HERMES_HOME/plugins/blockmachine-chainwake" \
  chainwake --json --durable \
  --context "Tell me the observed price and block." \
  bt subnet 19 price --above 0.10
```

Then attach Hermes' background terminal completion to:

```text
UV_CACHE_DIR="$HERMES_HOME/cache/uv" env -u VIRTUAL_ENV \
  uv run --no-dev --frozen \
  --project "$HERMES_HOME/plugins/blockmachine-chainwake" \
  chainwake --json jobs wait <job-id>
```

The job and result survive a client or gateway restart and a replacement
`jobs wait` can reattach. Chainwake does not yet recreate the originating
Hermes session automatically.
