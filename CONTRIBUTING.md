# Contributing to Chainwake

Thank you for contributing. Keep changes focused, tested, and explicit about
chain-specific behavior. Read the repository's
[development guide](CLAUDE.md) before changing architecture, provider
boundaries, stable contracts, or release behavior.

## Development setup

Install Python 3.13 or 3.14 and
[uv](https://docs.astral.sh/uv/), then run:

```shell
uv sync --locked --dev
uv run prek install
```

Run the same core checks used in CI:

```shell
uv run prek run --all-files
uv run pytest -m unit -n auto
uv run pytest -q tests/contracts
```

Integration tests require Docker. Run:

```shell
uv run pytest -m integration
```

If a failed run leaves services behind, clean up only the relevant Compose
project or use the matching files under `tests/integration/`.

## Changes and pull requests

- Open an issue first for large features, new chains, or public schema changes.
- Preserve the boundary between reusable primitives and chain/provider-specific
  behavior.
- Test reorg handling, duplicate delivery, finality, and provider variation
  when they can affect the change.
- Update documentation and schema contracts with user-visible behavior.
- Never commit private keys, tokens, secrets, or authenticated RPC URLs.

Pull requests should explain the user outcome, list the exact checks run, and
call out compatibility, migration, and rollback considerations.
