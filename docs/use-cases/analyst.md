# Analyst

You track Bittensor on-chain activity for content, research, or trading signals.
You want to know about governance changes, subnet registrations, and large
on-chain moves the moment they happen — not after scanning an explorer manually.

---

## Scenario 1: Wake on a new subnet registration

Every new subnet registration is a story. Ask your agent to watch for the next
one:

```sh
chainwake bt event --type subnet-registered --max-runtime 30d --json
```

When the command exits, the agent receives the netuid, block, and complete wake
context. It can then query the subnet identity and decide what matters. Start
the same wake again after handling the event when you want continuous coverage.

For a conventional data pipeline rather than an agent, stream events to a file:

```sh
chainwake bt event --type subnet-registered \
  --out stream \
  --out "file:///var/log/subnet-registrations.jsonl" \
  --max-runtime 30d
```

---

## Scenario 2: Alert when subnet registration cost crosses a round number

Registration cost crossing key thresholds (100, 500, 1000 TAO) is a governance
and sentiment signal. Get a Telegram ping when it drops below 500:

```sh
chainwake bt network subnet-registration-cost --below 500 \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --max-runtime 30d
```

---

## Scenario 3: Runtime upgrade notification

A Substrate runtime upgrade is significant news for the ecosystem. Know
immediately when it happens:

```sh
chainwake bt network runtime-version --on-change \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --out stream \
  --max-runtime 30d
```

---

## Scenario 4: Hyperparameter changes on a subnet

When a subnet changes a hyperparameter, it often signals governance activity or
an operator response to performance. Watch the supported hyperparameter snapshot:

```sh
chainwake bt subnet 19 hyperparams --on-change \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --out stream \
  --max-runtime 30d
```

The state watcher compares one block-pinned snapshot with the next.
`observed.previous_value` and `observed.value` contain the previous and current
snapshots, so downstream code can diff them to identify the changed fields.

Current Subtensor has no generic event that catches every hyperparameter
change across every subnet. To monitor the whole network rather than one
netuid, watch the concrete runtime event for each parameter of interest:

```sh
chainwake bt event --type-raw SubtensorModule.WeightsSetRateLimitSet \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --out stream \
  --max-runtime 30d
```

Use runtime metadata to discover other specific parameter events.

---

## Scenario 5: Large transfers between coldkeys

TAO transfers above a threshold are notable. Filter them at the watcher rather
than waking the agent for every transfer. `--amount-min` is denominated in rao,
so 10,000 TAO is 10,000,000,000,000 rao:

```sh
chainwake bt event --type transfer \
  --amount-min 10000000000000 \
  --max-runtime 30d \
  --json
```

The `observed.args` block includes `from`, `to`, and `amount`. The agent can
interpret those fields directly and decide whether to investigate.

---

## Scenario 6: Feed new subnet registrations to an LLM for commentary

Tell the agent:

> Watch for the next Bittensor subnet registration. When it happens, look up
> the subnet identity, explain what registered, and draft a short post.

The only wake command it needs is:

```sh
chainwake bt event --type subnet-registered --max-runtime 30d --json
```

The LLM receives the complete result when Chainwake exits; it can understand
the netuid and block without a shell loop or field-extraction step. After it
comments or posts, it starts the same wake again.

---

## Building a signal dashboard

For a persistent monitoring setup, combine multiple watchers with file adapters
and a log aggregator:

```sh
#!/bin/bash
set -euo pipefail

LOG=/var/log/chainwake

# Subnet registrations
chainwake bt event --type subnet-registered \
  --out "file://${LOG}/subnet-registrations.jsonl" \
  --out stream --max-runtime 30d &

# Transfer events
chainwake bt event --type transfer \
  --out "file://${LOG}/transfers.jsonl" \
  --out stream --max-runtime 30d &

# Stake changes
chainwake bt event --type stake-added \
  --out "file://${LOG}/stake-added.jsonl" \
  --out stream --max-runtime 30d &

chainwake bt event --type stake-removed \
  --out "file://${LOG}/stake-removed.jsonl" \
  --out stream --max-runtime 30d &

wait
```

Each NDJSON log file can be parsed by any log aggregation tool (Grafana Loki,
Elasticsearch, plain jq queries).

---

## Further reading

- [adapters.md](../adapters.md) — notification setup, multi-adapter fan-out
- [cli-reference.md](../cli-reference.md) — event resource, network resource
  flag reference
- [agent-integration.md](../agent-integration.md) — waking an agent with the
  complete result
- [concepts.md](../concepts.md) — curated event name mapping
