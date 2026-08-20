# Holdout prompts

Operator overfitting check. Not in the weekday 90.

`viajante bench --prompts` loads smoke → insane via `manifest.json`. This
file is **not** listed there. After a quality PR, the operator runs:

```bash
uv run viajante bench --prompts --holdout
```

Mean 1–100 on holdout is the check that a quality change did not overfit
the frozen weekday battery. Looping speed agents read `program.md` and
`bench-history.md` only; they must not open `holdout.jsonl` to pick work.

Do not add these rows to `manifest.json`. Do not rewrite them easier.
