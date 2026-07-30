# MCP integration

Blockmachine Chainwake's built-in MCP server exposes Bittensor, Ethereum, Base, and BSC watchers as
[MCP tools](https://modelcontextprotocol.io/) so any MCP-aware client (Claude
Desktop, Cursor, Hermes, OpenClaw, and custom harnesses) can invoke them
directly, without subprocess plumbing.

The EVM catalogues include:

- Ethereum: `chainwake_eth_network_base_fee`,
  `chainwake_eth_token_price`, `chainwake_eth_tx`
- Base: `chainwake_base_network_base_fee`,
  `chainwake_base_network_l1_base_fee`,
  `chainwake_base_network_l1_blob_base_fee`,
  `chainwake_base_token_price`, `chainwake_base_tx`
- BSC: `chainwake_bsc_network_gas_price`,
  `chainwake_bsc_token_price`, `chainwake_bsc_tx`

Transaction tools advertise only the finality levels supported by their chain
profile and return receipt execution context when they wake.
Token-price tools accept a chain-scoped symbol or contract address and return
the resolved token plus CoinGecko USD-price provenance.

Hermes and OpenClaw should normally use their native background-process
completion wakes instead. Those let the agent turn end while Chainwake waits,
then resume the originating session from process output without polling. See
[agent-integration.md](agent-integration.md). The MCP setup below remains
available for clients that deliberately keep a tool call and agent turn alive.

---

## Why use it

Without MCP, an agent that wants to watch a chain condition must construct and
execute a subprocess call, capture stdout, handle exit codes, and parse JSON.
With MCP, the agent calls a tool with structured arguments and receives the
chainwake JSON payload back as the tool result. The transport and marshalling are
handled by the MCP client.

Use Chainwake's MCP mode when:

- You are building prompts for Claude Desktop or Cursor and want chain watching
  as a native tool.
- Your agent framework supports MCP and you want to avoid subprocess management.
- You want to expose chainwake tools over HTTP to a fleet of agents.

Use the CLI directly (via subprocess) when:

- You are writing a shell script or Python agent that manages its own processes.
- You need fine-grained control over process lifecycle and output streaming.
- See [agent-integration.md](agent-integration.md) for those patterns.

---

## How a result reaches an agent

The normal integration is simple: the agent awaits a Chainwake tool or command,
and its agent runtime notifies it when the call completes. There are three
integration models:

### Blocking MCP continuation

The agent calls a Chainwake MCP tool and the same tool request remains pending.
When the condition matches or the watch reaches a limit, the tool returns a
JSON payload and the same live agent turn continues with that result. Nothing
starts a second agent session: the MCP client must keep the request and session
alive.

### Awaited CLI command

An agent launches the CLI with `--json` and awaits it. When the CLI exits, the
agent runtime reports that the command completed and the agent continues with
the JSON payload. A supervisor, scheduler, or job runner is needed only when
the watcher was deliberately detached from the agent that launched it.

### Notification-only adapters

Telegram, Discord, Slack, email, and other Apprise adapters notify their
configured destination. A notification-only adapter does not wake or resume an
agent and does not inject context into an agent session. A webhook can be an
input to a callback runner you operate, but Chainwake does not provide that
runner.

### Restart durability

**No durable re-entry:** an in-flight stdio tool call and its watcher are tied
to the live client/server processes. If the MCP client or gateway restarts, the
pending call is lost and Chainwake does not reconstruct the agent session.

For a persisted watcher, use the CLI job surface alongside MCP:

```sh
chainwake --json --durable \
  --context "Return this monitoring request with the match." \
  bt subnet 19 price --above 0.10
chainwake --json jobs wait <job-id>
```

The SQLite-backed job and result survive the MCP client or gateway restarting;
a replacement `jobs wait` can reattach. `--context` is returned with the
watcher payload. Durable creation is not yet an MCP tool, and Chainwake still
does not recreate the originating MCP request or agent session.

---

## Install

The MCP server is included in the stable PyPI package. Install it as an
isolated CLI:

```sh
uv tool install chainwake
```

Installing into the active Python environment is also supported:

```sh
pip install chainwake
```

For an unreleased source build, use
`uv tool install git+https://github.com/taostat/chainwake.git`.

---

## Server modes

### Stdio mode (for local MCP clients)

```sh
chainwake mcp serve --stdio
```

The server reads MCP messages from stdin and writes responses to stdout. Claude
Desktop and Cursor both use this transport. You configure the client to launch
this command as a child process.

### HTTP mode

```sh
chainwake mcp serve --port 8080
```

Starts an HTTP server on `127.0.0.1:8080` using the Streamable HTTP MCP
transport. The endpoint is `POST /mcp`. Chainwake does not currently implement
HTTP authentication, so it deliberately rejects non-loopback bind addresses.
Use stdio for local agent integrations; place a separately authenticated proxy
in front only when you have an explicit network deployment requirement.

HTTP tool calls cannot override `rpc_url`. Output adapters are CLI-only; MCP
tools always return the matched wake directly to the awaiting agent.

Every MCP wake is subject to four nested limits, from inner to outer:

1. The watcher's `max_runtime`.
2. Chainwake's server-side `--tool-timeout`.
3. The MCP client request timeout.
4. The enclosing agent or gateway run timeout.

Keep each finite limit shorter than the next one. The Chainwake server safety
timeout defaults to 24 hours. Generated Hermes and OpenClaw configs add one hour
of MCP client grace, so their default request timeout is 25 hours. The config
generator sets those middle two limits; it does not configure the enclosing
agent run because that is client-wide policy.

This ordering is not a ban on long waits. It ensures the watcher reaches its own
limit first and returns a structured `timeout` payload containing `watcher`,
`condition`, and budget context. If the outer server or client limit fires
first, the agent sees transport cancellation instead of a normal Chainwake
result. Increase all four limits for a longer wait while preserving that
ordering.

For a longer monitor, generate synchronized server and client settings in one
command:

```sh
chainwake mcp config hermes --tool-timeout 3d
chainwake mcp config openclaw --tool-timeout 3d
```

Those commands produce a 3-day server limit and a 73-hour MCP request limit.
They do not extend the agent turn. Set the enclosing limit above 73 hours as
described in the client-specific sections below.

`CHAINWAKE_MCP_TOOL_TIMEOUT` sets the same server option when launching
Chainwake directly.

---

## Hermes Agent integration

**Preferred:** install `taostat/chainwake` from Hermes' Plugins page and use
the native completion-notification flow in
[agent-integration.md](agent-integration.md). Use the MCP configuration below
only when a pending tool call is the desired lifecycle.

Hermes reads MCP servers from `~/.hermes/config.yaml`. Print a deterministic
copy-paste fragment:

```sh
chainwake mcp config hermes
```

The output is:

```yaml
mcp_servers:
  chainwake:
    command: chainwake
    args:
      - mcp
      - serve
      - --stdio
      - --tool-timeout
      - 24h
    timeout: 90000
```

Merge the `chainwake` entry into any existing `mcp_servers` mapping. Use
`--command /absolute/path/to/chainwake` if Hermes does not inherit the shell
`PATH` where Chainwake is installed. Save the file, then start a fresh
`hermes chat` session; Hermes discovers the tools at startup.

### Hermes long waits

Hermes' `timeout` field above is the per-tool MCP request limit, not the
enclosing agent limit. In gateway mode, `HERMES_AGENT_TIMEOUT` is the inactivity
limit for the whole running agent; its current default is 1800 seconds (30
minutes). For the generated 25-hour MCP limit, set a finite 26-hour enclosing
limit and restart the Hermes gateway:

```sh
hermes config set HERMES_AGENT_TIMEOUT 93600
```

For the 3-day example, use `270000` (75 hours), or set it to `0` only when an
unlimited gateway turn is intentional and another operational guard exists.
See Hermes' official
[environment-variable reference](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/environment-variables.md)
and
[MCP configuration reference](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/mcp-config-reference.md).

### Hermes rate-limit recovery

Start anonymously. If Blockmachine returns a rate-limit error, sign up for
higher limits and store the supplied key in Hermes' private dotenv file, not in
the generated fragment:

```dotenv
# ~/.hermes/.env
CHAINWAKE_BT_API_KEY=replace_with_your_key
```

Then add an explicit environment mapping to the existing Chainwake server
entry:

```yaml
mcp_servers:
  chainwake:
    # Keep the generated command, args, and timeout here.
    env:
      CHAINWAKE_BT_API_KEY: ${CHAINWAKE_BT_API_KEY}
```

Hermes filters inherited variables before starting a stdio MCP subprocess.
Variables explicitly named in the server's `env` mapping are passed through;
the placeholder is resolved from `~/.hermes/.env`. Restart Hermes after the
change. `chainwake mcp config hermes` intentionally continues to print an
anonymous, secret-free fragment.

Smoke prompt:

> Discover the Chainwake tools, then call `chainwake_bt_subnet_price` with
> `netuid` 1, `condition` `{"kind":"above","value":0}`, and `max_runtime`
> `60s`. Report `status`, `watcher`, `condition`, and `observed`; if it times
> out, say that `observed` is null.

Hermes prefixes discovered tool names with `mcp_chainwake_`. See the
[official Hermes MCP guide](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/mcp.md).

Once `chainwake` is released on PyPI and its pinned catalog entry is accepted,
users will be able to run:

```sh
hermes mcp install chainwake
```

That shortcut requires a PR to the Hermes repository adding
`optional-mcps/chainwake/manifest.yaml`; catalog presence means Nous has
reviewed and approved the entry. Until then, use the generated config above.

---

## OpenClaw integration

**Preferred:** install `@blockmachine/chainwake` from OpenClaw's Skills UI and
use the native process-exit wake in
[agent-integration.md](agent-integration.md). Use the MCP configuration below
only when a pending tool call is the desired lifecycle.

OpenClaw keeps gateway configuration in `~/.openclaw/openclaw.json` under
`mcp.servers`. Print valid JSON:

```sh
chainwake mcp config openclaw
```

The output is:

```json
{
  "mcp": {
    "servers": {
      "chainwake": {
        "command": "chainwake",
        "args": [
          "mcp",
          "serve",
          "--stdio",
          "--tool-timeout",
          "24h"
        ],
        "requestTimeoutMs": 90000000,
        "connectionTimeoutMs": 10000
      }
    }
  }
}
```

Merge the `chainwake` entry into any existing `mcp.servers` mapping. Use
`--command /absolute/path/to/chainwake` when the gateway needs an explicit
executable. OpenClaw hot-applies changes under `mcp.*`; the next tool discovery
or call starts the new server.

Use the same smoke prompt shown for Hermes. The generated defaults allow a
watcher `max_runtime` under 24 hours, enforce a 24-hour server safety timeout,
and give the OpenClaw client a 25-hour request timeout.

### OpenClaw long waits

OpenClaw's `requestTimeoutMs` is the per-server MCP request limit. The outer
limit for a normal agent turn is `agents.defaults.timeoutSeconds`; the current
default is 172800 seconds (48 hours), already above Chainwake's generated
25-hour client limit. To make that relationship explicit:

```sh
openclaw config set agents.defaults.timeoutSeconds 93600
```

For the 3-day example, use `270000` (75 hours). The value must remain greater
than the generated MCP request limit. See OpenClaw's official
[agent-loop timeout documentation](https://docs.openclaw.ai/agent-loop).

### OpenClaw rate-limit recovery

Start anonymously. After a Blockmachine rate-limit response, store the
higher-limit key in the gateway's global dotenv file:

```dotenv
# ~/.openclaw/.env
CHAINWAKE_BT_API_KEY=replace_with_your_key
```

Then add a placeholder to the existing generated server entry:

```json
{
  "mcp": {
    "servers": {
      "chainwake": {
        "env": {
          "CHAINWAKE_BT_API_KEY": "${CHAINWAKE_BT_API_KEY}"
        }
      }
    }
  }
}
```

Keep the generated `command`, `args`, and timeout fields alongside `env`; the
abbreviated object above shows only the addition. Reload the gateway. OpenClaw
allows ordinary custom `*_API_KEY` values in stdio server environments and
resolves `${VAR}` from its trusted global environment. See its official
[MCP client documentation](https://github.com/openclaw/openclaw/blob/main/docs/cli/mcp.md)
and
[environment reference](https://docs.openclaw.ai/help/environment).
`chainwake mcp config openclaw` intentionally remains anonymous and never
prints a key.

See also the
[official OpenClaw MCP configuration reference](https://github.com/openclaw/openclaw/blob/main/docs/gateway/configuration-reference.md#MCP).
OpenClaw has no separate MCP-only catalog. Prefer the Chainwake skill
installation at the start of this section when you want native process-exit
wakes; use the MCP configuration only when you specifically want a pending
tool call.

---

## Claude Desktop integration

Locate or create `~/Library/Application Support/Claude/claude_desktop_config.json`
on macOS (or the equivalent path on Windows/Linux).

Add a `mcpServers` entry:

```json
{
  "mcpServers": {
    "chainwake": {
      "command": "chainwake",
      "args": ["mcp", "serve", "--stdio"]
    }
  }
}
```

This zero-config setup uses `wss://rpc.blockmachine.io`. No API key is required
for the anonymous Blockmachine free tier. Start with no credentials. If a call
returns a rate-limit error, sign up at [Blockmachine](https://blockmachine.io/)
for higher limits and then add an `env` object containing
`"CHAINWAKE_BT_API_KEY": "your_api_key_here"`. You can also set
`CHAINWAKE_BT_RPC_URL` there when using a different endpoint.

After saving, restart Claude Desktop. You should see chainwake tools in the
tool picker.

---

## Cursor integration

Open Cursor settings and navigate to the MCP section. Add a server with:

```json
{
  "chainwake": {
    "command": "chainwake",
    "args": ["mcp", "serve", "--stdio"]
  }
}
```

The same anonymous Blockmachine default applies. Only add
`CHAINWAKE_BT_API_KEY` to an `env` object after a rate-limit response or when
using another authenticated endpoint.

The exact location in Cursor's settings UI varies by version. Refer to the
[Cursor MCP docs](https://docs.cursor.com/context/mcp) for current instructions.

---

## Tool catalogue

The MCP server generates one tool per wired CLI command. The tool's input schema
is derived directly from the Pydantic input model for that command.

Currently exposed tools:

- Subnet numeric wakes: `chainwake_bt_subnet_price`,
  `chainwake_bt_subnet_tao_depth`, `chainwake_bt_subnet_alpha_depth`,
  `chainwake_bt_subnet_depth_for_trade`, `chainwake_bt_subnet_alpha_supply`,
  `chainwake_bt_subnet_moving_price`, `chainwake_bt_subnet_volume`,
  `chainwake_bt_subnet_registration_cost`,
  `chainwake_bt_subnet_emission_share`, `chainwake_bt_subnet_burn_rate`, and
  `chainwake_bt_subnet_ema_tao_flow`.
- Subnet state wakes: `chainwake_bt_subnet_hyperparams` and
  `chainwake_bt_subnet_identity`.
- Validator wakes: `chainwake_bt_validator_dividends_alpha`,
  `chainwake_bt_validator_stake_alpha`, `chainwake_bt_validator_commission`,
  `chainwake_bt_validator_weights`, `chainwake_bt_validator_child_keys`, and
  `chainwake_bt_validator_identity`.
- Neuron wakes: `chainwake_bt_neuron_incentive`,
  `chainwake_bt_neuron_dividends`, `chainwake_bt_neuron_stake_alpha`,
  `chainwake_bt_neuron_last_update`, and
  `chainwake_bt_neuron_blocks_until_immunity_expires`.
- Account wakes: `chainwake_bt_account_balance` and
  `chainwake_bt_account_activity`.
- Network wakes: `chainwake_bt_network_subnet_registration_cost`,
  `chainwake_bt_network_tao_price`, `chainwake_bt_network_runtime_version`,
  `chainwake_bt_network_subnet_count`, and
  `chainwake_bt_network_on_runtime_upgraded`.
- Event and transaction wakes: `chainwake_bt_event` and `chainwake_bt_tx`.

The `burnrate` spelling alias does not create a duplicate MCP tool. Agents use
the canonical tool name above.

### `chainwake_bt_subnet_price`

Watch alpha price for a Bittensor subnet. Suspends until a threshold or
percentage-move condition fires.

Input schema:

| Field | Type | Required | Description |
|---|---|---|---|
| `netuid` | integer | yes | Subnet netuid |
| `condition` | object | yes | Discriminated threshold or percentage-move condition |
| `rpc_url` | string | no | Override RPC endpoint |
| `name` | string | no | Human-readable label |
| `max_runtime` | string | no | Hard limit; default is unbounded |
| `max_ru` | integer | no | Registry-estimated observation budget; not a provider billing cap |

Across all MCP tools, `max_ru` guards declared registry observation costs.
Connection bootstrap, transient retries, and RPCs hidden inside the SDK are
excluded from that estimate. Chainwake selects the storage, head, epoch, event,
or transaction observation driver from the registry; MCP callers do not need
to tune a polling interval.

The `condition.kind` is one of `below`, `above`, `drop-pct`, `rise-pct`, or
`move-pct`. Threshold conditions carry `value`. Delta conditions carry `pct`.
They may carry exactly one of `window_time`, `window_blocks`, or
`window_epochs` for rolling behavior. Omit all three to compare against the first successful observation since watcher start.

Example tool call (JSON):

```json
{
  "name": "chainwake_bt_subnet_price",
  "arguments": {
    "netuid": 19,
    "condition": {
      "kind": "drop-pct",
      "pct": 5.0,
      "window_time": "1h"
    },
    "max_runtime": "4m"
  }
}
```

### Mechanism-aware neuron and validator tools

Spec-440 mechanism-indexed watchers are exposed as:

- `chainwake_bt_neuron_incentive`
- `chainwake_bt_neuron_last_update`
- `chainwake_bt_validator_weights`

All three accept `mechid` as an integer from `0` through `15`, defaulting to
the main mechanism (`0`). Neuron tools also require `netuid` and `hotkey`.
Validator weights requires `hotkey` and accepts `netuid` (default `1`) because
mechanism ids are subnet-local.

Example:

```json
{
  "name": "chainwake_bt_neuron_incentive",
  "arguments": {
    "netuid": 19,
    "hotkey": "5Fxxx...",
    "mechid": 1,
    "condition": {
      "kind": "below",
      "value": 0.01
    },
    "max_runtime": "4m"
  }
}
```

The server checks non-zero mechanism ids against current chain state. It
returns a provider error for a nonexistent mechanism instead of treating the
missing vector as a zero incentive or stale update.

### Runtime monitoring

`chainwake_bt_network_runtime_version` watches the full Bittensor runtime
version and fires when it changes. `chainwake_bt_network_on_runtime_upgraded`
subscribes to the underlying `System.CodeUpdated` event. The version watcher is
usually the better choice when the agent needs the before/after version data;
the event watcher is useful when event timing itself matters.

```json
{
  "name": "chainwake_bt_network_runtime_version",
  "arguments": {
    "max_runtime": "4m",
    "max_ru": 25000
  }
}
```

Both tools also accept `rpc_url`, `name`, `max_runtime`, and `max_ru`.

### `chainwake_bt_tx`

Wait for a Bittensor transaction to reach a finality level.

| Field | Type | Required | Description |
|---|---|---|---|
| `tx_hash` | string | yes | Transaction hash (0x...) |
| `finality` | `"included"` or `"finalized"` | yes | Required finality level |
| `rpc_url` | string | no | Override RPC endpoint |
| `name` | string | no | Human-readable label |
| `max_runtime` | string | no | Hard limit; default is unbounded |
| `max_ru` | integer | no | Registry-estimated observation budget; not a provider billing cap |

Example tool call:

```json
{
  "name": "chainwake_bt_tx",
  "arguments": {
    "tx_hash": "0xabababababababababababababababababababababababababababababababab",
    "finality": "finalized",
    "max_runtime": "4m"
  }
}
```

Transaction lookup performs one bounded historical bootstrap scan, then scans
new blocks only. Included block metadata is cached while waiting for
finalization. The bootstrap scan is excluded from the `max_ru` registry
estimate.

### `chainwake_bt_event`

Wait for one event matching a curated friendly-name or raw canonical
`Module.Event` filter.

| Field | Type | Required | Description |
|---|---|---|---|
| `event_type` | string (one of 11 verified names) | no | Friendly event name |
| `type_raw` | string | no | Raw Substrate event (e.g. `"Balances.Transfer"`) |
| `from_addr` | string | no | Require an exact match on the event's decoded `from` field |
| `to_addr` | string | no | Require an exact match on the event's decoded `to` field |
| `amount_min` | non-negative integer | no | Require the decoded `amount` or `value` field to be at least this many rao |
| `direction` | `"in"` or `"out"` | no | Match direction relative to the paired `to_addr` or `from_addr` |
| `rpc_url` | string | no | Override RPC endpoint |
| `name` | string | no | Human-readable label |
| `max_runtime` | string | no | Hard limit; default is unbounded; recommended for unattended jobs |
| `max_ru` | integer | no | Registry-estimated observation budget; not a provider billing cap |

Exactly one of `event_type` or `type_raw` must be provided.
The structured filters apply only to events that expose the corresponding
decoded arguments. `direction: "in"` requires `to_addr` and compares the
decoded `to` field; `direction: "out"` requires `from_addr` and compares the
decoded `from` field. An event missing a field required by a filter does not
match.

Example tool call:

```json
{
  "name": "chainwake_bt_event",
  "arguments": {
    "event_type": "transfer",
    "to_addr": "5Bob...",
    "amount_min": 1000000000,
    "direction": "in",
    "max_runtime": "4m"
  }
}
```

### Subnet alpha stake and dividends

Three numeric tools expose the corrected, subnet-scoped alpha balances:

- `chainwake_bt_validator_dividends_alpha`
- `chainwake_bt_validator_stake_alpha`
- `chainwake_bt_neuron_stake_alpha`

Each requires `netuid`, `hotkey`, and a threshold-or-delta `condition`. Values
are denominated in the selected subnet's alpha token; they are not TAO, and
values from different subnets are never summed.

```json
{
  "name": "chainwake_bt_validator_stake_alpha",
  "arguments": {
    "netuid": 19,
    "hotkey": "5Fxxx...",
    "condition": {
      "kind": "below",
      "value": 1000
    },
    "max_runtime": "4m"
  }
}
```

---

## How tool execution works

When a tool is called:

1. The MCP server maps the tool name to the equivalent CLI command and arguments.
2. It executes `chainwake <args>` as a subprocess, inheriting the environment
   (including `CHAINWAKE_BT_RPC_URL` and `CHAINWAKE_BT_API_KEY`).
3. When chainwake exits, it captures stdout (the JSON payload) and returns it as
   the tool result text.
4. The exit code is not surfaced separately — the `status` field in the JSON
   payload carries the result.

All valid Chainwake payloads are returned as a normal tool result, including
user_error, provider_error, auth_error, and internal_error. This keeps the
complete `watcher`, `condition`, `observed`, `reason`, and `message` context
visible to the agent on both stdio and HTTP. A protocol error is reserved for
failures that prevent Chainwake from producing a valid payload, such as a
process spawn failure, invalid JSON, an unexpected exit code, or the server
safety timeout.

`out` is intentionally unavailable over MCP. A CLI output adapter such as
`stream`, `file://`, or a notification URI can keep a watcher alive after a
match, which would prevent the awaiting agent from resuming. Use those adapters
only with the standalone CLI.

On `matched`, the returned JSON preserves the complete wake context:
`watcher` identifies what was monitored, `watcher.invocation` is the executable
CLI equivalent, `condition` states why it fired, and `observed` contains the
matching chain data. A watcher-lifetime `timeout` is also a normal JSON result
with the same `watcher`, `watcher.invocation`, and `condition` context and a
null `observed`.

Tool calls suspend from the MCP client's perspective while Chainwake runs the
watcher asynchronously. The MCP server has a configurable safety timeout
(`--tool-timeout`, default 24 hours) and terminates the child process on that
limit or when the client cancels the call. Unlike a watcher-lifetime timeout,
this server safety limit is an MCP error because the child did not emit its
documented JSON context. Generated clients use 25 hours by default. Keep
`max_runtime` below the server limit, and the server limit below the client
timeout so Chainwake can return a structured result before an outer layer
cancels it. Longer waits are supported by increasing the three limits in that
order.

---

## Further reading

- [installation.md](installation.md) — install chainwake
- [agent-integration.md](agent-integration.md) — subprocess patterns without MCP
- [concepts.md](concepts.md) — what observables and primitives are available
