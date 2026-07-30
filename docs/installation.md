# Installation

## Requirements

chainwake supports Python 3.13 and 3.14. Its runtime talks to Bittensor through
`async-substrate-interface` and to Ethereum through standard WebSocket
JSON-RPC; installing either chain's full SDK is not required.

## Install chainwake

The recommended stable install uses uv and keeps Chainwake isolated from other
Python tools:

```sh
uv tool install chainwake
```

Or install into the active Python environment with pip:

```sh
pip install chainwake
```

### Pre-release or source install

To test unreleased changes directly from GitHub:

```sh
uv tool install git+https://github.com/taostat/chainwake.git
```

For a contributor checkout:

```sh
git clone https://github.com/taostat/chainwake
cd chainwake
uv sync --locked --dev
```

---

## Verify the install

Run a command that requires no network access and exits immediately if no
condition is given:

```sh
chainwake --version
```

You should see the version string. If you see a `command not found` error,
confirm that the install location is on your `PATH`. With `uv tool install`, the
tool lives in `~/.local/bin`; with `pip install`, it depends on your Python
environment.

---

## Shell completion

chainwake ships with context-aware shell completion. After installing:

```sh
chainwake --install-completion
```

This detects your shell (bash, zsh, fish, or PowerShell) and installs the
completion script. Restart your shell or source the updated config:

```sh
# bash
source ~/.bashrc

# zsh
source ~/.zshrc
```

After that, tab-completing `chainwake bt subnet 19 ` shows valid sub-resources;
`chainwake bt subnet 19 price ` shows applicable flags.

---

## Configuration

### RPC endpoint

chainwake needs a Substrate WebSocket endpoint to connect to. The default is
`wss://rpc.blockmachine.io` — Blockmachine's public Bittensor RPC.
No API key is required for the Blockmachine free tier, so the default install
is ready to use without configuration.

To use your own endpoint:

```sh
# Environment variable (per-session or in .env / shell profile)
export CHAINWAKE_BT_RPC_URL=wss://your-node:9944

# CLI flag (per-command override, highest precedence)
chainwake bt subnet 19 price --above 0.10 --rpc-url wss://your-node:9944
```

Precedence order (highest to lowest):

1. `--rpc-url` flag
2. `CHAINWAKE_BT_RPC_URL` environment variable
3. Default: `wss://rpc.blockmachine.io`

Ethereum follows the same precedence:

```sh
export CHAINWAKE_ETH_RPC_URL=wss://your-ethereum-node.example
chainwake eth network base-fee --below 10
chainwake eth tx 0x0123...abcd --confirmations 3
```

The anonymous Ethereum default is `wss://rpc-eth.blockmachine.io`.

Base and BSC follow the same pattern:

```sh
export CHAINWAKE_BASE_RPC_URL=wss://your-base-node.example
chainwake base tx 0x0123...abcd --finality safe

export CHAINWAKE_BSC_RPC_URL=wss://your-bsc-node.example
chainwake bsc network gas-price --above 0.1
```

Their anonymous defaults are `wss://rpc-base.blockmachine.io` and
`wss://rpc-bsc.blockmachine.io`.

### Optional API key

The default Blockmachine free tier is anonymous. Start without credentials. If
Chainwake returns a rate-limit error, or if you choose a different RPC endpoint
that requires authentication, configure an API key:

```sh
# Environment variable
export CHAINWAKE_BT_API_KEY=your_key_here

# Or global fallback
export CHAINWAKE_API_KEY=your_key_here

# CLI flag
chainwake bt subnet 19 price --above 0.10 --api-key your_key_here
```

Precedence order:

1. `--api-key` flag
2. `CHAINWAKE_BT_API_KEY` (per-chain)
3. `CHAINWAKE_API_KEY` (global fallback)

For Ethereum, Base, and BSC, use `CHAINWAKE_ETH_API_KEY`,
`CHAINWAKE_BASE_API_KEY`, or `CHAINWAKE_BSC_API_KEY`; the global fallback
remains `CHAINWAKE_API_KEY`.

TAO/USD and EVM token-price watches use CoinGecko separately from the chain
RPC. Anonymous
access is the default. If CoinGecko returns a rate limit, create a free Demo
key and set `CHAINWAKE_COINGECKO_API_KEY`; do not pass that key with
`--api-key`, which is reserved for the selected chain RPC.

If no API key is configured and a selected RPC endpoint requires one, chainwake
exits immediately with an `auth_error` payload describing the required operator
action. Do not retry unchanged credentials.

### Blockmachine RPC

Blockmachine is the default provider for Bittensor, Ethereum, Base, and BSC.
No API key is required for its anonymous free tier. If Chainwake returns a
rate-limit error, sign up at [blockmachine.io](https://blockmachine.io) and add
the issued key with `--api-key` or the matching
`CHAINWAKE_<CHAIN>_API_KEY`.

### No config file

chainwake has no config file in the current version. All configuration is via
environment variables and CLI flags. A `~/.chainwake/config.toml` is planned for
a future release.

---

## Next steps

- [quickstart.md](quickstart.md) — run your first watcher in under a minute
- [concepts.md](concepts.md) — understand primitives and the output schema
- [adapters.md](adapters.md) — send notifications to Telegram, Discord, Slack

## Contributor setup

Install the locked development environment and run the deterministic unit suite:

```sh
uv sync --locked --dev
uv run pytest -m unit -n auto
```

Integration tests use the official RaoFoundation spec-440 localnet and Foundry
Anvil images, pinned by release or commit and digest for reproducibility.
Docker starts only for an integration selection:

```sh
uv run pytest -m integration
```

`bittensor-wallet` is a development-only dependency used to sign transactions
against disposable localnet accounts. Chainwake production code remains
observation-only and never loads wallet keys.
