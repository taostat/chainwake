# Validator

You run a Bittensor validator. You care about your own validation health,
maintaining your delegator relationships, and monitoring competitor validators.
A missed weight-set window or a commission change by a competitor matters.

---

## Scenario 1: Wake if you miss a weight-set window

Your validator should be setting weights every epoch. If it misses one, you need
to know immediately.

```sh
chainwake bt validator 5Fyyyy... weights --silent-for 1epoch \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --max-runtime 7d
```

Replace `5Fyyyy...` with your validator hotkey. The liveness primitive fires
if no weight-set event from your hotkey is observed across one actual subnet
epoch. Chainwake follows the subnet's on-chain epoch index, so tempo changes,
re-anchoring, and owner-triggered epochs are included.

---

## Scenario 2: Delegator removes stake

A large delegator unstakes from you. You want to know so you can follow up.

```sh
chainwake bt event --type stake-removed \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --out stream \
  --max-runtime 7d
```

The `observed.args` block includes the delegator's coldkey, your hotkey, and the
amount removed. This is chain-wide — post-process with `jq` to filter for events
touching your hotkey:

```sh
chainwake bt event --type stake-removed --out stream --max-runtime 7d \
  | jq 'select(.observed.args.hotkey == "5Fyyyy...")'
```

---

## Scenario 3: Competitor changes commission

A competitor validator changes their commission rate. You want to know so you
can decide whether to adjust yours.

```sh
chainwake bt validator 5Fzzzz... commission --on-change \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --max-runtime 7d
```

Replace `5Fzzzz...` with the competitor hotkey. The state primitive fires on any
change to the commission value. The match payload includes both the old and new
values.

---

## Scenario 4: Dividend drop alert

Your own dividends dropped significantly — another signal that something is wrong
with your validation setup or that competition has increased.

```sh
chainwake bt validator 5Fyyyy... dividends-alpha --netuid 19 \
  --drop-pct 20 --window-epochs 3 \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --max-runtime 30d
```

Fires when the validator's SN19 alpha dividends drop 20% or more over 3
epochs. Dividends are scoped to one subnet because each subnet's alpha token is
a separate currency.

---

## Scenario 5: Child-key changes

Someone changed the child-key delegation on a validator you are tracking:

```sh
chainwake bt validator 5Fzzzz... child-keys --on-change \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --max-runtime 7d
```

---

## Multi-validator monitoring

Watch several validators at once by running watchers in parallel:

```sh
#!/bin/bash
set -euo pipefail

MY_HOTKEY=5Fyyyy...
COMPETITOR_A=5Faaaa...
COMPETITOR_B=5Fbbbb...

# My weights liveness
chainwake bt validator "${MY_HOTKEY}" weights --silent-for 1epoch \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" --max-runtime 30d &

# Competitor commission changes
chainwake bt validator "${COMPETITOR_A}" commission --on-change \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" --out stream --max-runtime 30d &

chainwake bt validator "${COMPETITOR_B}" commission --on-change \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" --out stream --max-runtime 30d &

# Stake removed from me
chainwake bt event --type stake-removed \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --out stream --max-runtime 30d &

wait
```

---

## Staker / delegator monitoring

If you are a staker rather than a validator operator, most of the same commands
apply — you are watching the validator you are staked to, not your own hotkey:

```sh
# My staked validator went silent for 3 epochs
chainwake bt validator 5Fyyyy... weights --silent-for 3epochs \
  --out "mailto://${SMTP_USER}:${SMTP_PASS}@smtp.example.com" \
  --max-runtime 30d

# Commission raised
chainwake bt validator 5Fyyyy... commission --on-change \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --max-runtime 30d
```

---

## Further reading

- [adapters.md](../adapters.md) — notification setup
- [cli-reference.md](../cli-reference.md) — validator, neuron, event flag reference
- [use-cases/subnet-operator.md](subnet-operator.md) — subnet-level monitoring
