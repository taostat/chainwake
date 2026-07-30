---
name: chainwake
description: Sleep an OpenClaw agent until a Bittensor, Ethereum, Base, or BSC condition matches, then wake it with structured context. Uses OpenClaw's native background-process completion wake without agent polling.
metadata:
  openclaw:
    requires:
      bins:
        - chainwake
    install:
      - kind: uv
        package: chainwake==0.5.0
        bins:
          - chainwake
    envVars:
      - name: CHAINWAKE_BT_API_KEY
        required: false
        description: Optional Blockmachine key for higher Bittensor rate limits.
      - name: CHAINWAKE_ETH_API_KEY
        required: false
        description: Optional key for an authenticated Ethereum RPC endpoint.
      - name: CHAINWAKE_BASE_API_KEY
        required: false
        description: Optional key for an authenticated Base RPC endpoint.
      - name: CHAINWAKE_BSC_API_KEY
        required: false
        description: Optional key for an authenticated BSC RPC endpoint.
      - name: CHAINWAKE_COINGECKO_API_KEY
        required: false
        description: Optional free Demo key after a CoinGecko price rate limit.
    emoji: "⛓️"
    homepage: https://github.com/taostat/chainwake
---

# Blockmachine Chainwake

Use Chainwake when the user wants OpenClaw to wait for a Bittensor or EVM condition
and continue only when it fires. Chainwake is observation-only: it never needs
wallet keys and never submits extrinsics.

## Install

Install this skill from ClawHub. OpenClaw detects the declared `chainwake`
binary requirement and offers the `uv` PyPI installer automatically:

```sh
openclaw skills install @blockmachine/chainwake --global
```

For a manual CLI install, use `uv tool install chainwake`.
`pip install chainwake` is also supported. For a pre-release or source test, install
`git+https://github.com/taostat/chainwake.git` with uv instead.

The anonymous Blockmachine free tier is the default. Do not ask for an API key
during onboarding. If Chainwake returns `auth_error` or a provider rate limit,
ask the operator to sign up with Blockmachine and set `CHAINWAKE_BT_API_KEY` in
`~/.openclaw/.env`. Never paste a key into a prompt or command.

For TAO/USD, use:

```sh
chainwake --json bt network tao-price --below 180
chainwake --json bt network tao-price --move-pct 5 --window-time 1h
```

This is a CoinGecko aggregate quote checked every 60 seconds. It does not
support block or epoch windows.

For Ethereum, the common wake is:

```sh
chainwake --json eth network base-fee --below 10
chainwake --json eth tx <hash>
chainwake --json eth tx <hash> --confirmations 3
chainwake --json eth tx <hash> --finality finalized
```

The value is gwei and the default Ethereum endpoint is anonymous. Other
threshold and movement forms are discoverable with
`chainwake eth network base-fee --help`; transaction confidence options are
discoverable with `chainwake eth tx --help`.

Base and BSC use the same shape with chain-native finality and fees:

```sh
chainwake --json base tx <hash> --finality safe
chainwake --json base network l1-base-fee --below 20
chainwake --json base network l1-blob-base-fee --below 2
chainwake --json bsc tx <hash> --confirmations 12
chainwake --json bsc network gas-price --above 0.1
```

Token-price wakes work on every EVM chain:

```sh
chainwake --json eth token DAI price --below 0.995
chainwake --json base token DAI price --move-pct 1 --window-time 1h
chainwake --json bsc token GRAM price --above 0.01
```

Prices are CoinGecko aggregate USD quotes checked every 60 seconds. Resolve
symbols within the selected chain and use the exact contract address if
Chainwake reports an ambiguity. Do not ask for a CoinGecko key during
onboarding; if anonymous requests are rate limited, ask the operator to set
`CHAINWAKE_COINGECKO_API_KEY` in `~/.openclaw/.env`.

## Arm a wake

Run one Chainwake CLI watcher through OpenClaw's `exec` tool:

```json
{
  "command": "chainwake --json bt subnet 19 price --above 0.10",
  "background": true,
  "timeout": 0
}
```

The tool call returns `status: "running"` and the current agent turn may end.
OpenClaw keeps the process attached to the originating agent. When Chainwake
exits, OpenClaw's automatic completion wake starts a follow-up turn with the
process output.

`background: true` is required. `timeout: 0` disables OpenClaw's exec timeout;
use Chainwake's `--max-runtime` only when the user asked for a monitoring
deadline. Always pass `--json`, and do not add `--out`: a streaming or
notification adapter can keep Chainwake alive after a match.

OpenClaw normally enables `tools.exec.notifyOnExit`. If it is disabled, tell
the operator to enable it before arming the wake. Chainwake always writes a
JSON result on watcher exit, including clean no-match exits, so
`tools.exec.notifyOnExitEmptySuccess` is not required.

**Do not poll** the OpenClaw `process` tool, do not schedule heartbeat checks,
and do not keep an MCP request pending. Process exit is the wake signal. No
model or agent loop runs while Chainwake is waiting.

## Handle the completion wake

Read the JSON payload from the completion event:

- `watcher` says exactly what was monitored.
- `condition` says what had to happen.
- `observed` contains the matching value or event and block context.
- `watcher.invocation` preserves the command that armed the wake.

Then handle `status`:

- `matched`: explain what fired and continue the user's requested action.
- `timeout`, `budget_exhausted`, or `stopped`: report that no match occurred.
  Arm another wake only when the user's requested monitoring period remains
  active.
- `provider_error`: retry only with bounded exponential backoff.
- `auth_error`: request operator action; do not retry unchanged credentials.
- `user_error`: correct the command once.
- `internal_error`: stop and report the failure.

Do not silently replace an unavailable completion wake with polling. Tell the
user that the current OpenClaw runtime cannot provide the requested semantics.

## Example prompt

> Use Chainwake to wake me when subnet 19's price rises above 0.10 TAO. Do not
> poll. When it fires, tell me the observed price and block.

Run:

```text
chainwake --json bt subnet 19 price --above 0.10
```

## Durability boundary

**No durable re-entry:** OpenClaw keeps background exec sessions in memory. If
the client or gateway restarts, the process session and its automatic
continuation can be lost.

Persist the watcher independently when required:

```text
chainwake --json --durable \
  --context "Tell me the observed price and block." \
  bt subnet 19 price --above 0.10
```

Then run `chainwake --json jobs wait <job-id>` with `background: true` and
`timeout: 0`. OpenClaw still uses its normal process-exit wake, while the job
and result survive a client or gateway restart and a replacement `jobs wait`
can reattach. Chainwake does not yet recreate the originating OpenClaw session.

## MCP fallback

Prefer the background CLI flow above. If the operator deliberately installs
Chainwake as a blocking MCP server, use the full setup in
<https://github.com/taostat/chainwake/blob/main/docs/mcp.md>. Notable tools
include `chainwake_bt_network_runtime_version`,
`chainwake_bt_network_on_runtime_upgraded`, `chainwake_bt_network_tao_price`,
`chainwake_bt_event`, and `chainwake_eth_network_base_fee`, plus
`chainwake_<eth|base|bsc>_token_price`.

A blocking MCP wait also consumes an outer agent turn. Keep
`agents.defaults.timeoutSeconds` above the MCP request timeout; for the default
Chainwake configuration:

```sh
openclaw config set agents.defaults.timeoutSeconds 93600
```

See <https://docs.openclaw.ai/agent-loop>. After a rate-limit response, pass
the optional key from `~/.openclaw/.env` into that stdio server as
`"CHAINWAKE_BT_API_KEY": "${CHAINWAKE_BT_API_KEY}"`.
