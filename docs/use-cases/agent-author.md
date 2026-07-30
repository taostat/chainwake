# Agent author

Chainwake lets an agent stop thinking while it waits for a chain condition:

```text
agent starts a wake → agent turn ends → Chainwake watches →
condition matches → agent wakes with the result → agent reasons and acts
```

The host handles process completion. Chainwake handles the chain connection.
The LLM receives the complete result and understands what was watched, what
matched, the observed value, and the relevant block.

No polling loop or JSON parsing step belongs in the agent prompt.

## One wake needs one command

Tell your agent:

> Watch Bittensor subnet 19. Wake when its price rises above 0.10 TAO, then
> explain what happened.

The agent runs:

```sh
chainwake bt subnet 19 price --above 0.10 --json
```

When Chainwake exits, the host returns its output to the same agent. The agent
reads the result, answers the user, and starts another wake only if the user
asked it to keep monitoring.

Add `--max-runtime` when the request has a real deadline:

```sh
chainwake bt subnet 19 price --above 0.10 --max-runtime 12h --json
```

The limit belongs to the monitoring request, not to the agent or tool-call
timeout.

## Price move: investigate before acting

Tell your agent:

> Watch subnet 19 for a 5% price rise within one hour. When it happens, explain
> the move and decide whether it needs investigation. Do not trade without my
> approval.

```sh
chainwake bt subnet 19 price \
  --rise-pct 5 --window-time 1h --json
```

The wake result includes the previous value, current value, percentage move,
and chain context. The LLM can reason over those fields directly.

## New subnet: analyse and draft a post

Tell your agent:

> Watch for the next Bittensor subnet registration. When one appears, look up
> its identity, explain what registered, and draft a short post.

```sh
chainwake bt event --type subnet-registered --json
```

The event result includes the new netuid and registration block. After handling
it, the agent can run the same command again for the next registration.

Use `--out stream` for a conventional always-on data pipeline. An agent should
normally use the one-shot command above so process exit wakes it with one
complete event.

## Transaction finality: continue after confirmation

Tell your agent:

> Wait for this transaction to finalize. When it does, tell me whether it
> succeeded and continue with the next step.

```sh
chainwake bt tx 0x0123...abcd --finality finalized --max-runtime 5m --json
```

The same pattern works across the supported EVM chains:

```sh
chainwake eth tx 0x0123...abcd --finality finalized --json
chainwake base tx 0x0123...abcd --finality safe --json
chainwake bsc tx 0x0123...abcd --confirmations 12 --json
```

## Several possible wakes

If several independent conditions matter, the agent can start several
background wakes:

```sh
chainwake bt subnet 19 price --rise-pct 5 --window-time 1h --json
chainwake bt neuron 19 5Fxxx... last-update --silent-for 10blocks --json
chainwake bt event --type subnet-registered --json
```

Each process has one clear responsibility. Whichever exits returns its own
complete context. The host should notify the agent on process completion, not
ask the LLM to check process status repeatedly.

Cancel the remaining wakes only when the user's request no longer applies.

## Choosing how the agent waits

### Hermes and OpenClaw

Use their background-process completion notification. The agent starts
Chainwake, ends its turn, and is resumed with the output when the process exits.
Do not replace completion notification with process polling.

See [agent integration](../agent-integration.md) for the small host-specific
setup.

### MCP clients

The built-in MCP server exposes the same watchers as tools:

```sh
chainwake mcp serve --stdio
```

An MCP tool call remains open until the wake finishes. This is suitable when
the client can keep a request open for the full wait. For long waits, prefer a
background process so the agent turn can end.

See [MCP integration](../mcp.md) for installation and tool names.

### Durable jobs

Use a durable wake when the watcher must survive the creating shell or agent
gateway:

```sh
chainwake --json --durable \
  --context "Explain what matched and what I should do next." \
  bt subnet 19 price --above 0.10
```

The context is returned with the completed job. The host can attach a
completion wait with:

```sh
chainwake --json jobs wait <job-id>
```

Durability preserves the watcher and result. Re-entering the correct agent
conversation remains the host's responsibility.

## What the agent should do with a result

- `matched`: reason over the observed value and act within the user's authority.
- `timeout`: the requested monitoring period ended without a match.
- `budget_exhausted`: ask before increasing the monitoring budget.
- `provider_error`: retry later or use another endpoint; do not hot-loop.
- `auth_error`: fix access rather than retrying unchanged credentials.
- `user_error`: correct the wake command.
- `internal_error`: report the Chainwake failure.

The agent should never infer permission to trade, transfer funds, or publish
merely because a wake matched. Monitoring context informs the next decision;
it does not expand the agent's authority.

## Further reading

- [Agent integration](../agent-integration.md) — Hermes, OpenClaw, MCP, and
  durable completion
- [CLI reference](../cli-reference.md) — every available wake
- [Concepts](../concepts.md) — thresholds, changes, events, and observation
  cadence
- [Miner examples](miner.md) — miner-specific monitoring requests
