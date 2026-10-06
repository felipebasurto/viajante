# Install viajante as an MCP server

Stdio process by default. No API keys. Local Streamable HTTP is opt-in (see below). One search at a time.

A second search while one is running raises `a viajante search is already running in this process` immediately. That busy error is not `MCP error -32001: Request timed out`. Do not treat timeouts as lock-busy. `lookup_airports` and the offline `validate_itinerary` may run during a search. Optional `country` is Google `gl` (origin market); omit when unset; do not pass a destination ISO. `max_stops` is 0, 1, or 2. If a calendar returns `blocked`, stop that request.

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

Reload MCP. The 19 tools are: `get_runtime_info` (offline executing package/Python/schema versions), `search_flights`, `search_dates`, `search_flex`, `search_explore`, `search_hotels` (Google by default; `stays` batches up to 8 stays; named `near` adds straight-line distance; `max_distance_km` requires `near` and excludes outside/unknown coordinates before ranking), `search_hotel_rooms` (Skiplagged room rates, USD), `get_hotel_details` (a hotel offer this process returned; `room_rates` true fetches a separate USD room quote and is open-world, `room_rates` false is a local read), `search_trip`, `lookup_airports`, `search_hidden_city` (Skiplagged, opt-in; USD/omit currency), `compare_awards`, `lookup_transfers`, `validate_itinerary` (offline tri-state validation), `recheck_offer` (one fresh Google Flights search that re-checks an earlier offer by flight numbers and departure times; a search, so it takes the one-search lock and is never cached; `check_failed` means it could not complete), `plan_stay_blocks`, `split_stay_costs` (offline roster blocks and per-person cost split), `verify_answer` (offline draft-reply evidence check), and `get_guide` (the long operational guide; the same markdown is the `viajante://guide` resource). Every tool has a human title and read-only annotations (`readOnlyHint`, `destructiveHint: false`, `idempotentHint`); `openWorldHint` is true for `search_*`, `recheck_offer`, and `get_hotel_details`. Needs `mcp>=1.14.1`.

Hotel source `skiplagged` is opt-in, supports up to 10 adults and 9 rooms, and
rejects `entire_home`. Omit currency or name USD; nothing converts. Read
`resolved_place` before claiming the requested city was searched and
`applied.not_applied` before claiming free cancellation. `search_hotel_rooms`
selects by `hotel_id` or exact normalized `hotel_name` plus `city`, supports
up to 5 rooms, and returns rates in provider order. Its `occupancy_limit` does
not prove a party fits across several rooms. Google hostel totals may price
dormitory beds; check room names and confirm on the provider.

On `rate_limited: true`, stop and wait (`retry_after` / `retry_after_seconds` on the error say how long when a cooldown was recorded; otherwise 30–60 minutes); do not retry or switch
method. Direct Google HTTP 429 and data-less status 13 share a cooldown;
Skiplagged HTTP 429 has its own cooldown and no retry. Status 13 can cause a
2-minute pause even when its cause is not throttling.

Check `get_runtime_info` before searching (available in 1.4.0+), and `viajante --version` for the CLI. Hotel reports carry `viajante_version`. An npm MCP pins its own Python package; it does not upgrade a separate installed uv tool. Unpinned `uvx --from viajante` can reuse that tool. Upgrade it explicitly with `uv tool upgrade viajante`, or use `uvx --refresh --from 'viajante==VERSION' viajante --version` with the published version. Do not assume latest was installed.

After a new release, `uvx` / `npx` can cache an old tool list. Reload the MCP client, `uvx --refresh --from viajante[mcp] viajante-mcp`, `npx -y @viajante/mcp@latest`, or point the client at a local `viajante-mcp`.

## Errors

A bad argument fails the tool call (`isError: true`). The text is the SDK's exact
`Error executing tool <name>: ` prefix followed by a JSON object. Strip that prefix
and parse the rest only if it starts with `{`:

```json
{"error": {"code": "invalid_parameter", "field": "origin", "message": "unknown origin IATA code: 'XXX'"}}
```

`field` is the parameter the message names, or `null` when it does not single one
out. Missing, mistyped and undeclared arguments get the same body (`field` is set only
when every failure is on one parameter). A second search while one runs is `code: "search_in_progress"` with the same
sentence as before. Rate-limited search errors keep `rate_limited: true` and their
message, and add `retry_after` (ISO 8601 UTC) and `retry_after_seconds` only while a
recorded cooldown is running.

## Local Streamable HTTP (opt-in)

```bash
viajante-mcp --transport streamable-http            # http://127.0.0.1:8000/mcp
viajante-mcp --transport streamable-http --port 8123
```

No authentication, no public hosting; it binds 127.0.0.1 unless `--host` says
otherwise (a non-loopback host prints a warning; any 127.0.0.0/8 address or `::1`
counts as loopback). A foreign `Host` or `Origin` header is refused. Every client shares this
machine's IP and the machine-wide provider cooldown. Client configs:

```json
{ "mcpServers": { "viajante": { "url": "http://127.0.0.1:8000/mcp" } } }
```

Claude Code: `claude mcp add --transport http viajante http://127.0.0.1:8000/mcp`.
Clients that only speak stdio keep using the `command` entries above.

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
