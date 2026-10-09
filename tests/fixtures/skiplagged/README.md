# Skiplagged MCP fixtures

Captured live on 2026-10-08 from the public Skiplagged MCP server
(`https://mcp.skiplagged.com/mcp`, server `@skiplagged/mcp` 0.0.4, protocol
`2025-03-26`). Client: the repo's own `viajante.skiplagged` HTTP code, with
one-second pacing between calls and two seconds in the capture script.
Search dates: 2026-11-17 (outbound), 2026-11-22 (return). No HTTP 429 was seen.

Files:

- `tools_list_sk_flights_search.json`: the `sk_flights_search` entry from
  `tools/list` (input and output schema, annotations). Other tools trimmed.
- `flights_jfk_mia_oneway.json`, `flights_jfk_ord_oneway.json`,
  `flights_jfk_den_oneway.json`: `result` objects from `tools/call`
  `sk_flights_search` (`limit` 8, `sort` price, `adults` 1). Each holds the
  markdown `content` text and `structuredContent` (8 cards, `pagination`,
  `searchUrl`).
- `flights_jfk_mia_roundtrip.json`: same, with `returnDate` 2026-11-22. Cards carry
  a nested `returnFlight`.
- `hotels_miami_search.json`: `result` of `tools/call` `sk_hotels_search` (city
  Miami, 2026-11-17 to 2026-11-20, 2 adults, 1 room, `limit` 8, `sort` price),
  captured the same day with `imageUrl` removed. `structuredContent.results[].price`
  is a per-night rate (`$30.67/night`); the stay total with taxes and the review
  score (`5.8/10`) appear only in the markdown table, so the parser reads both.

No session ids, headers, cookies or tokens are stored. The `searchUrl` and
`deepLink` values include the public `utm_*` tracking parameters the server
adds. They are not personal data.
