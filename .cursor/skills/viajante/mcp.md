# Install viajante as an MCP server

Stdio process. No API keys. No Streamable HTTP. One search at a time.

A second search while one is running raises `a viajante search is already running in this process` immediately. That busy error is not `MCP error -32001: Request timed out`. Do not treat timeouts as lock-busy. `lookup_airports` may run during a search. Optional `country` is Google `gl` (origin market); omit when unset; do not pass a destination ISO. `max_stops` is 0, 1, or 2. If a calendar returns `blocked`, stop that request.

Paste this into Cursor Settings → MCP, Claude Desktop, or any `mcpServers` client. Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.10+. No clone.

## Sweep + Google Hotels (no Chromium)

npx (Node on PATH; still runs PyPI via `uvx`):

```json
{
  "mcpServers": {
    "viajante": {
      "command": "npx",
      "args": ["-y", "@viajante/mcp"]
    }
  }
}
```

Native Python:

```json
{
  "mcpServers": {
    "viajante": {
      "command": "uvx",
      "args": ["--from", "viajante[mcp]", "viajante-mcp"]
    }
  }
}
```

Reload MCP. Tools: `search_flights`, `search_dates`, `search_flex`, `search_explore`, `search_hotels` (Google), `search_trip`, `lookup_airports`, `search_hidden_city` (Skiplagged, opt-in), `compare_awards`, `lookup_transfers`.

After a new release, `uvx` / `npx` can cache an old tool list. Reload the MCP client, `uvx --refresh --from viajante[mcp] viajante-mcp`, `npx -y @viajante/mcp@latest`, or point the client at a local `viajante-mcp`.

## Booking.com or `fetch=detail`

Same MCP process needs the browser extra **and** Chromium in that uvx env. Another venv does not count.

```json
{
  "mcpServers": {
    "viajante": {
      "command": "uvx",
      "args": [
        "--from",
        "viajante[mcp,browser]",
        "viajante-mcp"
      ]
    }
  }
}
```

```bash
uvx --from 'viajante[mcp,browser]' playwright install chromium
```

## This checkout (contributors)

`.cursor/mcp.json` is only for developing this tree:

```json
{
  "mcpServers": {
    "viajante": {
      "command": "uv",
      "args": ["run", "--extra", "mcp", "viajante-mcp"]
    }
  }
}
```

```bash
uv sync --extra mcp
```
