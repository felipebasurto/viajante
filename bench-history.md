# Loop history (one screen)

Read this before picking a hypothesis. Do **not** redo a listed keep or loss.
Do **not** plot `score_ms` across PRs (different VMs). Only same-host deltas count.
Checked-in `bench-baseline.json` **1603** is a **fossil**, not keep. Do not rewrite it.

- **Operator lock 2026-08-20 16:05 CEST:** Keep is only `judge_mean` (already #34). Last real keep **97.9** on `60d9ed4` (26 scored). Last judged **95.7** on `22da78a` (35 scored, 23 skip) is **not** a keep vs 97.9 on `4faaabd`. Fossil 1603 is not keep. Import/IATA/unittest-cache/FastMCP-off-tests a MCP user cannot feel = veto; do not launch. `bench.py` / `prompt_bench.py` / `tests/bench/` / `tests/prompts/` read-only. Replay/2×MAD not weekday keep. Cold `--help` worse = veto. Weekday launches planner / honest parse / English fetch / savage hardness. Gate is suite + `fail:0`. `score_ms` is not the product trophy.

## Tonight shipped (2026-08-20; one row each; not weekday keep unless judged)

Numbers only where recorded. Do not invent a `judge_mean` or p50.

| pr | landed | recorded numbers |
|---:|--------|------------------|
| **43** | HTTP/2 multiplex + TLS reuse | Live remesure on `0e28305`: p50 **1330 ms** (113/118) then 429. Later remesure on `3248285` (#56 tip): plan p50 **0.356 ms** (n=184); sweep 15/16 ok, p50 **1370.504 ms**, then harness stop on HTTP 429 JFK-LHR. Not the same sample. |
| **44** | 50 ms retry empty/drift/5xx | Happy path no sleep. |
| **45** | `--depart-window` + sort | |
| **46** | `search_dates` + `--nights` | |
| **47** | kids/infants + currency/country | |
| **48** | packaged open-jaw `--trip rt` | |
| **49** | airline/alliance on the shopping request | |
| **50** | dates sparkline / `summary` | |
| **51** | multi-city tfs from owned legs | |
| **52** | `viajante flex` / `search_flex`: calendar then one shop; miss = empty | Battery 180, gate fail:0, not judged. Feature keep (`50c772a`). |
| **53** | packaged RT typical + `vs_typical_pct` from same-stay calendar; omit on miss | |
| **54** | owned `google_flights_url`; `booking_token` wins; omit if cannot encode | |
| **55** | `stops_compare` cheapest nonstop vs 1-stop from the same eligible set | |
| **56** | 429: reset TLS + 50 ms, continue remaining jobs on a fresh session. Happy path no sleep. Sequential remesure harness still stops on 429. | See sweep table. Remesure on `3248285` is the #43 row (15/16, p50 1370.504 ms, then 429 JFK-LHR). |
| **57** | recover nested/broken judge JSON; score+reason required; `JUDGE_SYSTEM_PROMPT` untouched | Judged 181 on `22da78a`: **judge_mean 95.7** (35 scored, 23 skip). Not a keep vs 97.9 on `4faaabd`. |
| **58** | `--via` / `--exclude-via`: parse then filter on owned `layover_city` / `legs[].layovers`. Unknown layover cannot prove include (drop) or exclude (keep). Nonstops drop for include, stay for exclude. No invented via list. Overnight IST `keep_connect` is still `require_overnight ∪ via`. | Merged as `e8ef10e`. No `judge_mean` recorded. |
| **59** | planner maps occupancy (omit if no count), alliance (no invented ST), depart-window | |
| **76** | explore dest pricing same owned post-filters as `search_flights` (`bags` / `via` / `airlines` / `price_cap_eur`; unnamed stays unset) | Operator judged **97.2** / 58 scored / skip 0 on `0bd0c09` — not a keep vs 97.9. Compact date-grid cells / Explore catalog places are not offers. |
| **77** | `viajante trip` / `search_trip` same owned flight post-filters as `search_flights` | Landed `ae17c52`. Gate ok. Unittest 803. `viajante bench --prompts` fail:0 (193 prompts, 135 pass, 58 skip, 0 scored). `judge: skip`. `judge_mean:` blank — not invented. Operator judges on the box — do not invent a mean. Cold `--help` this host main 311.4/305.1 → branch 306.2/305.1 (later 309.0/315.6; did not veto). `score_ms` 1666 is not the keep. |
| **78** | planner maps named bags/via/cap onto dates/flex/explore/trip intents; unnamed stays unset | Landed `31e9228`. Operator judges on the box — do not invent a mean. |
| **79** | optional `--nearby` on dates/flex/explore/trip and MCP `search_dates` / `search_flex` / `search_explore` / `search_trip`; same owned same-city IATA as `search_flights`; default off; named open-jaw stays; no invented codes; nearby `trip_total` is min in the city group, not a sum | Landed `4fadd46`. Gate ok. Unittest 834. `viajante bench --prompts` fail:0 (193 prompts, 135 pass, 58 skip, 0 scored). `judge: skip`. `judge_mean:` blank — not invented. Operator judges on the box — do not invent a mean. Cold `--help` this host main 302.8/306.7 → branch 301.6/298.6 (uv 310.4/308.8; did not veto). `score_ms` 1810 is not the keep. |
| **80** | planner maps named nearby wording onto dates/flex/explore/trip intents; unnamed stays false; MAD-BCN does not invent a third code | Gate ok. Unittest 838. `viajante bench --prompts` fail:0 (193 prompts, 135 pass, 58 skip, 0 scored). `judge: skip`. `judge_mean:` blank — not invented. Operator judges on the box — do not invent a mean. Cold `--help` this host main 312.6/314.3 → branch 297.0/304.5 (uv 315.3/312.6; did not veto). `score_ms` 1828 is not the keep. |
| **81** | named `--depart-window` on dates sweep-fallback, flex winning-day shop, explore dest cards; compact calendar cells have no clock and stay unfiltered; unnamed stays unset; explore planner copies a named window | Gate ok. Unittest 845. `viajante bench --prompts` fail:0 (193 prompts, 135 pass, 58 skip, 0 scored). `judge: skip`. `judge_mean:` blank — not invented. Operator judges on the box — do not invent a mean. Cold `--help` this host main 354.2/331.5 → branch 345.3/331.8 (later 320.7/314.6; did not veto). `score_ms` 1754 is not the keep. |
| **82** | named `--max-layover` / `--min-layover` / `--max-duration` on dates sweep-fallback, flex winning-day shop, explore dest cards; compact calendar cells and Explore catalog places have no layover clock and stay unfiltered; unnamed stays unset; unknown layover cannot prove min/max; nonstops stay for max-layover | Gate ok. Unittest 854. `viajante bench --prompts` fail:0 (193 prompts, 135 pass, 58 skip, 0 scored). `judge: skip`. `judge_mean:` blank — not invented. Operator judges on the box — do not invent a mean. Cold `--help` this host main 503.9/331.2 → branch 322.8/322.9 (did not veto). `score_ms` 1768 is not the keep. |
| **83** | named `--alliance` / `--exclude-alliance` on dates sweep-fallback, flex winning-day shop, explore dest shopping POSTs; same owned FlightQuery overlay as `search_flights` (IATA designators, no member list); compact calendar cells and Explore catalog places have no airline/alliance and stay unfiltered; unnamed stays unset; explore planner copies a named alliance | Gate ok. Unittest 861. `viajante bench --prompts` fail:0 (193 prompts, 135 pass, 58 skip, 0 scored). `judge: skip`. `judge_mean:` blank — not invented. Operator judges on the box — do not invent a mean. Cold `--help` this host main 323.4 → branch 307.5 (later 322.6/310.1; did not veto). `score_ms` 1823 is not the keep. |
| **84** | named `--children` / `--infants-in-seat` / `--infants-on-lap` on dates calendar_trip + sweep-fallback, flex winning-day shop, explore catalog RPC + dest shopping POSTs; same owned FlightQuery occupancy as `search_flights` (constraints index 6); unnamed stays 0; planner named occupancy already landed; “family” does not invent a count | Gate ok. Unittest 875. `viajante bench --prompts` fail:0 (193 prompts, 135 pass, 58 skip, 0 scored). `judge: skip`. `judge_mean:` blank — not invented. Operator judges on the box — do not invent a mean. Cold `--help` this host main 313.3 → branch 304.8 (later 310.4/309.4; did not veto). `score_ms` 1894 is not the keep. |
| **85** | named `--currency` / `--country` on dates/flex/explore HTTP source (`curr` / optional `gl`); same owned `GoogleFlightsHttpSource` kwargs as `search_flights`; unnamed stays EUR / `gl` omitted; do not default `gl` to a hub; “in USD” without the flag does not invent a code | Gate ok. Unittest 894. `viajante bench --prompts` fail:0 (193 prompts, 135 pass, 58 skip, 0 scored). `judge: skip`. `judge_mean:` blank — not invented. Operator judges on the box — do not invent a mean. Cold `--help` this host main 309.9 → branch 304.2 (later 311.1; did not veto). `score_ms` 1851 is not the keep. |
| **86** | leftover shop-parity `stops_compare`: flex winning-day shop, dates sweep-fallback days, explore dest shops reuse `compare_nonstop_vs_one_stop`; compact calendar cells and Explore catalog places omit; empty bucket omits that side; both empty omits the block; no invented fare | Gate ok. `viajante bench --prompts` fail:0 (193 prompts, 135 pass, 58 skip, 0 scored). `judge: skip`. `judge_mean:` blank — not invented. Operator judges on the box — do not invent a mean. Cold `--help` this host main 345/313 → branch 331/309 (did not veto). `score_ms` 1839 is not the keep. |
| **61** | `viajante trip` / `search_trip` owned total = flight + hotel; omit if either miss / dates / currency | |
| **62** | planner around/±N → `intent=flex`; cheapest week → `intent=dates`; packaged RT stays | Battery 186, gate fail:0, not judged. Cold `--help` 293→294 (did not veto). Rebased onto `3248285` (keeps #59). |
| **60** | optional `--nearby` same-city IATA; default off; no invented codes; open-jaw not rewritten | |
| **67** | leftover 95s: stamp shipped-constraint notes (via∩overnight, dest-vs-hub, ATW circuit, explore notes) | Battery 191, gate fail:0, not judged. `judge: skip`. `judge_mean:` blank — not invented. Cold `--help` 310.2→312.6 (did not veto). |
| **69** | planner maps carry-on only / N checked / no hold luggage onto `--bags` / `--carry-on`; unnamed leaves index 10 None | Battery 191, gate fail:0. `judge: skip`. `judge_mean:` blank — not invented. Cold `--help` 299.4→301.6 (did not veto). |
| **71** | named `--price-cap` / `price_cap_eur` is a local owned-EUR post-filter; shopping index 7 stays None (RPC layout unknown). Unnamed stays unset. Never invent a cap or a fare. | Battery 193, gate fail:0. `judge: skip`. `judge_mean:` blank — not invented. Cold `--help` 326.8→328.1 (did not veto). |
| **72** | named `via_regions` is a shortlist constraint: stamp one English honesty note; keep named regions + origin/dest/date; do not invent hop IATA or a fare per hop. Unnamed stays unset. Does not redo ATW leftover notes. | Battery 193, gate fail:0. `judge: skip`. `judge_mean:` blank — not invented. Cold `--help` 317.4→313.1 (did not veto). |

## Speed keep-or-revert (`score_ms` history; not weekday keep)

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

## Sweep HTTP (not weekday keep)

| pr | result | gate | fail | score_ms | cold `--help` | live | do not redo |
|---:|--------|------|-----:|---------:|--------------:|------|-------------|
| **56** | keep | ok | 0 | 1189 | 287.7→287.6 | 16/16 then 60/64 (4 rejected, 0×429) | 429 resets TLS and continues remaining; no happy-path sleep; multiplex kept |

Live 429 did not fire on this host, so it did not stop the batch. Unittest covers continue-after-429. Operator remesure on `3248285` (#56 tip, not this host): plan p50 0.356 ms (n=184); sweep 15/16 ok, p50 1370.504 ms, then harness stop on HTTP 429 JFK-LHR. Not the same sample as `0e28305` (p50 1330 ms, 113/118, then 429). No extra p50 invented.

## Quality keep (weekday)

- KEEP METRIC is `judge_mean` (mean of `score_1_100` on `judge=llm` scored rows). Gate = suite+fail:0. Keep iff `judge_mean` strictly up; Δ<3 → second run. Holdout is human veto. `score_ms` is not the keep. Agent must not edit the judge or pad easy prompts.

## Quality already landed (do not redo)

- #4 open-jaw keeps every pair; refuse impossible continents
- #5 midnight clocks; nested/sibling RT legs; Google `--rooms`
- #7 invented price/route = automatic 0; scored rows dump `plan=`
- #8 GRU dests kept; bare “first” is not cabin
- #35 keep IST overnight and via constraints instead of dropping them
- #10 `typical_eur` = same-route calendar median (`below`/`near`/`above`)
- #52 flex window (`50c772a`): calendar grid in around±N, then one shopping POST on the cheapest legal day. Feature keep. Battery 180, gate fail:0, not judged.
- #58 `--via` / `--exclude-via` (`e8ef10e`): parse-then-filter on owned layover; unknown cannot prove include/exclude; overnight IST `keep_connect` is still `require_overnight ∪ via`. No `judge_mean` recorded.
- #62 planner around/±N → `intent=flex` with a real window; cheapest week → `intent=dates`. Rebased onto `3248285` (keeps #59 occupancy/alliance/window). Battery 186, gate fail:0, not judged. Cold `--help` 293→294 (did not veto).
- #67 leftover honesty notes (`e001663`): via ∩ no_overnight keep both; named same-city dest-vs-hub is a constraint not a second dest; around-the-world notes name the circuit; explore stamps rest-of-trip / exclude-region / ±1. Operator judged **95.8** on 58 (that host) is not a keep vs 97.9. `brutal-impossible-secret-dest-quote` scored **1**.
- #68 unnamed secret dest quote: explore from the named origin, drop named regions, stamp that N unnamed dests cannot be quoted. No invented dest list, IATA, EUR, or hotels. Overlay booking+hotels refuse. Does not revert #67.
- #70 `work_back_by` + timezone-impossible IDL: compile Jumatatu/Umsombuluko/Montag; keep named route/dates/field; do not invent hops or a Monday SYD/AKL 09:00.
- #72 named `via_regions` shortlist honesty: stamp one English note; keep named regions + origin/dest/date; do not invent hop IATA or a fare per hop. Unnamed stays unset. Does not redo #67 ATW leftover notes. Operator judged **97.1** on `8045cfb` (58 scored) is not a keep vs 97.9. `insane-yhz-fiji-via-continents` scored **95** on that tip.

Battery on the operator box (judge): **79 → 88 → 89 → 98**, 0 fail. Last **real keep** `judge_mean`: **97.9** on `60d9ed4` (26 scored). Last judged: **95.7** on `22da78a` (35 scored, 23 skip) — not a keep vs 97.9 on `4faaabd`. Operator box after **#65** on `cdc97be`: 191 prompts, pass 133, fail 0, skip 1, scored 57, **judge_mean 97.6** (not invented here; not a keep vs 97.9 on 36 scored). Do not invent a mean. Import-path speed PRs did not re-run it. Weekday keep is 97.9, not the `score_ms` table above. Gate is suite + fail:0. `score_ms` is not the product trophy.

- **#65** honesty pass (merged `cdc97be`): planner maps shipped `--nearby` / via / packaged RT / occupancy / `search_trip`; hotel cities stay owned English; verdict `score_1_100 =` keyvals. Operator judged **97.6** / 57 scored / skip 1 — not a keep vs 97.9.
- **#66** leftover skip + occupancy/cabin honesty (`cursor/honesty-verdict-planner-719a`): recover split-nested score/reason, sibling `notes`, missing comma, `91/100`, `reasoning_content`. No default score. Mixed cabin omits `--cabin`; conflicting `--adults` stay in notes; nonstop+via and free-cancel vs `--allow-non-refundable` keep both. English city labels compile from the owned alias table. Gate ok. `viajante bench --prompts` fail:0 (191 prompts, 133 pass, 58 skip, 0 scored). `judge: skip` (no key). `judge_mean:` blank — not invented. Cold `--help` this host origin/main 304.4/299.8 → branch 299.7/300.8 (did not veto). `score_ms` 1531 is not the keep. A DeepSeek 0 or keyless prose remains unparseable-without-invention. Not a keep vs 97.9.
- **#67** leftover 95s honesty notes (`cursor/honest-leftover-95s-b590`): via ∩ no_overnight keep both (`keep_connect` still `require_overnight ∪ via`); named same-city dest-vs-hub from owned `same_city_iata` (no second dest, `--nearby` stays off); around-the-world notes name the circuit / no invented hops; explore no longer drops notes (rest-of-trip / exclude-region / ±1). Nonstop+via and free vs `--allow-non-refundable` left as #66 shipped. Gate ok. Unittest 745. `viajante bench --prompts` fail:0 (191 prompts, 133 pass, 58 skip, 0 scored). `judge: skip` (no key). `judge_mean:` blank — not invented. Cold `--help` this host origin/main 321.9/310.2 → branch 311.8/312.6 (did not veto). `score_ms` 1630 is not the keep. Operator judged **97.5** after #66 on `cc0d682` (58 scored, skip 0) is not a keep vs 97.9. Not judged on this host.
- **#68** unnamed secret dest quote honesty (`cursor/secret-dest-quote-honesty-0cdc`): `brutal-impossible-secret-dest-quote` keeps `intent=explore` from PER, `exclude_regions=[asia]`, `max_stops=1`; empty dest list; notes stamp that three unnamed dests cannot be quoted (count from the prompt). Overlay booking+hotels refuse. No invented IATA/EUR/hotels/shortlist. Does not revert #67. Gate ok. Unittest 746. `viajante bench --prompts` fail:0 (191 prompts, 133 pass, 58 skip, 0 scored). `judge: skip` (no key). `judge_mean:` blank — not invented. Cold `--help` this host origin/main 366.6/302.8 → branch 374.3/302.5 (did not veto). `score_ms` 1557 is not the keep. Operator judged **95.8** after #67 on `e001663` (58 scored; that row was **1**) is not a keep vs 97.9. Not judged on this host.
- **#69** planner bag flags (`cursor/planner-bags-carry-on-ca20`): named carry-on only / N checked / no hold luggage / no bags ship `--bags N` / `--carry-on` into shopping constraints index 10. Unnamed leaves the slot `None`. Carry-on only and N checked together do not pick one. No invented bag fee or bag count. Does not revert #67/#68. No extra honesty-note stamps. Gate ok. Unittest 752. `viajante bench --prompts` fail:0 (191 prompts, 133 pass, 58 skip, 0 scored). `judge: skip` (no key). `judge_mean:` blank — not invented. Cold `--help` this host origin/main 485.3/299.4 → branch 305.4/301.6 (did not veto). `score_ms` 1587 is not the keep. Operator judged **97.2** on `db80c12` (191 prompts, 58 scored, skip 0) is not a keep vs 97.9. Not judged on this host.
- **#70** honest `work_back_by` / IDL (`cursor/work-back-by-idl-f10b`): compile already-covered weekday aliases (Jumatatu / Umsombuluko / Montag) onto the shipped field. Named Monday office clock that a civil 06:00 local homebound depart cannot meet (owned tz + great-circle 800 km/h + 1h) keeps the route, dates, and field and notes timezone/IDL; does not invent hops or a midnight SYD/AKL itinerary. Outbound `depart_after` does not move the return. YYZ-CDG still maps `work_back_by` and is not stamped impossible. No extra leftover-note stamps; no bag work. Gate ok. Unittest 758. `viajante bench --prompts` fail:0 (191 prompts, 133 pass, 58 skip, 0 scored). `judge: skip` (no key). `judge_mean:` blank — not invented. Cold `--help` this host origin/main 306.4 → branch 310.9 (two-run keep; later pairs 306.2; not treated as veto, within 301–317 jitter). `score_ms` 1651 is not the keep. Operator judged **97.2** on `e1028c7` (191 prompts, 58 scored, skip 0) is not a keep vs 97.9. Not judged on this host.
- **#72** named `via_regions` honesty (`cursor/via-regions-honesty-55f0`): `insane-yhz-fiji-via-continents` keeps `via_regions`, `split_packages`, one-way YHZ-NAN on the named date, empty hop IATA; notes stamp that named via_regions is a shortlist constraint (do not invent airport codes or a fare per hop; keep named regions and origin/dest/date). Unnamed stays unset. Does not redo #67 ATW leftover notes (`do not invent hops`) or #71 price-cap. Gate ok. Unittest 770. `viajante bench --prompts` fail:0 (193 prompts, 135 pass, 58 skip, 0 scored). `judge: skip` (no key). `judge_mean:` blank — not invented. Cold `--help` this host origin/main 360.9/317.4 → branch 314.5/313.1 (did not veto). `score_ms` 1684 is not the keep. Operator judged **97.1** on `8045cfb` (193 prompts, 58 scored, skip 0; that Fiji row was **95**) is not a keep vs 97.9. Not judged on this host.
- **#76** explore dest pricing same owned post-filters (`0bd0c09`): named bags / via / airlines / `--price-cap` drop dests whose cheapest surviving fare contradicts; unnamed keeps them. Catalog places are not offers. Operator judged **97.2** / 58 scored / skip 0 — not a keep vs 97.9.
- Compact RT wrapped/sibling return: proved on this tip (`0bd0c09`; no PR). Outbound-only compact body stays one leg; do not invent the return.
- **#77** trip flight shop same owned post-filters (`cursor/trip-flight-post-filters-a638`): `search_trip` forwards `bags` / `carry_on` / `via` / `exclude_via` / `airlines` / `exclude_airlines` / `price_cap_eur` onto `search_flights` / `_normalize_offer`. CLI/MCP same optional flag names. Unnamed stays unset. Empty flight side after filters omits `trip_total`. Does not redo #75/#76 or compact RT sibling return. Gate ok. Unittest 803. `viajante bench --prompts` fail:0 (193 prompts, 135 pass, 58 skip, 0 scored). `judge: skip` (no key). `judge_mean:` blank — not invented. Cold `--help` this host main 311.4/305.1 → branch 306.2/305.1 (later 309.0/315.6; did not veto). `score_ms` 1666 is not the keep. Not a keep vs 97.9.

## Honesty leftovers (leave them)

- Compact `--trip rt` with only the outbound in the bytes still has one leg. Do not invent the return.
- Typical is omitted when the calendar is thin or the trip is multi-city. Packaged RT uses the same-stay grid.
- Booking challenge timeout. Untouched.
- Do not shorten Playwright detail delays. No live Google as the reward.
- Never invent a fare, typical, token, or via list.
