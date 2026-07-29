# Changelog

Chainwake follows semantic versioning. Release artifacts are published to PyPI
from signed-in GitHub Actions environments using trusted publishing.

## [Unreleased]

- Harden source-distribution contents and durable job isolation.
- Prepare repository governance, security, and contribution material for the
  public source release.

## [0.5.0] - 2026-07-29

- Added chain-profile-driven Ethereum, Base, and BNB Smart Chain support.
- Added transaction confirmation and finality watches for EVM chains.
- Added Ethereum and Base fee watches and BSC gas-price watches.
- Added chain-scoped ERC-20/BEP-20 symbol and contract-address price watches.
- Added CoinGecko-backed TAO/USD and token USD prices.

## [0.4.0] - 2026-07-29

- Added Ethereum transaction and EIP-1559 base-fee monitoring.
- Introduced shared backend boundaries for additional EVM chains.

## [0.3.0] - 2026-07-29

- Added persistent durable jobs and a local supervisor.
- Added native process-completion wake flows for Hermes and OpenClaw.

## [0.2.0] - 2026-07-28

- Added registry-owned observation policies.
- Added WebSocket head and storage subscriptions with reconnect handling.

## [0.1.0] - 2026-07-28

- First package release with the Bittensor watcher CLI, MCP server,
  structured output schema, notification adapters, and local-chain tests.

[Unreleased]: https://github.com/taostat/chainwake
[0.5.0]: https://pypi.org/project/chainwake/0.5.0/
[0.4.0]: https://pypi.org/project/chainwake/0.4.0/
[0.3.0]: https://pypi.org/project/chainwake/0.3.0/
[0.2.0]: https://pypi.org/project/chainwake/0.2.0/
[0.1.0]: https://pypi.org/project/chainwake/0.1.0/
