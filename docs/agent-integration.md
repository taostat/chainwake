# Agent integration

Chainwake gives an agent runtime a simple event-driven wait primitive:

```text
agent starts Chainwake → agent turn ends → condition matches →
Chainwake exits → host wakes the agent with context
```

Chainwake holds the chain subscription. The host watches the local process.
No model or agent loop runs while Chainwake is waiting.

## Run a wake

```sh
chainwake bt subnet 19 price \
  --below 0.05 \
  --max-runtime 4h \
  --json
```

When it finishes, stdout contains one JSON result explaining:

- what it watched in `watcher`;
- what condition it used in `condition`;
- what happened in `observed`;
- why it stopped in `status` and, where applicable, `reason`.

The original command is preserved in `watcher.invocation`.

## OpenClaw: automatic process-exit wake

Install Chainwake from OpenClaw's Skills UI or run:

```sh
openclaw skills install @blockmachine/chainwake --global
```

The skill declares its `chainwake` dependency, so OpenClaw offers to install
the version-matched PyPI package with `uv`.

Have OpenClaw call `exec` with:

```json
{
  "command": "chainwake --json bt subnet 19 price --below 0.05",
  "background": true,
  "timeout": 0
}
```

The current turn can finish after OpenClaw returns the background session ID.
Its default `tools.exec.notifyOnExit` behavior registers an automatic
completion wake. When Chainwake exits, OpenClaw requests a heartbeat and
starts a follow-up turn in the originating agent session with the process
output.

`timeout: 0` disables OpenClaw's process timeout. Add Chainwake's
`--max-runtime` only when the monitoring request itself has a deadline.
Chainwake always emits a non-empty JSON result on watcher exit, so the
host's empty-success notification option is not needed.

**Do not poll** OpenClaw's `process` tool and do not schedule heartbeat checks.
If automatic completion wake is unavailable, report that this runtime cannot
provide a Chainwake wake rather than silently replacing it with polling.

The installable OpenClaw skill is
[`integrations/openclaw/chainwake/SKILL.md`](../integrations/openclaw/chainwake/SKILL.md).

## Hermes: automatic completion notification

Open `http://<hermes-host>:9119/plugins`, enter `taostat/chainwake`, leave
**Enable after install** selected, install it, and restart the gateway. This
shows the plugin as `blockmachine-chainwake` and registers its skill as
`blockmachine-chainwake:chainwake`. A lightweight hook tells Hermes that
Blockmachine Chainwake is available, so ordinary prompts can discover it.

Have Hermes call `terminal` with:

```python
terminal(
    command=(
        'UV_CACHE_DIR="$HERMES_HOME/cache/uv" '
        'env -u VIRTUAL_ENV uv run --no-dev --frozen '
        '--project "$HERMES_HOME/plugins/blockmachine-chainwake" '
        "chainwake --json bt subnet 19 price --below 0.05"
    ),
    background=True,
    notify_on_complete=True,
)
```

The current turn can finish after Hermes returns the background session ID.
`notify_on_complete=True` registers an automatic completion notification for
the originating session. When Chainwake exits, Hermes starts a follow-up turn
with the process output. Hermes' foreground command timeout does not govern
the managed background process.

**Do not poll** Hermes' `process` tool and do not create a cron loop. The active
Hermes surface must support asynchronous process delivery; native messaging
gateways do. If Hermes returns `notify_unsupported`, report the limitation
instead of polling.

Hermes' current dashboard/TUI completion queue is process-wide. Keep one live
TUI session while a wake is armed; concurrent dashboard sessions can race for
the same completion. Native messaging gateways retain chat/thread routing.

The Hermes plugin manifest is at the repository root. Its bundled skill is
[`integrations/hermes/chainwake/SKILL.md`](../integrations/hermes/chainwake/SKILL.md).

## What the agent does next

Read `status` first:

| Status | Meaning | Typical next step |
|---|---|---|
| `matched` | The condition fired | Reason over `observed`, then act |
| `timeout` | The runtime limit was reached | Stop or begin another bounded wake |
| `budget_exhausted` | The RU limit was reached | Stop or raise the budget |
| `stopped` | The watcher was shut down | Stop or restart if still wanted |
| `provider_error` | The RPC failed | Back off or change endpoint |
| `auth_error` | RPC access needs attention | Fix access; do not retry unchanged credentials |
| `user_error` | The command is invalid | Correct the command |
| `internal_error` | Chainwake failed unexpectedly | Report the bug |

Exit codes provide the same broad control flow: `0` matched, `1` stopped
without a match, `2` bad input, `3` provider/access failure, and `4` internal
failure. The JSON contains the useful context.

## Minimal Python example

```python
import json
import subprocess

result = subprocess.run(
    [
        "chainwake", "bt", "subnet", "19", "price",
        "--below", "0.05",
        "--max-runtime", "4h",
        "--json",
    ],
    capture_output=True,
    text=True,
)

payload = json.loads(result.stdout)
if payload["status"] == "matched":
    decide_and_act(payload)
```

An async agent without a native completion wake can await the subprocess, then
inspect the returned payload. That keeps the agent turn alive, so OpenClaw and
Hermes should use their background-process integrations above.

## MCP agents

With the built-in MCP server, the agent calls a Chainwake tool instead of
launching the CLI itself. The tool call stays pending and returns the same
structured wake context when it finishes. This is useful only when the client
can keep the request and agent turn alive for the whole watch. Prefer the
native process-exit integrations above for OpenClaw and Hermes.

See [mcp.md](mcp.md) for Hermes, OpenClaw, Claude Desktop, and Cursor setup.

## Output contract

For watcher automation, always pass `--json`. A watcher invocation then writes
one JSON object to stdout. Human diagnostics go to stderr. Without `--json`,
output attached to an interactive TTY is human-readable; agents should not
depend on automatic TTY detection.

[`schemas/output.json`](../schemas/output.json) is the sole current output
contract. Consumers must validate payloads against it and reject unknown
fields. Output shape changes update the Pydantic models and this schema
atomically.

MCP configuration helpers, help/version output, and command parsing before a
watcher is selected are separate CLI surfaces; they are not watcher
invocations and do not use this JSON-envelope contract.

## Durability

The process-exit examples above remain the light-touch route. Their host
registration has **no durable re-entry** if the client or gateway restarts.

Persist the chain watcher itself with:

```sh
chainwake --json --durable \
  --context "Tell me what matched and include the observed block." \
  bt subnet 19 price --below 0.05
```

The command returns a job id immediately. Have the host use this as its
background completion process:

```sh
chainwake --json jobs wait <job-id>
```

`jobs wait` waits on local job state rather than polling the chain. If the host
restarts, the job remains visible through `jobs list` and a replacement
`jobs wait` can reattach. Chainwake persists the job and result, but does not
reconstruct the originating agent session; same-session re-entry remains the
host's responsibility.

The supervisor and isolated watcher normally continue across an agent-gateway
restart. After a machine reboot, run `chainwake daemon start` or `jobs wait`.
A stateful watcher whose worker was lost is re-armed from current chain state;
exact historical backfill across machine downtime is not yet guaranteed.
Durable mode does not persist literal `--api-key` or `--rpc-url` values; set
`CHAINWAKE_BT_API_KEY` or `CHAINWAKE_BT_RPC_URL` in the supervisor environment.

## More examples

- [quickstart.md](quickstart.md) — commands for common wakes
- [mcp.md](mcp.md) — use Chainwake as agent tools
- [use-cases/agent-author.md](use-cases/agent-author.md) — advanced orchestration
