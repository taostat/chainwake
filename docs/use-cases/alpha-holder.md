# Alpha holder

You hold alpha on one or more subnets and want price alerts on your phone. You
do not write code for a living. You want to know when to pay attention, not when
to stare at a chart.

---

## Set up Telegram once

Follow the three-step Telegram setup in [adapters.md](../adapters.md):

1. Create a bot via @BotFather, get your token.
2. Get your chat ID.
3. Set environment variables in your shell profile.

```sh
# Add to ~/.zshrc or ~/.bashrc
export TG_BOT_TOKEN=7123456789:AAFxxxxxxxxxxxxxxxx
export TG_CHAT_ID=123456789
```

All examples below use these variables.

---

## Scenario 1: Price drops below a target (threshold alert)

You want a Telegram ping when SN19 alpha drops below 0.05 TAO.

```sh
chainwake bt subnet 19 price --below 0.05 \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --max-runtime 24h
```

The watcher polls at the chain's natural per-block cadence. When the price
crosses below 0.05, it sends
a Telegram message containing the match payload (block number, price, timestamp)
and exits. If the price does not drop below 0.05 within 24 hours, it exits
silently.

To keep watching across multiple days, use `--out stream` to prevent the watcher
from stopping after the first notification, or use a process manager to relaunch
on exit:

```sh
while true; do
  chainwake bt subnet 19 price --below 0.05 \
    --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
    --max-runtime 24h
  sleep 5
done
```

---

## Scenario 2: Large price move in an hour (delta alert)

You want a Telegram ping when SN19 alpha moves 10% in either direction over the
past hour — both rallies and crashes.

```sh
chainwake bt subnet 19 price --move-pct 10 --window-time 1h \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --out stream \
  --max-runtime 7d
```

`--out stream` keeps the watcher alive so you get a notification every time the
condition fires again. Without it, the watcher would exit after the first alert.

---

## Scenario 3: Pool depth drops below a painful exit level

You want to know when you can no longer exit a position in SN64 without too
much slippage. Monitor the TAO reserve depth:

```sh
chainwake bt subnet 64 tao-depth --below 5000 \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --max-runtime 48h
```

This watches the actual TAO reserve in the subnet's dTAO pool. It does not use
price as a liquidity proxy.

---

## Scenario 4: Alert on a new subnet registration

You want to know any time a new subnet registers so you can evaluate early entry.

```sh
chainwake bt event --type subnet-registered \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --out stream \
  --max-runtime 7d
```

The match payload's `observed.args` block contains the new subnet's `netuid`.
Use `bt subnet <netuid> identity` when you also need its owner or metadata.

---

## Running watchers persistently

For persistent alerting, run chainwake under a process manager. On macOS with
launchd, or on Linux with systemd:

```sh
# Simple: run as a background job in your shell session
chainwake bt subnet 19 price --move-pct 10 --window-time 1h \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --out stream \
  --max-runtime 30d &
```

For server-side deployments, a systemd unit or Docker container is more
reliable. The `--out file://` adapter is useful for logging on a server:

```sh
chainwake bt subnet 19 price --move-pct 10 --window-time 1h \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --out "file:///var/log/chainwake/sn19-price.jsonl" \
  --out stream \
  --max-runtime 30d
```

---

## Further reading

- [adapters.md](../adapters.md) — Telegram setup, Discord, Slack, email
- [concepts.md](../concepts.md) — how threshold and delta primitives work
- [quickstart.md](../quickstart.md) — working examples to copy-paste
