# viajante for Hermes Agent

Flight and hotel search that runs on your machine. No API keys.

This package registers the `viajante` MCP server (`npx -y @viajante/mcp@1.5.5`, which runs the same-version Python package through `uvx`) and one skill that tells the agent how to read the results.

Needs [uv](https://docs.astral.sh/uv/) and Python 3.10 or later on the machine, plus Node.js for `npx`.

Without a plugin install, add the server directly:

```bash
hermes mcp add viajante --connect-timeout 300 --command npx --args -y @viajante/mcp
```

Source, docs and issues: <https://github.com/felipebasurto/viajante>. MIT.
