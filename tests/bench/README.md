# Bench fixture corpus

Checked-in copies of the owned compact-shopping / `wrb.fr` / HTML card
fixtures built by `tests/test_google_flights.py`. The bench parses every
file listed in `manifest.json`. These are the real owned bytes, not a
network capture.

Required names, per-file floors, and `MIN_*` live in
`src/viajante/bench.py`. Do not add empty files to “win”. Do not drop a
name from the manifest. Do not shrink a file below those floors. New
fixtures belong here only when they are real owned parse cases already
covered by tests.
