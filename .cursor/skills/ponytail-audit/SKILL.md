---
name: ponytail-audit
description: >
 Whole-repo audit for over-engineering. Ranked list of what to delete, simplify,
 or replace with stdlib/native equivalents. Use when the user says "audit this
 codebase", "audit for over-engineering", "what can I delete from this repo",
 "find bloat", "ponytail-audit", or "/ponytail-audit".
---

Scan the whole tree. Rank findings biggest cut first.

Tags: `delete:`, `stdlib:`, `native:`, `yagni:`, `shrink:`.

Output one line per finding: `<tag> <what to cut>. <replacement>. [path]`.
End with `net: -<N> lines, -<M> deps possible.`
Nothing to cut: `Lean already. Ship.`

Do not delete `tests/prompts/`, JSON contract keys, or fetch pacing.
