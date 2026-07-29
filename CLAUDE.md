# Chainwake development guide

This file records repository-specific engineering constraints for contributors
and coding agents. Human-facing setup and contribution instructions live in
`CONTRIBUTING.md`.

## Architecture

Chainwake is an observation-only chain monitor:

```text
cli -> core/runtime -> providers -> RPC
              |
              +-> core/primitives and core/registry
              |
              +-> output adapters
```

- `chainwake/providers/` owns all chain RPC access.
- `chainwake/core/primitives/` evaluates observations without calling providers.
- `chainwake/core/registry.py` is the catalogue of supported observables.
- `chainwake/core/runtime.py` selects provider capabilities and drives watches.
- `chainwake/output/schema.py` owns every emitted watcher payload.
- `chainwake/cli/` translates user input into a `WatcherSpec`.

Keep chain-specific transport behavior in providers and shared evaluation
semantics in the core.

## Bittensor RPC and keys

Production Bittensor RPC uses `async-substrate-interface` directly. Do not
import the `bittensor` SDK in `chainwake/`, and do not introduce signing,
extrinsic submission, wallet loading, or keystore handling.

`bittensor-wallet` is a development dependency used only by the local-chain
integration harness with standard development accounts. Production code must
remain observation-only.

## Stable contracts

Changes to these modules affect every backend and require contract-test updates:

- `chainwake/providers/base.py`
- `chainwake/core/primitives/base.py`
- `chainwake/output/schema.py`
- `chainwake/output/adapters.py`

`schemas/output.json` is the sole watcher-output contract. Regenerate it from
the Pydantic models with:

```sh
uv run python scripts/generate_json_schema.py
```

Consumers are expected to validate payloads against the published schema and
reject unknown fields.

## Registry and observation policy

The registry is the source of truth for observable paths, natural cadence,
subscription support, compatible primitives, and estimated read cost. Adding
an observable requires registry tests and, where applicable, provider,
runtime, CLI, MCP, and documentation coverage.

Do not duplicate observable path strings or cadence decisions across command
implementations.

## Tests and checks

Use Python 3.13 or 3.14 and install the locked development environment:

```sh
uv sync --locked --dev
uv run prek run --all-files
uv run pytest -m unit
uv run pytest tests/contracts
```

Integration tests require Docker and start pinned local Subtensor and Anvil
nodes:

```sh
uv run pytest -m integration
```

Set `CHAINWAKE_REUSE_NODE=1` only when intentionally managing the integration
containers outside pytest.

Tests must be deterministic and must not depend on live public RPC services
unless marked `smoke`. Add a failing test before fixing a behavior defect.

## Release hygiene

- Keep dependencies and GitHub Actions pinned.
- Keep wheels and source distributions deterministic and minimal.
- Never commit credentials, wallet material, `.env` files, or notification
  URLs containing real tokens.
- Keep subprocess calls shell-free and pass explicit argument lists.
- Preserve least-privilege filesystem modes for durable job state.
- Update every version-bearing manifest together; the tag release workflow
  checks them with `scripts/check_release_version.py`.
