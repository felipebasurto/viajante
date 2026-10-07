"""Server instructions and the long guide for the MCP server. No SDK import."""

from __future__ import annotations

# Sent on every connection, so it carries only the rules a model must not miss.
INSTRUCTIONS = """\
viajante returns raw provider evidence, not recommendations. Read the payload, weigh
price, duration, stops, clocks and rating. Read viajante://guide (or get_guide)
before a multi-step plan.

Read the envelope first: every tool except lookup_airports puts status, completeness,
empty_reason, error_code and retry_after beside its payload. Only empty_reason provider_empty
means "none found"; filtered_out means viajante's own filters removed the rows; not_loaded
means the search did not complete and availability is unknown. completeness partial: read
the per-query rows.

Evidence: before replying, pass the draft to verify_answer; it flags amounts, currencies,
codes, dates and links no search in this process returned. Never invent a fare, bag fee,
currency, IATA code or exchange rate. Viajante does not convert; you do the FX.
Currency: pass currency, or name an origin airport so it is proven. Hotels require currency.
If country, destination or currency is not proven, ask. Unnamed baggage_buffer is 0.
bags / carry_on: carry_on is verified on the page, checked bags are refused; see get_guide.
Empty is not absent: no_results, blocked, markup_drift or an empty shortlist is not proof
that nothing exists. Say what was searched; do not claim availability either way.
Rate limits: if an error has rate_limited true, wait until retry_after (UTC) and tell the
user. Do not retry, switch fetch mode or fan out other searches; they send nothing.
One search at a time per process: a busy error is not an MCP timeout; do not retry it blindly.
deadline_seconds stops a search early: rows with error code deadline were not loaded, not empty.
search_hidden_city is Skiplagged (USD), not Google: run it at most once, after a named-route
search_flights, never when bags were named, and never mix its rows with Google evidence.
search_dates is the cheapest week, search_flex is +/-N around a named date, search_explore
is destination triage. max_stops is 0, 1 or 2.
recheck_offer re-checks a finalist with one fresh search; check_failed means the check did not run,
never that the offer is gone.
Also search_split_tickets: separate tickets, so a missed connection is not protected. See the guide.
"""

GUIDE = r"""# viajante MCP guide

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
Also search_split_tickets (see Split tickets), a search that takes the one-search lock.
Also recheck_offer, a search that takes the one-search lock (see Re-checking an offer).
Also get_guide, which returns this guide (the same text as the viajante://guide resource).
Also get_hotel_details, which reads a hotel offer this process returned.
room_rates false is a local read and may run during a search; room_rates true
asks the provider and takes the one-search lock.
Also price_history (local read of recorded observations; may run during a search) and
watch_price (a search that takes the one-search lock; see Price history and watches).
No auth. One search at a time in this process. A second search while one is
running raises "a viajante search is already running in this process" immediately.
That busy error is not MCP timeout -32001; do not treat timeouts as lock-busy
or retry them 8×60s. lookup_airports, compare_awards, lookup_transfers,
validate_itinerary, plan_stay_blocks, split_stay_costs, verify_answer, and get_runtime_info may run
during a search. get_guide may also run during a search. get_hotel_details with
room_rates false may also run during a search.

Progress, cancel, deadline: a search tool sends notifications/progress only when the
client sends a progressToken; they are informational and the result is still the reply.
Cancelling the request stops the search at its next checkpoint and frees the lock; a
cancelled search is not cached, recorded as evidence, or counted toward a cooldown.
An optional deadline_seconds (or VIAJANTE_MCP_DEADLINE_SECONDS when the argument is
unset) stops starting new queries after that many seconds and returns what finished:
coverage.complete is false, coverage.stopping_reason is deadline, status is ok while
any row has evidence (otherwise timeout), completeness is partial, and each unfinished
query carries error code deadline with empty_reason not_loaded. That is availability
unknown, never "no results". The error carries no retry_after. A partial result is not
cached; ask again with a larger deadline_seconds or none.
In search_explore, `not_loaded` under a deadline means the prices weren't loaded,
even if destinations are listed.

## The result envelope

Read the envelope first. Every tool except lookup_airports (a bare list) puts
these top-level fields beside its payload: status (ok, no_results, rate_limited,
blocked, timeout, failed), completeness (complete, partial, blocked), empty_reason
(provider_empty, filtered_out, not_loaded; null when rows came back), empty_note,
error_code, retry_after / retry_after_seconds (a known cooldown), and observed_at
with observed_at_basis (fetch: when viajante asked; provider is reserved). Only
provider_empty may be told to a traveller as "no flights/hotels found". filtered_out
means viajante's own filters removed the provider's rows; not_loaded means the
search did not complete and availability is unknown. partial means some queries
failed or part of the window is missing: read the per-query rows. Offline tools
report status ok and are complete unless they say otherwise (unknown checks,
unallocated nights). get_guide carries the envelope too, beside its guide key.

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
comes back cached (cached: true) without a new request. The replay cache holds
at most 20 entries; a new successful search drops the oldest.

A rate-limited result or error that has a recorded cooldown also carries `retry_after` (ISO 8601
UTC) and `retry_after_seconds`. When they are absent no cooldown was recorded (for example a
proxied 429); do not invent a wait.

## Flight recommendation

A successful search_flights (and the flights half of search_trip) query may carry a
recommendation block beside its offers: one recommended pick, a shortlist of up to
three offers that differ in stops or departure slot, per-offer highlights and
tradeoffs from returned fields only, and published score weights. It is evidence,
not a verdict; offers and their order are unchanged. Read relaxed_requirements
first. A relaxed pick failed a requirement you named (listed there, and unmet in the
entry), so it is not an exact match. When the query row has empty_reason
filtered_out (envelope status no_results when every query is filtered_out) and a
recommendation, offers is empty because the named filters removed every row, and
the recommendation is a relaxed pick, not an exact match: say which requirements it
relaxed. Offers whose currency
differs or is unproven are never price-compared. Provider-empty queries carry no
recommendation. Fare rules (refund, change) are never in the rows. requirements
lists max_stops even when it is only the default, so its presence does not mean the
caller named a stop limit.

## Public-page transport in 1.4.6

Flights auto uses sweep public HTML bootstrap data, not unsigned shopping RPC.
Round trips pin up to eight owned outbound journeys and read the provider package
price from each return page; the result is scope-bound/partial and never a sum of
two one-way prices. Automatic typical-price calendar fanout is disabled. Dates and
flex request each named day (at most 31); flex then fetches the selected day freshly.
VIAJANTE_SWEEP_MODE=conservative caps HTTP/2 dispatch at two requests instead of
the standard eight. get_runtime_info reports the active mode and concurrency.
RPC status 13 has an unknown cause; it alone does not establish throttling or an IP
block. A 429 or status 13 stops unsent work and is not replayed or switched to detail.
Error diagnostics distinguish real HTTP status from RPC status and unsent requests;
cooldown_basis distinguishes provider Retry-After from a heuristic pause.

## Choosing a search tool

search_dates is the cheapest week. search_flex is ±N around a named date.
Do not brute-force a date matrix. search_explore is dest triage from an origin.
search_dates uses bounded per-day public-page GETs and has no fetch parameter. If it returns
blocked, stop that request: a separate browser's consent or prices are not MCP
evidence. fetch=detail applies only to search_flights and needs the browser
extra plus Chromium in the MCP environment. max_stops is 0, 1, or 2; the
product cannot require 3+ stops.
A metro code (LON, NYC, PAR, TYO, and the rest of the owned table) named on a
search_flights or search_trip route expands to its member airports. One call
sends at most 18 provider queries (LON has six members, NYC three). A plan
that would send more is rejected before anything is fetched. A route
whose origin and destination resolve to the same metro, including an
airport that belongs to the other side's metro, is rejected. lookup_airports
with the code lists its members. A metro code is never inferred from an airport
or a city.
search_hidden_city is Skiplagged, not Google. After a named-route
search_flights on a hub or leisure trunk, the caller may run it once
sequentially. Do not mix evidence. Skip when bags were named.
Skiplagged cards are USD; omit currency or pass USD. Do not copy a
Google/origin quote keep (GBP, JPY, …). A keep that matches no owned card is
currency_mismatch (owned quote stamped), not no_results. No FX.

## Split tickets

search_split_tickets is opt-in and costs extra searches (capped, sequential, stopped at the
first recorded cooldown). It builds separately ticketed itineraries only from real one-way
quotes it fetched: a one-way via a connection airport (`via`, up to 5 IATA codes, or layover
airports seen in the packaged results) or a round trip as two one-ways. Every itinerary says
split_ticket true, self_transfer true and connection_protected false. Tell the traveller: if
the first ticket is late, the second ticket does not protect the connection; bags may need to be
collected and checked in again; each ticket is confirmed on its own link.
Totals are summed only when every ticket is in one currency, else total is null and the
parts stay separate. vs_packaged compares only within a currency and carries savings (split
cheaper) or extra_cost (split dearer), both non-negative. Itineraries rank within one
currency; other currencies are capped at a few rows. Connection time is measured in UTC with
each airport's timezone; a missing timezone or a clock time that is ambiguous or does not
exist (a DST change) leaves timing unproven (timing_proven false, timing_note): say so.
Arriving at one airport and leaving from another is rejected as airport_mismatch.
Envelope: any itinerary is ok (partial if a fetch failed). With none, a failed fetch or
cooldown wins (rate_limited, blocked, timeout, failed; not_loaded; honour retry_after);
rows that were all rejected are no_results with filtered_out; only provider-empty legs are
provider_empty (every leg empty, whatever the packaged fare was). coverage is heuristic:
it never proves other hubs or dates have no fare.
observed_at is the search time, null when every fetch was a recorded cooldown.

## Local tools

compare_awards is local points math from a named offer; it does not invent seats.
lookup_transfers is a local partner table, not live award inventory.
plan_stay_blocks and split_stay_costs are local arithmetic over a per-night roster
the caller supplies; they never search, never convert money, never pick a stay.
validate_itinerary is local and offline. It returns pass, fail, or unknown from
owned v2 offer evidence; unknown evidence never becomes pass. It never fills
missing segment, baggage, or fare facts. Pass legs in travel order.
arrival_deadline and chronological read that order: arrival_deadline compares
the final arrival with a named instant (an explicit offset is UTC, a naive time
is local at the arrival airport) and chronological checks owned segment instants.
min_stay_days and max_stay_days use owned dates between journeys; fewer than two
journeys is unknown. Missing, ambiguous, and nonexistent civil times stay unknown.

## Re-checking an offer

recheck_offer is a search: it runs one fresh Google Flights query (never the 5-minute replay
cache), under the one-search lock and the machine-wide cooldown, and matches an earlier offer by
flight numbers and scheduled departure times. Re-check finalists before presenting them as
current. Outcomes: same_price, price_changed, not_found, multiple_matches, incomplete_identity,
check_failed, and substituted only with allow_substitute. multiple_matches picks no offer and
gives no price verdict. incomplete_identity sent nothing: the offer lacks flight numbers,
airports or clocks (`missing` names them); allow_loose_match opts in to carrier plus times and
labels the result loose_match. check_failed (check_completed false: blocked, rate limited,
incomplete offers, any provider error) means the check did not run to an answer, not that the
offer is gone; do not retry a rate limit. A closest_candidate on a not_found is a different
itinerary, for information only. Pass the offer's own currency; a different one is refused.
The envelope reads: a found itinerary is ok; not_found is no_results only for provider_empty or
filtered_out (an answered not_among_offers stays ok); check_failed carries the failure status and
not_loaded; incomplete_identity is failed and blocked. Caller-typed values are not recorded as
owned evidence. It is not a booking guarantee; the price is confirmed only on the provider's own
page.

## Hotel finalist details

get_hotel_details reads a hotel offer this process returned. selection_id is
attached only to those hotel offers. The read does not enter the evidence ledger
and does not evict a stored search. room_rates must be a boolean. False returns
the stored quote (status ok, completeness complete) and does not search. True
fetches a separate Skiplagged USD room quote. The envelope on that quote is
stamp_search of the room quote: ok when rates came back, no_results with
provider_empty when the provider returned none, or rate_limited, blocked,
timeout, or failed with the same retry_after fields as the error. A city that
matches more than one place sends nothing: status ok, completeness partial,
error_code ambiguous_city. Occupancy, dates, or coordinates that do not match
are no_results, empty_reason filtered_out, and error_code occupancy_mismatch,
dates_mismatch, or property_mismatch; room_quotes stays empty. When both
occupancy and dates differ, error_code is occupancy_mismatch and both flags
stay. An echo of nothing is echo unknown and is not a confident match.
When room_rates is true and the provider does not echo adults, rooms, and dates,
a returned quote is partial (never ok and complete together). An inconclusive
read never shows ok/complete.

## Price history and watches

price_history reads this machine's own recorded observations and reports, for one exact query in
one currency, first_seen, last_seen, lowest, highest and the change since the previous
observation. One observation means no trend. It never predicts and never compares or converts
currencies: each currency is its own series. Recording is opt-in: the server environment needs
VIAJANTE_PRICE_HISTORY=1 (unset it to stop; `viajante history --clear` deletes the log). Only a
real search that returned a priced offer is recorded; a cached replay, an empty result and a
rate-limited or blocked search record nothing. recheck_offer goes through the same search, so with
history on it also records one observation: a real observed price, nothing more.

If the log exists but cannot be read, price_history returns read_error and series null (unknown,
not empty), with status failed, completeness blocked and error_code history_unreadable. Do not
report that as "no history".

watch_price re-runs a saved search_flights or search_hotels argument set on demand and reports the
change since the last observation of the same query and currency. It saves the watch and records
its observation (even when the global opt-in is off), so it is not read-only. It takes the
one-search lock, the cooldown and the 5-minute cache like any search; its envelope is the inner
search's, with completeness partial when history could not be read (change is then null). It
sends no notification and schedules nothing: do not call it in a loop, because Google rate-limits.
Saving under an existing name replaces that watch, and kind is flight or hotel as in price_history.
If the saved watches file cannot be read, the list is watches null with status failed, completeness
blocked and error_code watches_unreadable (never an empty list), and saving or removing refuses.

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
do not pass a destination ISO. Unnamed baggage_buffer is 0. The 1.4.6
public-page transport sends a positive carry_on (a party total, not per
passenger) and airline, exclude-airline and alliance filters, and every page
must echo each one or the read fails. Google registers an airline exclusion
without applying it, so viajante drops excluded or carrier-unknown cards
itself. Owned carrier codes match exactly (returned codeshares count); name
aliases are a fallback only when codes are absent. It refuses named checked
bags, carry_on 0, exclude_alliances,
and multi-city (use fetch=detail) before network work. search_explore needs the
browser extra and accepts only one adult in economy. Detail
refuses every bag and carrier filter because it reads no page echo. Offer bag
allowances stay unknown. Do not remove a requested filter without an
explicitly separate scenario.
Do not invent a bag fee. Fetch locale is English. User prompts may be any language.
Compute ISO dates from today; do not send a past start.
"""
