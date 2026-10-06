"""Server instructions and the long guide for the MCP server. No SDK import."""

from __future__ import annotations

# Sent on every connection, so it carries only the rules a model must not miss.
INSTRUCTIONS = """\
viajante returns raw provider evidence, not recommendations. Read the payload, weigh
price against duration, stops, clocks and rating, and say why. The full guide is the
resource viajante://guide (or the get_guide tool): read it before a multi-step plan.

Evidence: before replying, pass the draft to verify_answer; it flags amounts, currencies,
codes, dates and links no search in this process returned. Never invent a fare, bag fee,
currency, IATA code or exchange rate. Viajante does not convert; you do the FX.
Currency: pass currency, or name an origin airport so it is proven. Hotels require currency.
If country, destination or currency is not proven, ask. Unnamed baggage_buffer is 0; pass
bags / carry_on so Google prices the bag.
Empty is not absent: no_results, blocked, markup_drift or an empty shortlist is not proof
that nothing exists. Say what was searched; do not claim availability either way.
Rate limits: if an error has rate_limited true, wait until retry_after (UTC) and tell the
user. Do not retry, switch fetch mode or fan out other searches; they send nothing.
One search at a time per process: a busy error is not an MCP timeout; do not retry it blindly.
search_hidden_city is Skiplagged (USD), not Google: run it at most once, after a named-route
search_flights, never when bags were named, and never mix its rows with Google evidence.
search_dates is the cheapest week, search_flex is +/-N around a named date, search_explore
is destination triage. max_stops is 0, 1 or 2.
"""

GUIDE = r"""\
# viajante MCP guide

## Overview

viajante-mcp is the stdio MCP server for local flight and hotel search.

This guide is the long operational reference. The short server instructions carry only the
load-bearing rules; everything else lives here, served as the `viajante://guide` resource and by
the `get_guide` tool.

## Install

```
Install:  uvx --from 'git+https://github.com/felipebasurto/viajante.git[mcp]' viajante-mcp
Checkout: uv sync --extra mcp && viajante-mcp
Browser:  uvx --from 'git+https://github.com/felipebasurto/viajante.git[mcp,browser]' \
            playwright install chromium
          (only for --fetch detail and Booking.com; extras must match the MCP env)
```

Stdio is the default transport. `viajante-mcp --transport streamable-http` serves the same tools
on a local Streamable HTTP endpoint (default `http://127.0.0.1:8000/mcp`). It has no
authentication: keep it on loopback. Every search leaves from this machine's IP and the
machine-wide provider cooldown applies to every client.

## Tools and the one-search rule

Tools: search_flights, search_dates, search_flex, search_explore,
search_hotels, search_hotel_rooms, search_trip, lookup_airports, search_hidden_city,
compare_awards, lookup_transfers, validate_itinerary, plan_stay_blocks,
split_stay_costs, verify_answer, get_runtime_info.
No auth. One search at a time in this process. A second search while one is
running raises "a viajante search is already running in this process" immediately.
That busy error is not MCP timeout -32001; do not treat timeouts as lock-busy
or retry them 8×60s. lookup_airports, compare_awards, lookup_transfers,
validate_itinerary, plan_stay_blocks, split_stay_costs, verify_answer, and get_runtime_info may run
during a search.

## Evidence and replying

Results are raw owned evidence, not a recommendation. You choose: read the
payload, weigh price against duration, stops, clocks, and rating, and say why.
Before replying, pass the draft to verify_answer; it flags amounts,
currencies, codes, dates, and links no search in this process returned.
verify_answer checks provenance, not link reachability, availability or room fit.
Check get_runtime_info before searching; an npm MCP and a separate installed
uv tool may execute different package versions. Do not assume uvx updates either.

## Rate limits and cooldowns

If an error has rate_limited true, tell the user to wait until the UTC time
named in its message.
Do not retry, switch fetch mode, or fan out other searches; they are paused
locally and send nothing. An identical successful search within 5 minutes
comes back cached (cached: true) without a new request.

A rate-limited result or error that has a recorded cooldown also carries `retry_after` (ISO 8601
UTC) and `retry_after_seconds`. When they are absent no cooldown was recorded (for example a
proxied 429); do not invent a wait.

## Choosing a search tool

search_dates is the cheapest week. search_flex is ±N around a named date.
Do not brute-force a date matrix. search_explore is dest triage from an origin.
search_dates is HTTP-calendar only and has no fetch parameter. If it returns
blocked, stop that request: a separate browser's consent or prices are not MCP
evidence. fetch=detail applies only to search_flights and needs the browser
extra plus Chromium in the MCP environment. max_stops is 0, 1, or 2; the
product cannot require 3+ stops.
search_hidden_city is Skiplagged, not Google. After a named-route
search_flights on a hub or leisure trunk, the caller may run it once
sequentially. Do not mix evidence. Skip when bags were named.
Skiplagged cards are USD; omit currency or pass USD. Do not copy a
Google/origin quote keep (GBP, JPY, …). A keep that matches no owned card is
currency_mismatch (owned quote stamped), not no_results. No FX.

## Local tools

compare_awards is local points math from a named offer; it does not invent seats.
lookup_transfers is a local partner table, not live award inventory.
plan_stay_blocks and split_stay_costs are local arithmetic over a per-night roster
the caller supplies; they never search, never convert money, never pick a stay.
validate_itinerary is local and offline. It returns pass, fail, or unknown from
owned v2 offer evidence; unknown evidence never becomes pass. It never fills
missing segment, baggage, or fare facts.

## Hotels and stays

Every MCP call is synchronous: never say you are still searching or will
report back; call the tool now or name the next step. Hotel location is one
named place; ask rather than substitute a nearby town. Hotel total_price is a
total-stay quote, not per person. Only claim the requested party total when
priced_adults agrees; unknown is unverified. General property descriptions do
not prove a private room. Check finalists' room rates and actual sleeping layout.
near names a reference point; max_distance_km requires it and excludes unknown
coordinates before ranking. Distances are straight lines, not walking routes.
Read resolved_place and report an unexpected place. Do not infer a city center.
link_context and applied.url_context distinguish stay, property, location and
none. A stay link preserves dates/adults/rooms, not guaranteed price or availability.
Never present an internal /travel/clk/hi tracker as a usable property link.
For changing groups use the latest confirmed nightly roster, group identical
people with plan_stay_blocks, and report unallocated_nights from split_stay_costs.
Do not extend a departing person's last night. Quote replacements before
recommending cancellation. A dorm room may lose exclusivity if beds are removed.
Use the exact cancellation deadline and property-local time from the reservation;
do not assume altered bookings retain prices, rooms or policy. Separate arithmetic
estimates from fresh quotes. Flight timing needs a verified transfer and airport
arrival margin; a latest check-out time alone does not prove a flight is reachable.
Keep warnings in the final response. Browser access denial is not a broken URL
or provider throttling: name the actual limitation and use available permitted
read-only evidence; never ask for permission the user already granted.

## Currency, bags and locale

Currency is currency or inferred from a named origin's owned country.
If unknown, ask. Hotels require currency (no origin airport). Viajante
does not convert. The calling agent may convert for the user. If country,
destination, or currency is not proven (a city with several airports,
Europe, unnamed origin, two possible currencies), do not pick: ask or
error. Unknown cannot prove include. Do not invent IATA, gl, or ISO 4217
from vibe. Optional country is Google gl (origin market); omit when unset;
do not pass a destination ISO. Unnamed baggage_buffer is 0. Prefer bags /
carry_on on the shopping request so Google prices the bag. Do not invent a
bag fee. Fetch locale is English. User prompts may be any language.
Compute ISO dates from today; do not send a past start.
"""
