# Blockmachine Chainwake documentation

Blockmachine Chainwake provides Bittensor and EVM monitoring for AI agents. A
watcher CLI invocation suspends until a chain-state condition fires, then exits
with a structured JSON payload on stdout. It is observation-only: no keys, no
signing, no transaction submission.

The core primitive is **sleep your agent until chain state changes**. One
invocation watches one observable, evaluates one condition, and exits with a
JSON result the caller can parse. Agents, shell scripts, and notification
pipelines all consume the same interface.

---

## Who should read what

### I just want to install it and run an example

Read [installation.md](installation.md), then [quickstart.md](quickstart.md).

### I want to understand how chainwake works

Read [concepts.md](concepts.md). It covers the six primitive types, the
observable registry, the output schema, exit codes, and observation cadence.

### I need the full flag reference for a command

Read [cli-reference.md](cli-reference.md). One section per resource: subnet,
validator, neuron, account, network, event, tx.

### I want to send Telegram or Discord alerts

Read [adapters.md](adapters.md). Covers the `--out` flag, the apprise
integration, and step-by-step setup for Telegram, Discord, and Slack.

### I want to use chainwake with Hermes, OpenClaw, Claude Desktop, or Cursor

For Hermes and OpenClaw, read
[agent-integration.md](agent-integration.md) for native process-exit wakes
without polling. For Claude Desktop, Cursor, or another MCP client, read
[mcp.md](mcp.md).

### I want to use chainwake in an agent or script

Read [agent-integration.md](agent-integration.md). Covers the run, wait, wake,
and act lifecycle plus the returned status and context.

### I want examples for my specific role

| Role | Read |
|------|------|
| Alpha holder / retail speculator | [use-cases/alpha-holder.md](use-cases/alpha-holder.md) |
| Subnet owner / operator | [use-cases/subnet-operator.md](use-cases/subnet-operator.md) |
| Miner | [use-cases/miner.md](use-cases/miner.md) |
| Validator | [use-cases/validator.md](use-cases/validator.md) |
| Agent author | [use-cases/agent-author.md](use-cases/agent-author.md) |
| Analyst / content creator | [use-cases/analyst.md](use-cases/analyst.md) |

---

## Further reading

- [Historical design proposal](../spec.md) — archived rationale and decisions;
  use the guides above for current behavior
- [Repo README](../README.md) — elevator pitch and quickstart
- [JSON output schema](../schemas/output.json) — machine-readable contract
- [apprise URI catalogue](https://github.com/caronc/apprise/wiki) — all ~100
  notification destinations supported by the apprise adapter

---

## Contents

```
docs/
├── README.md               (this file)
├── installation.md         Install, shell completion, configuration
├── quickstart.md           Five working examples
├── concepts.md             Primitives, observables, schema, exit codes
├── cli-reference.md        Per-resource flag reference with examples
├── adapters.md             --out, apprise, Telegram/Discord/Slack setup
├── mcp.md                  Desktop MCP and tool reference
├── agent-integration.md    Run, wait, wake, and act from an agent
└── use-cases/
    ├── alpha-holder.md
    ├── subnet-operator.md
    ├── miner.md
    ├── validator.md
    ├── agent-author.md
    └── analyst.md
```
