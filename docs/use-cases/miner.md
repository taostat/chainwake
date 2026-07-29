# Miner

You mine on one or more Bittensor subnets. You care about staying registered,
maintaining incentive rank, and knowing when registration costs drop into range.
Deregistration can happen fast and silently — chainwake exposes the current
chain signals without pretending the runtime has scheduled a pruning block.

---

## Scenario 1: Incentive-risk warning

Current Subtensor does not expose a pruning score or schedule a deterministic
deregistration block. Replacement is decided only when a full subnet receives a
registration, using relative emission, registration age, immunity, and owner
protections. Monitor your incentive as a truthful risk signal:

```sh
chainwake bt neuron 19 5Fxxx... incentive --below 0.01 \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --max-runtime 7d
```

Replace `19` with your subnet netuid and `5Fxxx...` with your hotkey. The
threshold is subnet-specific; choose it from the incentive distribution on the
subnet. Pair this with the `neuron-registered` event watcher below to observe
replacement pressure.

---

## Scenario 2: Node offline detection (last-update stale)

Your mining node submits `last_update` on every tick. If it stops, you want to
know within a few blocks.

```sh
chainwake bt neuron 19 5Fxxx... last-update --silent-for 10blocks \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --max-runtime 7d
```

The liveness primitive fires after 10 blocks with no `last_update` from your
neuron. Tune `--silent-for` to match how often your node actually submits.

---

## Scenario 3: Registration cost drops below budget

You want to mine on SN64 but registration costs are too high. Wake when the cost
drops below 0.5 TAO:

```sh
chainwake bt subnet 64 registration-cost --below 0.5 \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --max-runtime 7d
```

Registration burn can change on every block, so Chainwake samples this
watcher per block.

### Chain-wide registration cost

For the cost to register a new subnet (not a neuron slot), use the network resource:

```sh
chainwake bt network subnet-registration-cost --below 500 \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --max-runtime 30d
```

---

## Scenario 4: Incentive rank drop

Your incentive score dropped significantly — either competition increased or your
scoring degraded:

```sh
chainwake bt neuron 19 5Fxxx... incentive --drop-pct 30 --window-epochs 2 \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --max-runtime 30d
```

Fires when incentive drops 30% or more over 2 epochs.

---

## Scenario 5: New neuron registration on your subnet

Know when a new competitor enters your subnet:

```sh
chainwake bt event --type neuron-registered \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --out stream \
  --max-runtime 30d
```

The `observed.args` block will contain `netuid`, `uid`, and `hotkey` of the new
neuron. This is chain-wide — filter by netuid downstream if needed.

---

## Agent loop: wake on low incentive, act immediately

Tell the agent:

> Watch my SN19 incentive. If it drops below 0.01, explain the observed value
> and decide what I should inspect first.

It only needs to run:

```sh
chainwake bt neuron 19 5Fxxx... incentive \
  --below 0.01 --max-runtime 7d --json
```

No shell parser is needed. When Chainwake exits, the complete watcher,
condition, and observed value are returned to the agent for interpretation.

---

## Further reading

- [adapters.md](../adapters.md) — notification setup
- [cli-reference.md](../cli-reference.md) — neuron, subnet, network flag reference
- [agent-integration.md](../agent-integration.md) — await a wake from an agent
- [use-cases/agent-author.md](agent-author.md) — building a full agent loop
