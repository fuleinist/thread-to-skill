# MCP server

`t2s serve --mcp` exposes the local skill library to an agent host over MCP. The command reads the
library. It never writes to the workspace.

Use MCP when a host prefers explicit tools. Hooks stay the zero-config path, and this feature does
not replace them.

## Transport

The server speaks JSON-RPC 2.0 over stdio. Each message is one JSON object on one line, and the
server writes one response line per request. It writes no response for a notification.

The server reads the workspace from `--home`, or from the `T2S_HOME` environment variable, or from
`~/.thread2skill`. It opens no network listener and it starts no background process. It exits with
code 0 when the input closes.

The command takes no workspace lock, because every tool is read-only. A running server never blocks
`t2s ingest` or `t2s distill`.

## Tools

Four tools are available. Each one declares `readOnlyHint: true`.

- `search_skills(query, limit)` returns `slug`, `title`, `description`, `category`, `status`, and
  `score` for each match. Use it for a keyword lookup.
- `get_skill(slug)` returns `slug`, `metadata`, and `body`. The `body` value is the exact
  `SKILL.md` text read from disk, decoded as UTF-8. It is not the indexed copy, so an edit on disk
  shows up at once.
- `suggest_skills(prompt, limit, threshold)` returns the ranked matches with the reason each one
  matched. The default limit is 3 and the default threshold is 0.18.
- `list_skills(category, tag, status, limit)` returns full skill records. The status filter accepts
  `active`, `needs-review`, or `archived`. Archived skills stay hidden when you omit the status.

`limit` is clamped to the range 1 to 100. The server records no suggestion, so a call to
`suggest_skills` never changes the `suggested` counter and never writes a usage event.

## Errors

- An unknown tool name returns JSON-RPC `-32602` (invalid params).
- An unknown method returns JSON-RPC `-32601` (method not found).
- A line that is not JSON returns JSON-RPC `-32700` (parse error) with a null id.
- An unknown slug returns a normal tool result with `isError: true`. A protocol error would break
  the client loop, so a missing skill is a tool result instead.

## Claude Code

Register the server once for the project:

```bash
claude mcp add thread-to-skill -- t2s serve --mcp
```

Or add it to `.mcp.json` at the repository root:

```json
{
  "mcpServers": {
    "thread-to-skill": {
      "command": "t2s",
      "args": ["serve", "--mcp"]
    }
  }
}
```

Then ask the agent to call `suggest_skills` for the current task. Claude Code prints each tool call,
so the read-only surface stays visible.

## Cursor

Add the server to `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "thread-to-skill": {
      "command": "t2s",
      "args": ["serve", "--mcp"]
    }
  }
}
```

Reload the window after you change the file. Cursor shows the four tools in its MCP panel, and the
agent calls them only when the task needs a skill.

## OpenClaw

Add the server under the MCP servers key of the agent config, with the same `command` and `args`
pair. OpenClaw lists each tool after a restart.

## Check the server by hand

Send one request and read one response:

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | t2s serve --mcp
```

The response holds the four tool schemas. Press Ctrl-D to stop the server, or close the pipe. The
command exits with code 0.

## Limits

- No write tools. Ingest, distill, edit, archive, and install stay on the CLI.
- No HTTP, no SSE, and no authentication. The transport is stdio only.
- No new dependency. The server uses the standard library, so `pip install` is unchanged.