# Verifiable journeys and hotel finalists

This branch keeps three additions and the fixes that make them safe to use.

- Owned segment `arrival_date` and catalogue IANA timezones, with completeness
  `segment_dates` / `segment_timezones`. `validate_itinerary` checks
  `arrival_deadline`, `chronological`, and `min_stay_days` / `max_stay_days`.
  An `arrival_deadline` with an explicit offset is compared in UTC. A naive
  deadline is local at the arrival airport. Ambiguous, nonexistent, and missing
  civil times stay unknown.
- Metro codes (`LON`, `NYC`, `PAR`, `TYO`, and the rest of the owned table) on
  one-way and round-trip routes, and in `lookup_airports`. A route whose origin
  and destination resolve to the same metro is rejected.
- `get_hotel_details` reads a hotel offer this process returned. `selection_id`
  is stored only for those offers. A read does not enter the evidence ledger.
  `room_rates` must be a boolean. A separate Skiplagged room quote is not
  presented for the original stay when the city matches more than one place,
  the returned coordinates do not match the hotel, or the provider echoes
  different adults, rooms, or dates.

Flight search ranking matches the default search. Hidden-city offers keep
`evidence: "confirmed"` and do not receive a `selection_id`.

Offline gates on this tree, with fixtures only:

- `uv run ruff check src tests` — `All checks passed!`
- `uv run ruff format --check src tests` — `76 files already formatted`
- `uv run --with pytest pytest -q` — `1123 passed, 297 subtests passed`
- `uv run python -m unittest discover -s tests` — `Ran 1123 tests` / `OK`
- `uv run viajante bench` — `gate: ok` (`tests_ms: 1688`, `parse_ms: 31`, `score_ms: 1719`)

A stdio session against offline fixtures listed 17 tools. `search_hidden_city`
returned a priced offer (`622`, `evidence: confirmed`) with no `selection_id`.
`get_hotel_details` succeeded 25 times on one id. `room_rates: "yes"` was
rejected. `LON-LON` and `JFK-NYC` were rejected as the same metro. An arrival
at 09:00Z against `2099-07-02T09:00Z` was `pass`.

`search_flights` on the same offline card as `develop` differs only by
`completeness.segment_dates` and `completeness.segment_timezones`.

Live provider checks were not run. A Google 429 would not be evidence.
