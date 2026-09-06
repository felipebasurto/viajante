"""Graded prompt battery: quality contract, not the keep-or-revert score.

`viajante bench --prompts` (or VIAJANTE_BENCH_PROMPTS=1) loads the weekday
corpus via tests/prompts/manifest.json (smoke → savage). `--holdout` loads
only holdout.jsonl, which is not in that manifest. Deterministic contracts
are offline. LLM-as-judge is opt-in via VIAJANTE_BENCH_JUDGE=1, scores
quality 1–100 with DeepSeek (`deepseek-chat`), and is never folded into
score_ms. The API key lives outside the repo (DEEPSEEK_API_KEY, or
VIAJANTE_JUDGE_KEY as override). Unset key → judge: skip; never invent a
score.

Each weekday row also prints `plan_ms` (wall ms of `plan_prompt` plus the
cheap owned parse that row already does). Summary prints `plan_p50_ms` /
`plan_p90_ms` / `plan_max_ms`. That is not `score_ms` and not `judge_mean`.

Optional live find-flights timer: VIAJANTE_BENCH_SWEEP=1 (or `--timeit-sweep`).
Off by default. Caps at the first MAX_SWEEP_PROMPTS planned IATA+date flight
queries and uses the MCP `search_flights` HTTP sweep path (fetch=sweep, no
Playwright). Live Google is never the keep/revert number. Sweep off →
`sweep_ms:` blank, like `judge_mean:` when the judge skipped.

Harness priorities:
1. Honesty: invented price or route = 0 (deterministic; no DeepSeek, no live search).
2. Named contract (IATA, dates, dests the user said, refuse rest-of-trip,
   packaged RT vs two one-ways).
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
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from viajante.airports import is_known_iata
from viajante.bench import LIVE_ENV, repo_root
from viajante.mcp_handlers import search_flights_tool
from viajante.prompt_plan import (
    PromptPlan,
    _city_pairs_from_text,
    _iata_pairs,
    _iter_city_iata,
    asked_two_one_ways,
    plan_prompt,
    wants_packaged_rt,
)

PROMPTS_ENV = "VIAJANTE_BENCH_PROMPTS"
HOLDOUT_NAME = "holdout.jsonl"
HOLDOUT_TIER = "holdout"
SWEEP_ENV = "VIAJANTE_BENCH_SWEEP"
# Frozen planner calendar for this battery. Named corpus dates start 2026-09-01.
# Production CLI/MCP omit today= and still reject past dates with date.today().
PROMPT_BENCH_TODAY = date(2026, 8, 20)
MAX_SWEEP_PROMPTS = 8
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
    "- If the prompt names outbound and return dates and does NOT ask for "
    "two one-ways / without --trip / separate tickets, a plan that is two "
    "one-ways, sugar one-ways, or missing trip=rt is AUTOMATIC 0 (same as "
    "inventing a fare). Say the plan split a packaged RT.\n"
    "- If the prompt explicitly asks for two one-ways / without --trip rt / "
    "separate tickets, a plan that packages --trip rt is AUTOMATIC 0.\n"
    "- Quoting one-way prices as if they were the RT package, or inventing a "
    "package total, is AUTOMATIC 0.\n"
    "- Omitting prices, fare estimates, destination shortlists, and concrete "
    "routes is CORRECT and must not lower the score. Do not deduct for lacking "
    "explicit pricing, a destination shortlist, a concrete route, or fare estimates.\n"
    "- viajante searches flights and hotels; it does not book. Saying that is "
    "optional, not required for a score of 90 or above. Do not deduct for not "
    "stating that viajante does not book.\n"
    "- Madrid is not the implied home hub. No city is the default origin unless "
    "the prompt names one.\n"
    "The harness already zeros invented prices/routes and packaged-RT splits. "
    "You write a 1–100 only when the plan invented nothing and did not split "
    "or mis-package the RT.\n"
    "Score quality: follows the named contract (IATA, dates, dests the user said, "
    "refuse rest-of-trip, packaged RT vs two one-ways), sane routing, honest "
    "about what viajante can and cannot do.\n"
    'Reply JSON {"score_1_100": <integer 1-100>, "reason": "<one line>"}.'
)

MIN_PROMPT_CASES = 120
MIN_TIER_CASES = {
    "smoke": 10,
    "easy": 16,
    "medium": 20,
    "hard": 20,
    "insane": 8,
    "brutal": 25,
    "savage": 30,
}
MIN_INSANE = MIN_TIER_CASES["insane"]
MIN_UNIQUE_ORIGINS = 12
REQUIRED_PROMPT_FILES = (
    "smoke.jsonl",
    "easy.jsonl",
    "medium.jsonl",
    "hard.jsonl",
    "insane.jsonl",
    "brutal.jsonl",
    "i18n.jsonl",
    "savage.jsonl",
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
        "savage-yo-los-jnb-rt-hotel",
    }
)
TIER_ORDER = ("smoke", "easy", "medium", "hard", "insane", "brutal", "savage")
VALID_TIERS = (*TIER_ORDER, HOLDOUT_TIER)
REQUIRED_SAVAGE_LANGS = frozenset(
    {
        "yo",
        "sw",
        "am",
        "ka",
        "eu",
        "is",
        "km",
        "ta",
        "qu",
        "cy",
        "zu",
        "mn",
    }
)
VALID_SAVAGE_LANGS = REQUIRED_SAVAGE_LANGS | {"ar", "de", "en", "fr", "ja", "ko", "pt"}
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
    sweep_ms: Optional[int] = None


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
        if not row.lang:
            raise PromptCorpusError(f"{row.id} missing lang")
        if row.lang != "en" and row.expect.get("locale") != "en":
            raise PromptCorpusError(f"{row.id} non-English prompt must plan English fetch locale")
        origin = row.expect.get("origin")
        if isinstance(origin, str) and len(origin) == 3:
            origins.append(origin)
        if row.tier == "savage":
            if not row.id.startswith("savage-"):
                raise PromptCorpusError(f"{row.id} must use savage- prefix")
            if row.lang not in VALID_SAVAGE_LANGS:
                raise PromptCorpusError(f"{row.id} invalid savage lang {row.lang!r}")
        elif row.tier in {"smoke", "easy", "medium", "hard", "insane"} and row.lang != "en":
            raise PromptCorpusError(f"{row.id} must be English (lang=en)")
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
    savage_langs = {row.lang for row in cases if row.tier == "savage"}
    missing_langs = REQUIRED_SAVAGE_LANGS - savage_langs
    if missing_langs:
        raise PromptCorpusError(
            f"savage prompt slice missing required langs: {sorted(missing_langs)}"
        )
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
            raise PromptCorpusError("prompt corpus must be ordered smoke → savage")
        previous = rank
    return cases


def _clock() -> float:
    return time.perf_counter()


def _ms_since(started: float) -> int:
    return max(0, int(round((_clock() - started) * 1000)))


def percentile_ms(values: Sequence[int], p: float) -> Optional[int]:
    """Nearest-rank percentile. None when there are no samples (never invent)."""
    if not values:
        return None
    if p < 0 or p > 100:
        raise ValueError("percentile must be in 0..100")
    ordered = sorted(int(item) for item in values)
    index = int(round((p / 100.0) * (len(ordered) - 1)))
    return ordered[index]


def _fmt_optional_ms(key: str, value: Optional[int]) -> str:
    if value is None:
        return f"{key}:"
    return f"{key}: {value}"


def sweep_routes_for_plan(
    plan: PromptPlan, *, today: Optional[date] = None
) -> Optional[tuple[str, ...]]:
    """Owned route specs for a live sweep, or None when the plan is not a pair+date."""
    if plan.intent != "flights":
        return None
    if plan.refuse:
        return None
    if plan.around_the_world or plan.trip == "multi":
        return None
    if not plan.origin or not plan.destination or plan.departure_date is None:
        return None
    if not is_known_iata(plan.origin) or not is_known_iata(plan.destination):
        return None
    if plan.departure_date < (today or date.today()):
        return None
    if plan.route_specs:
        if len(plan.route_specs) > 2:
            return None
        return plan.route_specs
    if plan.trip == "rt" and plan.return_date is not None:
        out = plan.departure_date.isoformat()
        back = plan.return_date.isoformat()
        return (f"{plan.origin}-{plan.destination}:{out}:{back}",)
    return (f"{plan.origin}-{plan.destination}:{plan.departure_date.isoformat()}",)


def _sweep_search_flights(
    routes: Sequence[str],
    *,
    trip: str,
    max_stops: int,
    adults: int,
    cabin: str,
) -> Mapping[str, object]:
    """MCP search_flights path with HTTP sweep. Patch this hook in tests."""
    return search_flights_tool(
        routes,
        trip=trip,
        max_stops=max_stops,
        adults=adults,
        cabin=cabin,  # type: ignore[arg-type]
        fetch="sweep",
    )


def _maybe_time_sweep(plan: PromptPlan, sweep_left: Optional[list[int]]) -> Optional[int]:
    if sweep_left is None or sweep_left[0] <= 0:
        return None
    routes = sweep_routes_for_plan(plan, today=PROMPT_BENCH_TODAY)
    if routes is None:
        return None
    trip = plan.trip if plan.trip in {"one-way", "rt"} else "one-way"
    max_stops = plan.max_stops if plan.max_stops in {0, 1, 2} else 1
    adults = plan.adults if isinstance(plan.adults, int) and plan.adults >= 1 else 1
    if plan.cabin in {"economy", "premium-economy", "business", "first"}:
        cabin = plan.cabin
    else:
        cabin = "economy"
    started = _clock()
    try:
        _sweep_search_flights(
            routes,
            trip=trip,
            max_stops=max_stops,
            adults=adults,
            cabin=cabin,
        )
    except Exception:
        pass
    sweep_left[0] -= 1
    return _ms_since(started)


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
    if plan.price_cap is not None:
        plan_amounts.add(int(plan.price_cap))
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


def _split_return_specs(plan: PromptPlan) -> bool:
    """True when the plan is two reverse one-way legs instead of one RT package."""
    if len(plan.route_specs) != 2:
        return False
    first = _spec_pair(plan.route_specs[0])
    second = _spec_pair(plan.route_specs[1])
    if first is None or second is None:
        return False
    return first == (second[1], second[0])


def rt_split_reason(prompt: str, plan: PromptPlan) -> Optional[str]:
    """Deterministic packaged-RT law. One-line reason, or None when the plan is clean.

    Named outbound+return without an explicit split request must be one
    ``trip=rt`` package. Two one-ways, sugar one-ways, or a missing
    ``trip=rt`` is a 0 before DeepSeek. Packaging ``--trip rt`` when the
    prompt asked for two one-ways is the same 0.
    """
    if plan.intent != "flights":
        return None
    if wants_packaged_rt(prompt):
        if plan.trip != "rt" or _split_return_specs(plan):
            return "plan split a packaged RT"
        return None
    if asked_two_one_ways(prompt) and plan.trip == "rt":
        return "plan packaged a two one-way request as --trip rt"
    return None


def parse_score_1_100(raw: object) -> Optional[int]:
    """Accept an integer 1–100 only. Never coerce pass/fail or invent a score."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        score = raw
    elif isinstance(raw, float) and raw.is_integer():
        score = int(raw)
    elif isinstance(raw, str):
        token = raw.strip()
        if token.endswith("/100"):
            token = token[:-4].strip()
        score = _int_score_token(token)
        if score is None:
            return None
    else:
        return None
    if 1 <= score <= 100:
        return score
    return None


def _int_score_token(token: str) -> Optional[int]:
    """Parse a numeric token that is already an integer 1–100 candidate."""
    if not token:
        return None
    try:
        if token.isdigit():
            return int(token)
        if token.count(".") == 1:
            left, right = token.split(".")
            if left.isdigit() and right.isdigit() and set(right) <= {"0"}:
                return int(left)
    except ValueError:
        return None
    return None


_REASON_KEYS = frozenset(
    {
        "reason",
        "rationale",
        "explanation",
        "comment",
        "justification",
        "raison",
        "motivo",
        "razón",
        "razao",
        "razão",
        "grund",
        "begründung",
        "begruendung",
        "理由",
        "原因",
        "이유",
        "사유",
        "سبب",
        "السبب",
        "sababu",
        "ìdí",
        "idi",
        "ástæða",
        "rheswm",
        "arrazoia",
        "isizathu",
        "rason",
        "шалтгаан",
        "ምክንያት",
        "მიზეზი",
        "காரணம்",
        "មូលហេតុ",
    }
)
_NOT_REASON_KEYS = frozenset(
    {
        "score_1_100",
        "language",
        "lang",
        "notes",
        "id",
        "prompt",
        "expect",
        "plan",
        "rt_contract",
        "model",
        "type",
        "locale",
    }
)
_SMART_QUOTES = str.maketrans(
    {
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "«": '"',
        "»": '"',
        "「": '"',
        "」": '"',
        "『": '"',
        "』": '"',
        "〝": '"',
        "〞": '"',
        "｛": "{",
        "｝": "}",
    }
)
_SCORE_KV_RE = re.compile(
    r"""["']?score_1_100["']?\s*[:=]\s*["']?(\d{1,3}(?:\.0+)?)(?:/100)?["']?""",
    re.IGNORECASE,
)
_REASON_KEY_RE = re.compile(
    r"""["']?("""
    + "|".join(re.escape(key) for key in sorted(_REASON_KEYS, key=len, reverse=True))
    + r""")["']?\s*[:=]""",
    re.IGNORECASE,
)


def _normalize_judge_text(text: str) -> str:
    return text.lstrip("\ufeff").translate(_SMART_QUOTES)


def _reason_line(text: str) -> str:
    return text.strip().splitlines()[0].strip().strip("\"'")[:200]


def _reason_from_mapping(obj: Mapping[str, Any]) -> str:
    by_lower: dict[str, Any] = {}
    for key, value in obj.items():
        if isinstance(key, str):
            by_lower.setdefault(key.lower(), value)
    for key in ("reason", *sorted(k for k in _REASON_KEYS if k != "reason")):
        raw = obj.get(key)
        if raw is None:
            raw = by_lower.get(key.lower())
        if isinstance(raw, str) and raw.strip():
            return _reason_line(raw)
    best = ""
    for key, value in obj.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if key.lower() in _NOT_REASON_KEYS:
            continue
        candidate = value.strip()
        if len(candidate) > len(best):
            best = candidate
    return _reason_line(best) if len(best) >= 10 else ""


def _verdict_from_obj(
    obj: object,
    *,
    depth: int = 0,
    pending_score: Optional[int] = None,
    pending_reason: str = "",
) -> Optional[JudgeResult]:
    """Walk a decoded object for score_1_100 + a non-empty reason. No invention."""
    if depth > 6 or obj is None:
        return None
    if isinstance(obj, dict):
        score = parse_score_1_100(obj.get("score_1_100"))
        if score is None:
            lowered = {str(key).lower(): value for key, value in obj.items()}
            score = parse_score_1_100(lowered.get("score_1_100"))
        reason = _reason_from_mapping(obj)
        if not reason and score is not None:
            notes = obj.get("notes")
            if not isinstance(notes, str):
                lowered_notes = {str(key).lower(): value for key, value in obj.items()}
                notes = lowered_notes.get("notes")
            if isinstance(notes, str) and len(notes.strip()) >= 10:
                reason = _reason_line(notes)
        if score is None:
            score = pending_score
        if not reason:
            reason = pending_reason
        if score is not None and reason:
            return JudgeResult(score, reason)
        for value in obj.values():
            found = _verdict_from_obj(
                value,
                depth=depth + 1,
                pending_score=score,
                pending_reason=reason,
            )
            if found is not None:
                return found
        return None
    if isinstance(obj, list):
        for item in obj:
            found = _verdict_from_obj(
                item,
                depth=depth + 1,
                pending_score=pending_score,
                pending_reason=pending_reason,
            )
            if found is not None:
                return found
        return None
    if isinstance(obj, str) and "{" in obj and depth < 4:
        return _judge_verdict_from_text(obj, depth=depth + 1)
    return None


def _unescape_raw_newlines_in_strings(blob: str) -> str:
    out: list[str] = []
    in_str = False
    esc = False
    quote = ""
    for ch in blob:
        if in_str:
            if esc:
                out.append(ch)
                esc = False
                continue
            if ch == "\\":
                out.append(ch)
                esc = True
                continue
            if ch == quote:
                out.append(ch)
                in_str = False
                continue
            if ch == "\n":
                out.append("\\n")
                continue
            if ch == "\r":
                continue
            out.append(ch)
            continue
        if ch in "'\"":
            in_str = True
            quote = ch
        out.append(ch)
    return "".join(out)


def _object_slice(text: str, start: int) -> Optional[str]:
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    in_str = False
    esc = False
    quote = ""
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
            continue
        if ch in "'\"":
            in_str = True
            quote = ch
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _quote_unquoted_keys(blob: str) -> str:
    return re.sub(
        r"([{\[,]\s*)([A-Za-z_][\w]*)\s*:",
        r'\1"\2":',
        blob,
    )


def _insert_missing_commas(blob: str) -> str:
    """Insert a comma when DeepSeek omitted it between score_1_100 and the next key."""
    return re.sub(
        r"""(score_1_100["']?\s*[:=]\s*["']?\d{1,3}(?:\.0+)?(?:/100)?["']?)\s+(")""",
        r"\1, \2",
        blob,
        count=1,
        flags=re.IGNORECASE,
    )


def _loads_repaired(blob: str) -> Optional[object]:
    candidates = [blob, _unescape_raw_newlines_in_strings(blob)]
    stripped_commas = re.sub(r",(\s*[}\]])", r"\1", blob)
    candidates.append(stripped_commas)
    candidates.append(_quote_unquoted_keys(stripped_commas))
    missing = _insert_missing_commas(blob)
    candidates.append(missing)
    candidates.append(_quote_unquoted_keys(missing))
    if "'" in blob and '"' not in blob:
        candidates.append(blob.replace("'", '"'))
        candidates.append(_unescape_raw_newlines_in_strings(blob.replace("'", '"')))
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _verdict_from_keyvals(text: str) -> Optional[JudgeResult]:
    """Last resort: score_1_100 + reason keys in broken JSON or YAML-like prose."""
    for score_match in _SCORE_KV_RE.finditer(text):
        score = parse_score_1_100(score_match.group(1))
        if score is None:
            continue
        window_start = max(0, score_match.start() - 400)
        window_end = min(len(text), score_match.end() + 800)
        window = text[window_start:window_end]
        reason_match = _REASON_KEY_RE.search(window)
        if reason_match is None:
            continue
        reason = _reason_value_at(window, reason_match.end())
        if reason:
            return JudgeResult(score, reason)
    return None


def _reason_value_at(text: str, value_start: int) -> str:
    idx = value_start
    while idx < len(text) and text[idx] in " \t":
        idx += 1
    if idx >= len(text):
        return ""
    if text[idx] in "'\"":
        quote = text[idx]
        idx += 1
        raw: list[str] = []
        esc = False
        while idx < len(text):
            ch = text[idx]
            if esc:
                raw.append(ch)
                esc = False
                idx += 1
                continue
            if ch == "\\":
                esc = True
                idx += 1
                continue
            if ch == quote:
                break
            if ch == "\n":
                break
            raw.append(ch)
            idx += 1
        return _reason_line("".join(raw))
    raw = []
    while idx < len(text) and text[idx] not in ",}\n":
        raw.append(text[idx])
        idx += 1
    return _reason_line("".join(raw))


def _judge_verdict_from_text(text: str, *, depth: int = 0) -> Optional[JudgeResult]:
    """Recover score_1_100 + reason from model text. None if nothing parses.

    DeepSeek wraps JSON (fences, leading/trailing prose, nested objects, extra
    keys, rare-language reason keys, trailing commas). Garbage is not scored.
    """
    if depth > 4:
        return None
    text = _normalize_judge_text(text)
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(text):
        start = text.find("{", idx)
        if start < 0:
            break
        obj: Optional[object] = None
        end = start + 1
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            blob = _object_slice(text, start)
            if blob is not None:
                obj = _loads_repaired(blob)
                end = start + len(blob)
        idx = max(end, start + 1)
        found = _verdict_from_obj(obj) if obj is not None else None
        if found is not None:
            return found
    stripped = text.strip()
    if stripped and stripped[0] in '[{"':
        try:
            whole = json.loads(stripped)
        except json.JSONDecodeError:
            whole = None
        if whole is not None:
            found = _verdict_from_obj(whole)
            if found is not None:
                return found
    return _verdict_from_keyvals(text)


def _message_content(payload: Mapping[str, Any]) -> Optional[object]:
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, TypeError, IndexError):
        return None
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if not isinstance(text, str):
                    text = part.get("content")
                if isinstance(text, str):
                    parts.append(text)
        content = "".join(parts)
    if isinstance(content, str) and content.strip():
        return content
    if isinstance(content, dict):
        return content
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning
    if isinstance(reasoning, dict):
        return reasoning
    return None


def parse_judge_verdict(payload: object) -> JudgeResult:
    """Parse a DeepSeek chat payload into score_1_100 + one-line reason."""
    skip = JudgeResult(None, "judge: skip (malformed verdict)")
    if not isinstance(payload, dict):
        return skip
    content = _message_content(payload)
    if content is None:
        return skip
    if isinstance(content, dict):
        recovered = _verdict_from_obj(content)
    else:
        recovered = _judge_verdict_from_text(content)
    return recovered if recovered is not None else skip


def _skip_judge() -> JudgeResult:
    return JudgeResult(None, "judge: skip")


def judge_case(case: PromptCase, plan: PromptPlan) -> JudgeResult:
    """Score a plan. Invented price/route or a packaged-RT split is 0 without DeepSeek.

    Harness priorities:
    1. Honesty: invented price or route = 0.
    2. Named contract (IATA, dates, dests the user said, refuse rest-of-trip,
       packaged RT vs two one-ways).
    3. score_ms is a different loop.
    """
    invented = invention_reason(case.prompt, plan)
    if invented is not None:
        return JudgeResult(0, invented)
    split = rt_split_reason(case.prompt, plan)
    if split is not None:
        return JudgeResult(0, split)
    if os.environ.get(JUDGE_ENV) != "1":
        return _skip_judge()
    key = _judge_api_key()
    if not key:
        return _skip_judge()
    if wants_packaged_rt(case.prompt):
        rt_contract = "packaged_rt_required"
    elif asked_two_one_ways(case.prompt):
        rt_contract = "two_one_ways_required"
    else:
        rt_contract = "one_way_or_unspecified"
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
                        "rt_contract": rt_contract,
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


def _evaluate_case(
    case: PromptCase,
    *,
    sweep_left: Optional[list[int]] = None,
) -> PromptRunResult:
    started = _clock()
    plan = plan_prompt(case.prompt, today=PROMPT_BENCH_TODAY)
    if case.judge == "deterministic":
        ok, reason = plan.matches(case.expect)
        plan_ms = _ms_since(started)
        sweep_ms = _maybe_time_sweep(plan, sweep_left)
        return PromptRunResult(
            case=case,
            status="pass" if ok else "fail",
            elapsed_ms=plan_ms,
            reason=reason,
            plan=None if ok else plan,
            sweep_ms=sweep_ms,
        )
    invented = invention_reason(case.prompt, plan)
    split = None if invented is not None else rt_split_reason(case.prompt, plan)
    plan_ms = _ms_since(started)
    sweep_ms = _maybe_time_sweep(plan, sweep_left)
    if invented is not None:
        return PromptRunResult(
            case=case,
            status="scored",
            elapsed_ms=plan_ms,
            reason=invented,
            score_1_100=0,
            plan=plan,
            sweep_ms=sweep_ms,
        )
    if split is not None:
        return PromptRunResult(
            case=case,
            status="scored",
            elapsed_ms=plan_ms,
            reason=split,
            score_1_100=0,
            plan=plan,
            sweep_ms=sweep_ms,
        )
    if os.environ.get(JUDGE_ENV) != "1":
        return PromptRunResult(
            case=case,
            status="skip",
            elapsed_ms=plan_ms,
            reason="judge: skip",
            sweep_ms=sweep_ms,
        )
    judged = judge_case(case, plan)
    if judged.skipped:
        return PromptRunResult(
            case=case,
            status="skip",
            elapsed_ms=plan_ms,
            reason=judged.reason,
            sweep_ms=sweep_ms,
        )
    return PromptRunResult(
        case=case,
        status="scored",
        elapsed_ms=plan_ms,
        reason=judged.reason,
        score_1_100=judged.score_1_100,
        plan=plan,
        sweep_ms=sweep_ms,
    )


def _format_case_line(row: PromptRunResult) -> str:
    parts = [row.case.id, row.status, f"plan_ms={row.elapsed_ms}"]
    if row.sweep_ms is not None:
        parts.append(f"sweep_ms={row.sweep_ms}")
    if row.score_1_100 is not None:
        parts.append(f"score_1_100={row.score_1_100}")
    parts.append(row.reason)
    line = "  ".join(parts)
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
    sweep_requested: bool = False,
) -> str:
    counts = {"pass": 0, "fail": 0, "skip": 0, "scored": 0}
    scores: list[int] = []
    plan_times = [row.elapsed_ms for row in results]
    sweep_times = [row.sweep_ms for row in results if row.sweep_ms is not None]
    for row in results:
        counts[row.status] = counts.get(row.status, 0) + 1
        if row.status == "scored" and row.case.judge == "llm" and row.score_1_100 is not None:
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
    if judged:
        lines.append(f"judge_mean: {sum(scores) / len(scores):.1f}")
    else:
        lines.append("judge_mean:")
    lines.append(_fmt_optional_ms("plan_p50_ms", percentile_ms(plan_times, 50)))
    lines.append(_fmt_optional_ms("plan_p90_ms", percentile_ms(plan_times, 90)))
    lines.append(_fmt_optional_ms("plan_max_ms", max(plan_times) if plan_times else None))
    if sweep_requested and sweep_times:
        lines.append(f"sweep_n: {len(sweep_times)}")
        lines.append(_fmt_optional_ms("sweep_p50_ms", percentile_ms(sweep_times, 50)))
        lines.append(_fmt_optional_ms("sweep_p90_ms", percentile_ms(sweep_times, 90)))
        lines.append(_fmt_optional_ms("sweep_max_ms", max(sweep_times)))
    else:
        lines.append("sweep_ms:")
    return "\n".join(lines) + "\n"


def run_prompt_bench(
    *,
    root: Optional[Path] = None,
    holdout: bool = False,
    timeit_sweep: bool = False,
) -> int:
    if os.environ.get(LIVE_ENV) == "1":
        print(
            "note: prompt battery does not scrape; VIAJANTE_BENCH_LIVE is ignored",
            file=sys.stderr,
        )
    sweep_requested = timeit_sweep or os.environ.get(SWEEP_ENV) == "1"
    if sweep_requested:
        print(
            f"note: prompt sweep timer on (HTTP fetch=sweep, cap {MAX_SWEEP_PROMPTS}); "
            "not judge_mean or score_ms",
            file=sys.stderr,
        )
    try:
        cases = load_holdout_cases(root) if holdout else validate_prompt_corpus(root)
    except PromptCorpusError as exc:
        print("prompts: fail", flush=True)
        print(f"error: {exc}", file=sys.stderr)
        return 1

    started = _clock()
    results: list[PromptRunResult] = []
    sweep_left = [MAX_SWEEP_PROMPTS] if sweep_requested else None
    for case in cases:
        row = _evaluate_case(case, sweep_left=sweep_left)
        results.append(row)
        print(_format_case_line(row), file=sys.stderr)
    wall_ms = _ms_since(started)
    print(
        format_prompt_report(
            cases=cases,
            results=results,
            wall_ms=wall_ms,
            sweep_requested=sweep_requested,
        ),
        end="",
    )
    if any(row.status == "fail" for row in results):
        return 1
    return 0
