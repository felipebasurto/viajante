"""Graded prompt battery: quality contract, not the keep-or-revert score.

`viajante bench --prompts` (or VIAJANTE_BENCH_PROMPTS=1) loads the checked-in
corpus in tests/prompts/, runs the owned prompt→query planner, and checks
deterministic contracts offline. LLM-as-judge is opt-in via
VIAJANTE_BENCH_JUDGE=1, scores quality 1–100 with DeepSeek (`deepseek-chat`),
and is never folded into score_ms. The API key lives outside the repo
(DEEPSEEK_API_KEY, or VIAJANTE_JUDGE_KEY as override). Unset key →
judge: skip; never invent a score.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from viajante.bench import LIVE_ENV, repo_root
from viajante.prompt_plan import PromptPlan, plan_prompt

PROMPTS_ENV = "VIAJANTE_BENCH_PROMPTS"
JUDGE_ENV = "VIAJANTE_BENCH_JUDGE"
JUDGE_KEY_ENV = "DEEPSEEK_API_KEY"
JUDGE_KEY_OVERRIDE_ENV = "VIAJANTE_JUDGE_KEY"
JUDGE_MODEL_ENV = "DEEPSEEK_MODEL"
JUDGE_MODEL_OVERRIDE_ENV = "VIAJANTE_JUDGE_MODEL"
DEFAULT_JUDGE_MODEL = "deepseek-chat"
JUDGE_URL = "https://api.deepseek.com/v1/chat/completions"
MANIFEST_NAME = "manifest.json"

MIN_PROMPT_CASES = 80
MIN_TIER_CASES = {
    "smoke": 10,
    "easy": 16,
    "medium": 20,
    "hard": 20,
    "insane": 8,
}
MIN_INSANE = MIN_TIER_CASES["insane"]
MIN_UNIQUE_ORIGINS = 12
REQUIRED_PROMPT_FILES = (
    "smoke.jsonl",
    "easy.jsonl",
    "medium.jsonl",
    "hard.jsonl",
    "insane.jsonl",
)
REQUIRED_PROMPT_IDS = frozenset(
    {
        "insane-yhz-fiji-via-continents",
        "smoke-bos-lhr-one-date",
        "smoke-invalid-iata-xxx",
        "smoke-missing-date-bos-lhr",
        "hard-refuse-booking",
        "hard-refuse-trains",
        "hard-refuse-cars",
        "insane-eight-adults-one-room",
        "insane-contradictory-dates",
    }
)
TIER_ORDER = ("smoke", "easy", "medium", "hard", "insane")


class PromptCorpusError(RuntimeError):
    """Prompt corpus was emptied, dropped, or is missing required cases."""


@dataclass(frozen=True)
class PromptCase:
    id: str
    tier: str
    prompt: str
    judge: str
    expect: dict[str, Any]
    lang: str = ""
    path: Optional[Path] = None

    @property
    def is_llm(self) -> bool:
        return self.judge == "llm"


@dataclass(frozen=True)
class PromptRunResult:
    case: PromptCase
    status: str
    elapsed_ms: int
    reason: str
    score_1_100: Optional[int] = None


@dataclass(frozen=True)
class JudgeResult:
    score_1_100: Optional[int]
    reason: str

    @property
    def skipped(self) -> bool:
        return self.score_1_100 is None


def prompts_dir(root: Optional[Path] = None) -> Path:
    return (root or repo_root()) / "tests" / "prompts"


def load_manifest(root: Optional[Path] = None) -> dict[str, Any]:
    path = prompts_dir(root) / MANIFEST_NAME
    if not path.is_file():
        raise PromptCorpusError(f"missing {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise PromptCorpusError("prompt manifest must be a JSON object")
    return data


def _parse_case(raw: object, *, path: Path, line_no: int) -> PromptCase:
    if not isinstance(raw, dict):
        raise PromptCorpusError(f"{path.name}:{line_no} must be a JSON object")
    ident = raw.get("id")
    tier = raw.get("tier")
    prompt = raw.get("prompt")
    judge = raw.get("judge")
    expect = raw.get("expect")
    if not isinstance(ident, str) or not ident.strip():
        raise PromptCorpusError(f"{path.name}:{line_no} missing id")
    if tier not in TIER_ORDER:
        raise PromptCorpusError(f"{path.name}:{line_no} invalid tier {tier!r}")
    if not isinstance(prompt, str) or not prompt.strip():
        raise PromptCorpusError(f"{path.name}:{line_no} empty prompt is forbidden")
    if judge not in {"deterministic", "llm"}:
        raise PromptCorpusError(f"{path.name}:{line_no} invalid judge {judge!r}")
    if not isinstance(expect, dict) or not expect:
        raise PromptCorpusError(f"{path.name}:{line_no} expect must be a non-empty object")
    lang = raw.get("lang") if isinstance(raw.get("lang"), str) else ""
    return PromptCase(
        id=ident,
        tier=tier,
        prompt=prompt,
        judge=judge,
        expect=expect,
        lang=lang,
        path=path,
    )


def load_prompt_cases(root: Optional[Path] = None) -> list[PromptCase]:
    directory = prompts_dir(root)
    data = load_manifest(root)
    files = data.get("files")
    if not isinstance(files, list) or not files:
        raise PromptCorpusError("prompt manifest files must be a non-empty list")
    names: list[str] = []
    for row in files:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise PromptCorpusError("each prompt file entry needs name")
        names.append(row["name"])
    listed = set(names)
    missing = set(REQUIRED_PROMPT_FILES) - listed
    if missing:
        raise PromptCorpusError(f"prompt manifest dropped required files: {sorted(missing)}")
    extra = listed - set(REQUIRED_PROMPT_FILES)
    if extra:
        raise PromptCorpusError(f"prompt manifest added undeclared files: {sorted(extra)}")

    cases: list[PromptCase] = []
    seen: set[str] = set()
    for name in names:
        path = directory / name
        if not path.is_file():
            raise PromptCorpusError(f"dropped prompt file: {name}")
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            raise PromptCorpusError(f"empty prompt file is forbidden: {name}")
        file_count = 0
        for line_no, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            row = _parse_case(json.loads(line), path=path, line_no=line_no)
            if row.id in seen:
                raise PromptCorpusError(f"duplicate prompt id: {row.id}")
            seen.add(row.id)
            cases.append(row)
            file_count += 1
        if file_count < 1:
            raise PromptCorpusError(f"empty prompt file is forbidden: {name}")
    return cases


def validate_prompt_corpus(root: Optional[Path] = None) -> list[PromptCase]:
    cases = load_prompt_cases(root)
    if len(cases) < MIN_PROMPT_CASES:
        raise PromptCorpusError(
            f"prompt corpus shrank below {MIN_PROMPT_CASES} cases ({len(cases)})"
        )
    missing_ids = REQUIRED_PROMPT_IDS - {row.id for row in cases}
    if missing_ids:
        raise PromptCorpusError(f"prompt corpus dropped required ids: {sorted(missing_ids)}")
    by_tier: dict[str, int] = {tier: 0 for tier in TIER_ORDER}
    origins: list[str] = []
    for row in cases:
        by_tier[row.tier] += 1
        if row.lang != "en":
            raise PromptCorpusError(f"{row.id} must be English (lang=en)")
        origin = row.expect.get("origin")
        if isinstance(origin, str) and len(origin) == 3:
            origins.append(origin)
        if row.judge == "deterministic":
            intent = row.expect.get("intent")
            if intent in {"flights", "dates", "explore"}:
                origin = row.expect.get("origin")
                if not isinstance(origin, str) or len(origin) != 3 or origin != origin.upper():
                    raise PromptCorpusError(
                        f"{row.id} deterministic {intent} case needs expected origin IATA"
                    )
            if intent == "refuse":
                refuse = row.expect.get("refuse")
                if not refuse:
                    raise PromptCorpusError(f"{row.id} refuse case needs expected refuse reasons")
    for tier, floor in MIN_TIER_CASES.items():
        if by_tier[tier] < floor:
            raise PromptCorpusError(
                f"{tier} prompt tier shrank below {floor} cases ({by_tier[tier]})"
            )
    unique_origins = set(origins)
    if len(unique_origins) < MIN_UNIQUE_ORIGINS:
        raise PromptCorpusError(
            f"prompt origins shrank below {MIN_UNIQUE_ORIGINS} IATA codes ({len(unique_origins)})"
        )
    if origins:
        _code, top_count = Counter(origins).most_common(1)[0]
        if top_count / len(origins) > 0.2:
            raise PromptCorpusError(
                f"one origin dominates the prompt battery ({_code} in {top_count}/{len(origins)})"
            )
    ranks = {tier: index for index, tier in enumerate(TIER_ORDER)}
    previous = -1
    for row in cases:
        rank = ranks[row.tier]
        if rank < previous:
            raise PromptCorpusError("prompt corpus must be ordered smoke → insane")
        previous = rank
    return cases


def _ms_since(started: float) -> int:
    return max(0, int(round((time.perf_counter() - started) * 1000)))


def _env_value(*names: str) -> Optional[str]:
    for name in names:
        raw = os.environ.get(name)
        if raw is None:
            continue
        value = raw.strip()
        if value:
            return value
    return None


def _judge_api_key() -> Optional[str]:
    return _env_value(JUDGE_KEY_OVERRIDE_ENV, JUDGE_KEY_ENV)


def _judge_model() -> str:
    return _env_value(JUDGE_MODEL_OVERRIDE_ENV, JUDGE_MODEL_ENV) or DEFAULT_JUDGE_MODEL


def parse_score_1_100(raw: object) -> Optional[int]:
    """Accept an integer 1–100 only. Never coerce pass/fail or invent a score."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        score = raw
    elif isinstance(raw, float) and raw.is_integer():
        score = int(raw)
    elif isinstance(raw, str) and raw.strip().isdigit():
        score = int(raw.strip())
    else:
        return None
    if 1 <= score <= 100:
        return score
    return None


def parse_judge_verdict(payload: object) -> JudgeResult:
    """Parse a DeepSeek chat payload into score_1_100 + one-line reason."""
    skip = JudgeResult(None, "judge: skip (malformed verdict)")
    if not isinstance(payload, dict):
        return skip
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, TypeError, IndexError):
        return skip
    if not isinstance(content, str) or not content.strip():
        return skip
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        verdict = json.loads(text)
    except json.JSONDecodeError:
        return skip
    if not isinstance(verdict, dict):
        return skip
    score = parse_score_1_100(verdict.get("score_1_100"))
    if score is None:
        return skip
    reason = str(verdict.get("reason") or "").strip()
    if not reason:
        return skip
    return JudgeResult(score, reason.splitlines()[0][:200])


def _skip_judge() -> JudgeResult:
    return JudgeResult(None, "judge: skip")


def judge_case(case: PromptCase, plan: PromptPlan) -> JudgeResult:
    """LLM-as-judge. Scores 1–100 or skips. Never invents a score."""
    if os.environ.get(JUDGE_ENV) != "1":
        return _skip_judge()
    key = _judge_api_key()
    if not key:
        return _skip_judge()
    body = {
        "model": _judge_model(),
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "You score a travel-search planner from 1 to 100. "
                    "There is NO single correct answer. "
                    "Score quality: follows constraints, sane routing, "
                    "honest about what viajante can and cannot do, "
                    "does not invent bookings or prices. "
                    "viajante searches flights and hotels; it does not book. "
                    "Madrid is not the implied home hub. "
                    'Reply JSON {"score_1_100": <integer 1-100>, "reason": "<one line>"}.'
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "id": case.id,
                        "prompt": case.prompt,
                        "expect": case.expect,
                        "plan": plan.to_dict(),
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    }
    request = urllib.request.Request(
        JUDGE_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
        return JudgeResult(None, "judge: skip (request failed)")
    return parse_judge_verdict(payload)


def _evaluate_case(case: PromptCase) -> PromptRunResult:
    started = time.perf_counter()
    plan = plan_prompt(case.prompt)
    elapsed = _ms_since(started)
    if case.judge == "deterministic":
        ok, reason = plan.matches(case.expect)
        return PromptRunResult(
            case=case,
            status="pass" if ok else "fail",
            elapsed_ms=elapsed,
            reason=reason,
        )
    if os.environ.get(JUDGE_ENV) != "1":
        return PromptRunResult(
            case=case,
            status="skip",
            elapsed_ms=elapsed,
            reason="judge: skip",
        )
    judged = judge_case(case, plan)
    if judged.skipped:
        return PromptRunResult(
            case=case,
            status="skip",
            elapsed_ms=elapsed,
            reason=judged.reason,
        )
    return PromptRunResult(
        case=case,
        status="scored",
        elapsed_ms=elapsed,
        reason=judged.reason,
        score_1_100=judged.score_1_100,
    )


def _format_case_line(row: PromptRunResult) -> str:
    if row.score_1_100 is not None:
        return (
            f"{row.case.id}  {row.status}  {row.elapsed_ms}ms  "
            f"score_1_100={row.score_1_100}  {row.reason}"
        )
    return f"{row.case.id}  {row.status}  {row.elapsed_ms}ms  {row.reason}"


def format_prompt_report(
    *,
    cases: Sequence[PromptCase],
    results: Sequence[PromptRunResult],
    wall_ms: int,
) -> str:
    counts = {"pass": 0, "fail": 0, "skip": 0, "scored": 0}
    scores: list[int] = []
    for row in results:
        counts[row.status] = counts.get(row.status, 0) + 1
        if row.status == "scored" and row.score_1_100 is not None:
            scores.append(row.score_1_100)
    judged = os.environ.get(JUDGE_ENV) == "1" and _judge_api_key() is not None and scores
    lines = [
        f"prompts: {len(cases)}",
        f"pass: {counts['pass']}",
        f"fail: {counts['fail']}",
        f"skip: {counts['skip']}",
        f"scored: {counts['scored']}",
        f"wall_ms: {wall_ms}",
        f"judge: {'ran' if judged else 'skip'}",
    ]
    return "\n".join(lines) + "\n"


def run_prompt_bench(*, root: Optional[Path] = None) -> int:
    if os.environ.get(LIVE_ENV) == "1":
        print(
            "note: prompt battery does not scrape; VIAJANTE_BENCH_LIVE is ignored",
            file=sys.stderr,
        )
    try:
        cases = validate_prompt_corpus(root)
    except PromptCorpusError as exc:
        print("prompts: fail", flush=True)
        print(f"error: {exc}", file=sys.stderr)
        return 1

    started = time.perf_counter()
    results: list[PromptRunResult] = []
    for case in cases:
        row = _evaluate_case(case)
        results.append(row)
        print(_format_case_line(row), file=sys.stderr)
    wall_ms = _ms_since(started)
    print(format_prompt_report(cases=cases, results=results, wall_ms=wall_ms), end="")
    if any(row.status == "fail" for row in results):
        return 1
    return 0
