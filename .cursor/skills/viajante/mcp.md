# Configure viajante MCP

Stdio only. No auth. No Streamable HTTP. One search at a time in the process.

## Contents

- This checkout (Cursor)
- Booking / detail (same process needs Chromium)
- No clone (`uvx` from git)
- Reload

## This checkout (Cursor)

Server: `src/viajante/mcp_server.py`. Script: `viajante-mcp`.

Committed config: `.cursor/mcp.json`

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

Then reload MCP in Cursor Settings. Sweep and Google Hotels need no Chromium.

## Booking / detail

The MCP process that will call Booking or `fetch=detail` must include the browser extra **and** Chromium in that same env. Installing `viajante[browser]` elsewhere does nothing.

Checkout:

```json
{
  "mcpServers": {
    "viajante": {
      "command": "uv",
      "args": ["run", "--extra", "mcp", "--extra", "browser", "viajante-mcp"]
    }
  }
}
```

```bash
uv sync --extra mcp --extra browser
uv run playwright install chromium
```

## No clone

Claude Desktop, Cursor user MCP, or any `mcpServers` client:

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

With Chromium (install into **this** uvx env):

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

`pip install viajante` is not on PyPI yet. Use git until `pypi.org/pypi/viajante/json` returns 200.
