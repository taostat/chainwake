# Subnet operator

You own or operate a Bittensor subnet. You care about its health, economic
dynamics, and governance. You want to know immediately when anything changes —
a hyperparameter you did not set, a validator going silent, or a large new
delegator entering.

---

## Scenario 1: Hyperparameter change alert

Any hyperparameter change on your subnet should trigger an alert. You may not
have initiated it, and if someone else did you want to know immediately.

Chainwake watches a block-pinned snapshot of the subnet's supported
hyperparameters:

```sh
chainwake bt subnet 19 hyperparams --on-change \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --out stream \
  --max-runtime 30d
```

The match payload's `observed.previous_value` and `observed.value` contain the
previous and current supported snapshots. Diff them to identify which fields
changed. Use concrete raw runtime events when you need a chain-wide watcher
rather than one watcher per netuid.

---

## Scenario 2: Validator silence detection

A validator on your subnet has stopped setting weights. This affects the quality
of consensus and your subnet's health score. You want to know within a few epochs.

```sh
chainwake bt validator 5Fxxx... weights --silent-for 3epochs \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --max-runtime 7d
```

Replace `5Fxxx...` with the validator's hotkey. The liveness primitive checks
whether the validator has set weights within the last 3 epochs. If not, the
watcher fires.

---

## Scenario 3: New large stake entering your subnet

You want to know when a new delegator with significant stake starts validating
your subnet. Monitor the `stake-added` event with a minimum amount filter:

```sh
chainwake bt event --type stake-added \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --out stream \
  --max-runtime 7d
```

This fires on any stake-added event chain-wide. The `observed.args` block
contains the validator hotkey, coldkey, amount, and subnet. Filter downstream
in your notification handler if you only want events for your subnet.

---

## Scenario 4: Emission share change

Your subnet's share of the current block's total TAO emission changed
significantly:

```sh
chainwake bt subnet 19 emission-share --drop-pct 10 --window-blocks 300 \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --max-runtime 30d
```

---

## Scenario 5: New subnet registration (competitive intelligence)

You want to know when a competitor subnet registers so you can evaluate the
field. This also applies if you are watching for a specific netuid to become
available.

```sh
chainwake bt event --type subnet-registered --max-runtime 7d --json
```

The agent receives the full result when the command exits, including the new
`netuid` and registration block. It can look up the subnet identity, assess the
competitor, and decide how to respond without a separate parsing command.

---

## Multi-alert script

Run several watchers in parallel as a monitoring stack:

```sh
#!/bin/bash
set -euo pipefail

NETUID=19
VALIDATOR=5Fxxx...

# One concrete hyperparameter event. Current Subtensor has no generic
# "hyperparam changed" umbrella event; add one raw watcher per parameter.
chainwake bt event --type-raw SubtensorModule.WeightsSetRateLimitSet \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --out stream --max-runtime 30d &

# Validator silence
chainwake bt validator "${VALIDATOR}" weights --silent-for 3epochs \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --max-runtime 30d &

# New stake events
chainwake bt event --type stake-added \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --out stream --max-runtime 30d &

wait
```

---

## Further reading

- [adapters.md](../adapters.md) — notification setup
- [cli-reference.md](../cli-reference.md) — full flag reference for subnet,
  validator, and event resources
- [use-cases/validator.md](validator.md) — validator-side monitoring
- [use-cases/analyst.md](analyst.md) — chain-wide governance signals
