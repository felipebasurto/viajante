# Loop history (one screen)

Read this before picking a hypothesis. Do **not** redo a listed keep or loss.
Do **not** plot `score_ms` across PRs (different VMs). Only same-host deltas count.
Checked-in `bench-baseline.json` is still **1603**. Do not rewrite it.

## Speed keep-or-revert

| pr | result | warm | best after | Δ | do not redo |
|---:|--------|-----:|-----------:|--:|-------------|
| 3 | keep | 1231 | 1157 | −74 | lazy `curl_cffi` |
| 6 | keep | 1112 | 1015 | −97 | cache CLI `ArgumentParser` |
| 9 | **loss** | 1045 | 1059 | +14 | wrb.fr itinerary walk-once |
| 11 | keep | 1060 | 1050 | −10 | IATA index + compiled regexes |
| 12 | keep | 1077 | 1029 | −48 | one city-alias regex |
| 13 | **loss** | 1021 | 1030 | +9 | cache `plan_prompt` |
| 14 | keep | 1028 | 1004 | −24 | slim IATA CSV columns |
| 15 | keep | 1028 | 1002 | −26 | lazy urllib/ssl/gzip |
| 16 | keep | 996 | 967 | −29 | marshal IATA blob |
| **17** | **keep** | **1006** | **560** | **−446** | FastMCP off unittest; help without `build_server` |
| 21 | **loss** | 561 | 571 | +10 | Path IATA marshal; `importlib.resources` off unittest |

#17 is the large one: unittest was importing the MCP SDK.
Do not redo #18: cache `load_prompt_cases()` / prompt JSONL parse (closed loss).
#21 re-run after #20: score_ms 561→578/571 (not strictly below warm). Cold `--help` 278→267 (did not veto). Reverted.

## Quality already landed (do not redo)

- #4 open-jaw keeps every pair; refuse impossible continents
- #5 midnight clocks; nested/sibling RT legs; Google `--rooms`
- #7 invented price/route = automatic 0; scored rows dump `plan=`
- #8 GRU dests kept; bare “first” is not cabin
- #10 `typical_eur` = same-route calendar median (`below`/`near`/`above`)

Battery on the operator box (judge): **79 → 88 → 89 → 98**, 0 fail. Last run after #7+#10. Import-path speed PRs did not re-run it.

## Honesty leftovers (leave them)

- Compact `--trip rt` with only the outbound in the bytes still has one leg. Do not invent the return.
- Typical is omitted when the calendar is thin or the trip is packaged RT / multi.
- Booking challenge timeout. Untouched.
- Do not shorten Playwright detail delays. No peer-scraper names. No live Google as the reward.
