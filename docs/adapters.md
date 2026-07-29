# Adapters

chainwake's `--out` flag controls where match results go and whether the watcher
exits after the first match.

---

## The `--out` flag

`--out <uri>` is repeatable. Multiple adapters receive the same payload on every
match:

```sh
chainwake bt subnet 19 price --below 0.05 \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --out "file:///var/log/chainwake.jsonl" \
  --out stream
```

When no `--out` flag is given, the default adapter exits after one result. It
uses human output on an interactive TTY and JSON when piped. Agents should pass
`--json` explicitly; that JSON-mode behavior is the automation contract.

---

## Default adapter (no `--out`)

Writes the full JSON payload to stdout and exits. Exit code reflects the match
status. This is what agents and scripts parse.

```sh
result=$(chainwake bt subnet 19 price --below 0.05 --max-runtime 5m --json)
echo "$result" | jq '.observed.value'
```

The default adapter is the right choice whenever you need the result inline in a
script or agent loop. See [agent-integration.md](agent-integration.md) for
parsing patterns.

---

## `stream` adapter

Keeps the watcher alive and writes one NDJSON line to stdout per match. Does not
exit after a match.

```sh
chainwake bt event --type subnet-registered --out stream --max-runtime 7d
```

Each line is a compact JSON object (no pretty-printing). Pipe to `jq` for
filtering:

```sh
chainwake bt event --type subnet-registered --out stream --max-runtime 7d \
  | jq -r '.observed.args.netuid'
```

Use `--out stream` when you want continuous monitoring. Without it, the watcher
exits after the first match.

Combine with notification adapters to get both a persistent stream and alerts:

```sh
chainwake bt subnet 19 price --move-pct 10 --window-time 1h \
  --out stream \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --max-runtime 7d
```

---

## `file://` adapter

Appends NDJSON to a file. Does not exit after a match.

```sh
chainwake bt event --type subnet-registered \
  --out "file:///var/log/subnet-events.jsonl" \
  --max-runtime 7d
```

Path forms accepted:
- `file:///absolute/path` — absolute path
- `file://./relative/path` — relative to current directory

The parent directory is created if it does not exist. File is opened for append,
so existing contents are preserved. Flushed after every write.

---

## Apprise adapters

chainwake uses [apprise](https://github.com/caronc/apprise) for notification
destinations. Any URI the apprise library understands is accepted as an `--out`
value. This gives you access to ~100 notification services on day one.

When a notification adapter is the only `--out`, the watcher does not exit after
the first match — it keeps running and sends a notification for every subsequent
match. To exit after one match, use the default adapter (no `--out`) and handle
notifications in your own code, or combine `--out stream` with the notification
URI.

Failures from notification adapters are logged to stderr but do not crash the
watcher.

---

## Step-by-step: Telegram

**1. Create a bot**

Open [@BotFather](https://t.me/BotFather) on Telegram and send `/newbot`. Follow
the prompts. BotFather gives you a token like `7123456789:AAFxxxxxxxxxxxxxxxx`.

**2. Get your chat ID**

Send any message to your new bot, then open:

```
https://api.telegram.org/bot<TOKEN>/getUpdates
```

Look for `"chat": {"id": 123456789}`. That number is your chat ID.

For a group chat, add the bot to the group, send a message, and look for a
negative chat ID like `-987654321`.

**3. Build the URI**

```
tgram://7123456789:AAFxxxxxxxxxxxxxxxx/123456789
```

Format: `tgram://<bot_token>/<chat_id>`

**4. Test it**

```sh
TG_BOT_TOKEN=7123456789:AAFxxxxxxxxxxxxxxxx
TG_CHAT_ID=123456789

chainwake bt subnet 19 price --below 0.05 --max-runtime 5s \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}"
```

This will time out (5s is too short for the condition to fire), but you should
receive a Telegram message with the timeout payload. If not, recheck the token
and chat ID.

**5. Secure the token**

Never inline the token in scripts. Put it in your environment:

```sh
# ~/.zshrc or ~/.bashrc
export TG_BOT_TOKEN=7123456789:AAFxxxxxxxxxxxxxxxx
export TG_CHAT_ID=123456789
```

Then reference it in commands:

```sh
chainwake bt subnet 19 price --below 0.05 \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --max-runtime 4h
```

---

## Step-by-step: Discord

**1. Create a webhook in your channel**

Go to your Discord server, select the channel, open Settings > Integrations >
Webhooks > New Webhook. Copy the webhook URL — it looks like:

```
https://discord.com/api/webhooks/1234567890/xxxxxxxxxx-yyyyyyy
```

**2. Derive the apprise URI**

The apprise Discord form needs the webhook ID and token, not the full URL:

From the URL: `.../webhooks/<webhook_id>/<webhook_token>`

```
discord://1234567890/xxxxxxxxxx-yyyyyyy
```

**3. Use it**

```sh
DISCORD_WEBHOOK_ID=1234567890
DISCORD_WEBHOOK_TOKEN=xxxxxxxxxx-yyyyyyy

chainwake bt validator 5Fxxx... commission --on-change \
  --out "discord://${DISCORD_WEBHOOK_ID}/${DISCORD_WEBHOOK_TOKEN}" \
  --out stream \
  --max-runtime 7d
```

---

## Step-by-step: Slack

**1. Create a Slack app**

Go to [api.slack.com/apps](https://api.slack.com/apps), create a new app, add
the Incoming Webhooks feature, and activate it. Create a webhook for your target
channel.

**2. Get the webhook URL**

It looks like:

```
https://hooks.slack.com/services/<team_id>/<app_id>/<incoming_webhook_token>
```

**3. Derive the apprise URI**

```
slack://<team_id>/<app_id>/<incoming_webhook_token>
```

Format: `slack://<team_id>/<app_id>/<incoming_webhook_token>`

**4. Use it**

```sh
SLACK_TOKEN="<team_id>/<app_id>/<incoming_webhook_token>"

chainwake bt validator 5Fxxx... weights --silent-for 3epochs \
  --out "slack://${SLACK_TOKEN}" \
  --out stream \
  --max-runtime 7d
```

---

## Other destinations (apprise catalogue)

Any apprise-supported URI works. A non-exhaustive list:

| URI scheme | Destination |
|---|---|
| `tgram://token/chatid` | Telegram |
| `discord://webhook_id/webhook_token` | Discord |
| `slack://T/B/token` | Slack |
| `mailto://user:pass@host` | Email via SMTP |
| `mattermost://host/token` | Mattermost |
| `ntfy://host/topic` | ntfy.sh |
| `gotify://host/token` | Gotify |
| `pover://user@token` | Pushover |
| `json://host/path` | Generic JSON POST |
| `webhook://host/path` | Generic webhook |

The full catalogue is at [github.com/caronc/apprise/wiki](https://github.com/caronc/apprise/wiki).

---

## Multi-adapter fan-out

Fan out to multiple destinations with repeated `--out` flags. All adapters
receive the same payload simultaneously:

```sh
chainwake bt subnet 19 price --drop-pct 5 --window-time 1h \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}" \
  --out "file:///var/log/chainwake.jsonl" \
  --out stream \
  --max-runtime 24h
```

This sends a Telegram alert, appends to a log file, and emits NDJSON to stdout,
all on every match.

---

## Credential hygiene

Never put secrets (bot tokens, webhook URLs) directly in shell scripts that are
checked into version control. Use environment variables:

```sh
# .env file (gitignored)
TG_BOT_TOKEN=7123456789:AAFxxxxxxxxxxxxxxxx
TG_CHAT_ID=123456789
DISCORD_WEBHOOK_ID=1234567890
DISCORD_WEBHOOK_TOKEN=xxxxxxxxxx-yyyyyyy

# Load it
source .env

# Use it
chainwake bt subnet 19 price --below 0.05 \
  --out "tgram://${TG_BOT_TOKEN}/${TG_CHAT_ID}"
```

Apprise URIs containing credentials appear in the process list (`ps aux`). If
you are on a shared machine, consider using a secrets manager or environment
injection rather than inline URIs.

---

## Further reading

- [quickstart.md](quickstart.md) — quick working example with Telegram
- [agent-integration.md](agent-integration.md) — parsing JSON output in code
- [use-cases/alpha-holder.md](use-cases/alpha-holder.md) — notification workflow
  for price watchers
