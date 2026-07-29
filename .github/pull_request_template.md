## Summary

<!-- What changes, and what user or operator outcome does it enable? -->

## Validation

<!-- List the exact checks run and their results. -->

- [ ] `uv run prek run --all-files`
- [ ] `uv run pytest -m unit -n auto`
- [ ] Contract or integration tests were run when relevant

## Risk

- [ ] I considered reorg, duplicate-delivery, and provider-specific behavior where relevant.
- [ ] I added or updated tests for changed behavior.
- [ ] I updated public documentation or schemas where relevant.
- [ ] This change contains no credentials, private keys, tokens, or authenticated RPC URLs.

<!-- Note migrations, compatibility impact, rollback steps, or checks that were not run. -->
