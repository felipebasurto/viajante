"""Graded prompt battery: quality contract, not the keep-or-revert score.

`viajante bench --prompts` (or VIAJANTE_BENCH_PROMPTS=1) loads the weekday
corpus via tests/prompts/manifest.json (smoke → insane). `--holdout` loads
only holdout.jsonl, which is not in that manifest. Deterministic contracts
are offline. LLM-as-judge is opt-in via VIAJANTE_BENCH_JUDGE=1, scores
quality 1–100 with DeepSeek (`deepseek-chat`), and is never folded into
score_ms. The API key lives outside the repo (DEEPSEEK_API_KEY, or
VIAJANTE_JUDGE_KEY as override). Unset key → judge: skip; never invent a
score.

Harness priorities:
1. Honesty: invented price or route = 0 (deterministic; no DeepSeek, no live search).
2. Named contract (IATA, dates, dests the user said, refuse rest-of-trip).
3. score_ms is a different loop.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from viajante.airports import is_known_iata
from viajante.bench import LIVE_ENV, repo_root
from viajante.prompt_plan import (
    PromptPlan,
    _city_pairs_from_text,
    _iata_pairs,
    _iter_city_iata,
    plan_prompt,
)

PROMPTS_ENV = "VIAJANTE_BENCH_PROMPTS"
HOLDOUT_NAME = "holdout.jsonl"
HOLDOUT_TIER = "holdout"
JUDGE_ENV = "VIAJANTE_BENCH_JUDGE"
JUDGE_KEY_ENV = "DEEPSEEK_API_KEY"
JUDGE_KEY_OVERRIDE_ENV = "VIAJANTE_JUDGE_KEY"
JUDGE_MODEL_ENV = "DEEPSEEK_MODEL"
JUDGE_MODEL_OVERRIDE_ENV = "VIAJANTE_JUDGE_MODEL"
DEFAULT_JUDGE_MODEL = "deepseek-chat"
JUDGE_URL = "https://api.deepseek.com/v1/chat/completions"
MANIFEST_NAME = "manifest.json"
JUDGE_SYSTEM_PROMPT = (
    "You score a travel-search planner from 1 to 100. "
    "There is NO single correct answer.\n"
    "HARD SCORING LAW — do not miss this:\n"
    "- Inventing a price, fare, EUR amount, booking, or a destination/route "
    "that the user did not name is AUTOMATIC 0. Not a 15-point ding. Zero.\n"
    "- A price_cap or via_regions the user stated is a constraint, not an invention.\n"
    "- Extra route_specs / city pairs the user did not name are AUTOMATIC 0.\n"
    "- Omitting prices, fare estimates, destination shortlists, and concrete "
    "routes is CORRECT and must not lower the score. Do not deduct for lacking "
    "explicit pricing, a destination shortlist, a concrete route, or fare estimates.\n"
    "- viajante searches flights and hotels; it does not book. Saying that is "
    "optional, not required for a score of 90 or above. Do not deduct for not "
    "stating that viajante does not book.\n"
    "- Madrid is not the implied home hub. No city is the default origin unless "
    "the prompt names one.\n"
    "The harness already zeros invented prices/routes. You write a 1–100 only "
    "when the plan invented nothing.\n"
    "Score quality: follows the named contract (IATA, dates, dests the user said, "
    "refuse rest-of-trip), sane routing, honest about what viajante can and cannot do.\n"
    'Reply JSON {"score_1_100": <integer 1-100>, "reason": "<one line>"}.'
)

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
VALID_TIERS = (*TIER_ORDER, HOLDOUT_TIER)
MIN_HOLDOUT_CASES = 8
MAX_HOLDOUT_CASES = 12


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
    plan: Optional[PromptPlan] = None


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
    if tier not in VALID_TIERS:
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


def _load_jsonl_file(path: Path, *, seen: set[str]) -> list[PromptCase]:
    if not path.is_file():
        raise PromptCorpusError(f"dropped prompt file: {path.name}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise PromptCorpusError(f"empty prompt file is forbidden: {path.name}")
    cases: list[PromptCase] = []
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
        raise PromptCorpusError(f"empty prompt file is forbidden: {path.name}")
    return cases


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
    if HOLDOUT_NAME in listed:
        raise PromptCorpusError("holdout.jsonl must not be in the weekday prompt manifest")
    extra = listed - set(REQUIRED_PROMPT_FILES)
    if extra:
        raise PromptCorpusError(f"prompt manifest added undeclared files: {sorted(extra)}")

    cases: list[PromptCase] = []
    seen: set[str] = set()
    for name in names:
        cases.extend(_load_jsonl_file(directory / name, seen=seen))
    return cases


def load_holdout_cases(root: Optional[Path] = None) -> list[PromptCase]:
    """Operator overfitting set. Not part of the weekday 90."""
    path = prompts_dir(root) / HOLDOUT_NAME
    cases = _load_jsonl_file(path, seen=set())
    if not MIN_HOLDOUT_CASES <= len(cases) <= MAX_HOLDOUT_CASES:
        raise PromptCorpusError(
            f"holdout must have {MIN_HOLDOUT_CASES}-{MAX_HOLDOUT_CASES} cases ({len(cases)})"
        )
    for row in cases:
        if not row.id.startswith("holdout-"):
            raise PromptCorpusError(f"{row.id} must use holdout- prefix")
        if row.tier != HOLDOUT_TIER:
            raise PromptCorpusError(f"{row.id} must have tier={HOLDOUT_TIER}")
        if row.lang != "en":
            raise PromptCorpusError(f"{row.id} must be English (lang=en)")
    return cases


def validate_prompt_corpus(root: Optional[Path] = None) -> list[PromptCase]:
    cases = load_prompt_cases(root)
    if any(row.tier == HOLDOUT_TIER or row.id.startswith("holdout-") for row in cases):
        raise PromptCorpusError("holdout cases must not be in the weekday prompt battery")
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


@dataclass(frozen=True)
class _JudgeHttpRequest:
    full_url: str
    data: bytes
    headers: Mapping[str, str]


def _judge_urlopen(request: _JudgeHttpRequest, timeout: int = 30) -> Any:
    # Live DeepSeek only. Unittest patches this hook so urllib.request stays cold.
    import urllib.request

    req = urllib.request.Request(
        request.full_url,
        data=request.data,
        headers=dict(request.headers),
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout)


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


def compact_plan_dict(plan: PromptPlan) -> dict[str, Any]:
    """Drop empty planner fields so a scored log line stays one-line readable."""
    compact: dict[str, Any] = {}
    for key, value in plan.to_dict().items():
        if value is None or value is False or value == "" or value == []:
            continue
        compact[key] = value
    return compact


_EUR_AMOUNT = re.compile(
    r"€\s*(\d+(?:\.\d+)?)|"
    r"(\d+(?:\.\d+)?)\s*€|"
    r"(\d+(?:\.\d+)?)\s*(?:eur|euros)\b|"
    r"(?:fare|price)\s*(?:of|:)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_SPEC_PAIR = re.compile(r"\b([A-Z]{3})-([A-Z]{3})\b")


def _eur_amounts(text: str) -> set[int]:
    amounts: set[int] = set()
    for match in _EUR_AMOUNT.finditer(text):
        for group in match.groups():
            if group is None:
                continue
            amounts.add(int(float(group)))
    return amounts


_IATA_TOKEN_UPPER = re.compile(r"\b([A-Z]{3})\b")


def _named_iatas(prompt: str) -> set[str]:
    named: set[str] = set()
    for token in _IATA_TOKEN_UPPER.findall(prompt):
        if is_known_iata(token):
            named.add(token)
    folded = prompt.casefold()
    named.update(code for _alias, code in _iter_city_iata(folded))
    return named


def _named_pairs(prompt: str) -> set[tuple[str, str]]:
    pairs = set(_iata_pairs(prompt))
    pairs.update(_city_pairs_from_text(prompt))
    return pairs


def _spec_pair(spec: str) -> Optional[tuple[str, str]]:
    match = _SPEC_PAIR.search(spec.upper())
    if match is None:
        return None
    return match.group(1), match.group(2)


def invention_reason(prompt: str, plan: PromptPlan) -> Optional[str]:
    """Return a one-line reason if the plan invented a price or route.

    Grounded only in the user prompt. Does not scrape and does not need real fares.
    price_cap / via_regions the user named are constraints, not invention.
    Omitting prices and routes is not invention.
    """
    prompt_amounts = _eur_amounts(prompt)
    plan_amounts = _eur_amounts(plan.notes)
    if plan.price_cap_eur is not None:
        plan_amounts.add(int(plan.price_cap_eur))
    extra_amounts = plan_amounts - prompt_amounts
    if extra_amounts:
        amount = sorted(extra_amounts)[0]
        return f"invented price ({amount} EUR)"

    named_iata = _named_iatas(prompt)
    named_pairs = _named_pairs(prompt)
    plan_codes: list[str] = []
    if isinstance(plan.origin, str):
        plan_codes.append(plan.origin)
    if isinstance(plan.destination, str):
        plan_codes.append(plan.destination)
    for spec in plan.route_specs:
        pair = _spec_pair(spec)
        if pair is not None:
            plan_codes.extend(pair)
    extra_codes = [code for code in plan_codes if is_known_iata(code) and code not in named_iata]
    if extra_codes:
        return f"invented route ({extra_codes[0]})"

    primary = None
    if plan.origin and plan.destination:
        primary = (plan.origin, plan.destination)
    for spec in plan.route_specs:
        pair = _spec_pair(spec)
        if pair is None:
            continue
        origin, dest = pair
        if (origin, dest) in named_pairs or (dest, origin) in named_pairs:
            continue
        if primary is not None and pair in {primary, (primary[1], primary[0])}:
            continue
        return f"invented route ({origin}-{dest})"
    return None


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
    """Score a plan. Invented price/route is 0 without calling DeepSeek.

    Harness priorities:
    1. Honesty: invented price or route = 0.
    2. Named contract (IATA, dates, dests the user said, refuse rest-of-trip).
    3. score_ms is a different loop.
    """
    invented = invention_reason(case.prompt, plan)
    if invented is not None:
        return JudgeResult(0, invented)
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
                "content": JUDGE_SYSTEM_PROMPT,
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
    request = _JudgeHttpRequest(
        full_url=JUDGE_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with _judge_urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
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
            plan=None if ok else plan,
        )
    invented = invention_reason(case.prompt, plan)
    if invented is not None:
        return PromptRunResult(
            case=case,
            status="scored",
            elapsed_ms=elapsed,
            reason=invented,
            score_1_100=0,
            plan=plan,
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
        plan=plan,
    )


def _format_case_line(row: PromptRunResult) -> str:
    if row.score_1_100 is not None:
        line = (
            f"{row.case.id}  {row.status}  {row.elapsed_ms}ms  "
            f"score_1_100={row.score_1_100}  {row.reason}"
        )
    else:
        line = f"{row.case.id}  {row.status}  {row.elapsed_ms}ms  {row.reason}"
    if row.status in {"scored", "fail"} and row.plan is not None:
        plan_json = json.dumps(
            compact_plan_dict(row.plan),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        line = f"{line}  plan={plan_json}"
    return line


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


def run_prompt_bench(*, root: Optional[Path] = None, holdout: bool = False) -> int:
    if os.environ.get(LIVE_ENV) == "1":
        print(
            "note: prompt battery does not scrape; VIAJANTE_BENCH_LIVE is ignored",
            file=sys.stderr,
        )
    try:
        cases = load_holdout_cases(root) if holdout else validate_prompt_corpus(root)
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
