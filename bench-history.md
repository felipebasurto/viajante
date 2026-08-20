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
| 23 | **loss** | 549 | 572 | +23 | known-IATA frozenset; `plan_prompt` pair scan once |
| **25** | **loss** | **557** | **580** | **+23** | lazy selectolax / Lexbor off `google_flights` import |
| **26** | **loss** | **589** | **570** | −19 | IATA marshal rows as tuples; `Airport` only on lookup hits |
| **27** | **keep** | **850** | **752** | **−98** | `is_known_iata` via `_BY_CODE`; city scan only on `lookup_airports` |
| **28** | **keep** | **688** | **660** | **−28** | one combined LCC airline regex; `_bag_evidence` calls `is_low_cost` once |
| **29** | **keep** | **669** | **650** | **−19** | compile hotel evidence regexes once; one combined pattern per family |
| **30** | **loss** | **711** | **654** | −57 | one Lexbor tree for HTTP cards; no `main.html` re-parse |

#17 is the large one: unittest was importing the MCP SDK.
Do not redo #18: cache `load_prompt_cases()` / prompt JSONL parse (closed loss).
#21 re-run after #20: score_ms 561→578/571 (not strictly below warm). Cold `--help` 278→267 (did not veto). Reverted.
#23: score_ms 549→572/578 (not strictly below warm). Cold `--help` 285→280 (did not veto). Reverted.
#25: score_ms 557→580/584 (not strictly below warm). `parse_ms` 2→20 (parent bench process paid Lexbor at corpus parse; unittest subprocess still paid it). Cold `--help` 277→260 (did not veto). Reverted.
#26: score_ms 589→581/570 (both below warm). Cold `--help` 290→394 (veto). Reverted.
#27: score_ms 850→787/752 (both below warm). Cold `--help` 318→304 (did not veto). Kept. Baseline left at 1603.
#28: score_ms 688→665/660 (both below warm). Cold `--help` 292→289 (did not veto). Kept. Baseline left at 1603.
#29: score_ms 669→650/659 (both below warm). Cold `--help` 280→280 (did not veto). Kept. Baseline left at 1603.
#30: score_ms 711→654/656 (both below warm). Cold `--help` 281→285 (veto). Reverted.

## Quality keep (weekday)

- KEEP METRIC is `judge_mean` (mean of `score_1_100` on `judge=llm` scored rows). Gate = suite+fail:0. Keep iff `judge_mean` strictly up; Δ<3 → second run. Holdout is human veto. `score_ms` is not the keep. Agent must not edit the judge or pad easy prompts.

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
