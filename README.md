# crm-mcp-server

A small Model Context Protocol (MCP) server that lets an AI agent such as Claude read and update sales leads in a CRM, safely.

I built it to show how I think about APIs when the caller is an agent rather than a person. An agent reads your tool descriptions literally, it retries when something times out, and it will happily guess an id if you let it. So this server is designed around those habits.

## Tools

| Tool | Type | What it does |
| --- | --- | --- |
| `search_leads` | read | Search by text and status, with cursor pagination |
| `get_lead` | read | One lead with all of its notes |
| `create_lead` | write | Create a lead with status `new` |
| `update_lead_status` | write | Move a lead through `new`, `contacted`, `qualified`, `won`, `lost` |
| `add_note` | write | Add a note, such as a call summary |

## Design decisions

- **Reads and writes are separate tools, with MCP annotations.** Read tools are marked read-only, so a client can let them run freely and ask a person before any write.
- **Every write needs an idempotency key.** If an agent retries a call with the same key, it gets the first result back instead of creating a duplicate. Reusing a key for a different operation is rejected, and a failed write does not use up its key.
- **Every write can be previewed.** Pass `dry_run: true` and the server validates the call and returns what it would save, but saves nothing. The write actually runs inside a transaction that is rolled back, so the preview cannot drift from the real thing. A dry run does not use up its idempotency key, so once a person approves, the agent sends the same call again without `dry_run`.
- **Errors tell the agent what to do next.** "No lead with id 'x'. Call search_leads to find the right id first." is more useful to a model than a stack trace.
- **Cursor pagination, not offset.** Pages stay stable while other writes happen.
- **Typed inputs and outputs with Pydantic.** Every tool returns structured content, so the agent gets the same shape every time.
- **An audit log of every write,** so a person can see exactly what an agent changed.

## Run it

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run crm-mcp-server --db crm.db
```

Over HTTP instead of stdio:

```bash
uv run crm-mcp-server --db crm.db --transport streamable-http
```

### Use it from Claude Desktop

Add this to `claude_desktop_config.json`, with the path changed to where you cloned the repo:

```json
{
  "mcpServers": {
    "crm": {
      "command": "uv",
      "args": ["--directory", "/path/to/crm-mcp-server", "run", "crm-mcp-server", "--db", "crm.db"]
    }
  }
}
```

Then ask Claude something like "Add Ada Lovelace from Engines Ltd as a lead and note that she wants a demo next week."

## Tests

```bash
uv run pytest
```

The tests cover the agent flow end to end (create, search, update, note), retries with the same idempotency key, dry runs, pagination, and the error messages.

## What I would add next

- OAuth on the HTTP transport, with tokens scoped to read-only or read-write
- Tracing of each tool call (for example in LangFuse) to see which tools agents misuse

## License

MIT
