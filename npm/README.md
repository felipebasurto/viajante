# @viajante/mcp (npx)

npx entry for [viajante](https://pypi.org/project/viajante/). The searcher is Python; this package execs `uvx` against PyPI.

Requires **Python 3.10+** and [`uv`](https://docs.astral.sh/uv/) on PATH.

MCP (stdio):

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

CLI:

```bash
npx -y -p @viajante/mcp viajante airports JFK
```

The `mcp` and `viajante-mcp` bins run `uvx --from viajante[mcp,browser]==<this package's version> viajante-mcp`. The `viajante` bin stays `viajante==<version>`.

Install Chromium once (Playwright's browser cache is shared per user): `uvx --from 'viajante[mcp,browser]==<version>' playwright install chromium`.

Native Python install stays `pip install viajante` / `uvx --from viajante[mcp] viajante-mcp`.
