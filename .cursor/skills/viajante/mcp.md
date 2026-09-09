# Install viajante as an MCP server

Stdio process. No API keys. No Streamable HTTP. One search at a time.

Paste this into Cursor Settings → MCP, Claude Desktop, or any `mcpServers` client. Requires [`uv`](https://docs.astral.sh/uv/). No clone.

## Sweep + Google Hotels (no Chromium)

```json
{
  "mcpServers": {
    "viajante": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/felipebasurto/viajante.git[mcp]",
        "viajante-mcp"
      ]
    }
  }
}
```

Reload MCP. Tools: `search_flights`, `search_dates`, `search_flex`, `search_explore`, `search_hotels` (Google), `search_trip`, `lookup_airports`.

PyPI is unpublished. Do not use `--from viajante[mcp]` until `pypi.org/pypi/viajante/json` returns 200.

## Booking.com or `fetch=detail`

Same MCP process needs the browser extra **and** Chromium in that uvx env. Another venv does not count.

```json
{
  "mcpServers": {
    "viajante": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/felipebasurto/viajante.git[mcp,browser]",
        "viajante-mcp"
      ]
    }
  }
}
```

```bash
uvx --from 'git+https://github.com/felipebasurto/viajante.git[mcp,browser]' playwright install chromium
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
