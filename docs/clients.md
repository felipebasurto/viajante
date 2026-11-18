# MCP clients

One server, many clients. Every entry below starts the same process:

```
npx -y @viajante/mcp
```

Without Node: `uvx --from viajante[mcp] viajante-mcp`. Both need [uv](https://docs.astral.sh/uv/)
and Python 3.10 or later, except the Claude Desktop extension, which has the host install them.

The first start downloads viajante. A client with a short start timeout can fail on that one
run. Run `npx -y -p @viajante/mcp viajante --version` once in a terminal, then start the client.

Confirm the executing version with `get_runtime_info` before relying on a new parameter.

## Claude Desktop

Download `viajante.mcpb` from the
[latest release](https://github.com/felipebasurto/viajante/releases/latest) and open it with
Claude Desktop. The extension declares a uv runtime and pins the PyPI release that matches its
version, with the same extras as `npx -y @viajante/mcp`. Detail scrape, Booking.com and
`search_explore` still need Chromium, installed once:

```
uvx --from 'viajante[mcp,browser]' playwright install chromium
```

Manual config, merged into `claude_desktop_config.json` (Settings, Developer, Edit Config):

```json
{ "mcpServers": { "viajante": { "command": "npx", "args": ["-y", "@viajante/mcp"] } } }
```

## Claude Code

Plugin (MCP server and skill):

```
/plugin marketplace add felipebasurto/viajante
/plugin install viajante@viajante
```

The plugin pins the npm release of its own version. Or the server alone:

```
claude mcp add --scope user viajante -- npx -y @viajante/mcp
```

## Codex

```
codex mcp add viajante -- npx -y @viajante/mcp
```

The same entry in `~/.codex/config.toml`:

```toml
[mcp_servers.viajante]
command = "npx"
args = ["-y", "@viajante/mcp"]
startup_timeout_sec = 120
```

## ChatGPT

The desktop app reads the Codex entry above. ChatGPT on the web cannot start a program, so it
reaches viajante through OpenAI's
[Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels). The
workspace must allow custom MCP servers, and you need a tunnel and a restricted API key
(Tunnels Read and Use) from OpenAI Platform. On the computer that runs viajante:

```
export CONTROL_PLANE_API_KEY="sk-..."
tunnel-client init --sample sample_mcp_stdio_local --profile viajante \
  --tunnel-id YOUR_TUNNEL_ID --mcp-command "npx -y @viajante/mcp"
tunnel-client run --profile viajante
```

Keep `tunnel-client run` running. In ChatGPT Plugins, add a custom MCP server and set Connection
to Tunnel. viajante is not hosted and its HTTP server has no authentication, so there is no
public URL to give ChatGPT.

ChatGPT's own Python tool has no outbound network, so a script there cannot search.

## No MCP: scripts

An agent with Python and outbound network needs no MCP and no skill:

```
pip install viajante
python -m viajante flights JFK-LHR:2027-03-15 --fetch sweep --top 5 --save out.json
```

`python -m viajante` is the `viajante` command for a sandbox where the console script is not on
PATH. From Python, `get_flights(...).to_dict()` is JSON-ready (see the
[Python library](usage.md#python-library)). A cloud sandbox shares an IP that Google may block;
read `error_code` and `retry_after`, and do not retry a `rate_limited` result.

## Gemini CLI

```
gemini mcp add -s user viajante npx -y @viajante/mcp
```

The same entry in `~/.gemini/settings.json`:

```json
{ "mcpServers": { "viajante": { "command": "npx", "args": ["-y", "@viajante/mcp"], "timeout": 120000 } } }
```

`-s user` writes the user settings. Without it the entry goes to the project's `.gemini/settings.json`.

## Cursor

`.cursor/mcp.json` in the project, or `~/.cursor/mcp.json` for every project:

```json
{ "mcpServers": { "viajante": { "command": "npx", "args": ["-y", "@viajante/mcp"] } } }
```

## VS Code

Workspace-root `.mcp.json`, or `~/.copilot/mcp-config.json` for every workspace. Both use a
top-level `mcpServers` object:

```json
{
  "mcpServers": {
    "viajante": { "type": "stdio", "command": "npx", "args": ["-y", "@viajante/mcp"], "tools": ["*"] }
  }
}
```

**MCP: Add Server** writes the same entry. The editor-managed `.vscode/mcp.json` uses `servers`
instead of `mcpServers`, and `code --add-mcp` writes that file:

```
code --add-mcp '{"name":"viajante","type":"stdio","command":"npx","args":["-y","@viajante/mcp"]}'
```

## Zed

`settings.json` (`zed: open settings file`):

```json
{ "context_servers": { "viajante": { "command": "npx", "args": ["-y", "@viajante/mcp"], "env": {} } } }
```

## Goose

```
goose session --with-extension "npx -y @viajante/mcp"
```

Or in `~/.config/goose/config.yaml`:

```yaml
extensions:
  viajante:
    name: viajante
    type: stdio
    cmd: npx
    args: [-y, "@viajante/mcp"]
    enabled: true
    timeout: 300
```

## OpenCode

`opencode.json` in the project, or `~/.config/opencode/opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "viajante": { "type": "local", "command": ["npx", "-y", "@viajante/mcp"] }
    }
  }
}
```

Versions that predate the `servers` key read the same entry directly under `mcp`.

## Container

```
docker build --tag viajante-mcp .
docker run -i --rm viajante-mcp
```

The server talks over stdin and stdout, so keep `-i` and do not detach. Cooldowns and consent
cookies live in `/state` inside the container; mount a volume there to keep them. The image
installs the checkout it is built from, not a PyPI release.

## Other clients

Any client that starts a local stdio MCP server can run `npx -y @viajante/mcp`. The server is
loopback-only over HTTP, has no authentication, and is never hosted, so a client that accepts
only a remote URL cannot use it.
