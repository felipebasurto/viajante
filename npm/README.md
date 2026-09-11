# viajante (npx)

npx entry for [viajante](https://pypi.org/project/viajante/). The searcher is Python; this package execs `uvx` against PyPI.

Requires **Python 3.10+** and [`uv`](https://docs.astral.sh/uv/) on PATH.

```bash
npx -y viajante airports JFK
```

MCP (stdio):

```json
{
  "mcpServers": {
    "viajante": {
      "command": "npx",
      "args": ["-y", "viajante-mcp"]
    }
  }
}
```

Native Python install stays `pip install viajante` / `uvx --from viajante[mcp] viajante-mcp`.
