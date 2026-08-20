"""Real-looking malformed DeepSeek judge blobs. Not holdout prompts.

Each RECOVER row is a wrapper the operator-box skips still hit after the
fenced `{`-scan: trailing commas, nested objects, smart quotes, rare-language
reason keys, YAML-like prose, single quotes, unescaped newlines. SKIP rows
must stay skipped — never invent score_1_100.
"""

from __future__ import annotations

# id, score_1_100, reason, content
RECOVER: list[tuple[str, int, str, str]] = [
    (
        "yo-hotel-fence-trailing-comma",
        88,
        "Hotel stay matches Johannesburg dates.",
        """```json
{
  "score_1_100": 88,
  "reason": "Hotel stay matches Johannesburg dates.",
}
```""",
    ),
    (
        "sw-hotel-prose-unquoted-keys",
        90,
        "Hotel search matches London dates and occupancy.",
        "Tathmini ya mpango wa hoteli:\n"
        '{score_1_100: 90, reason: "Hotel search matches London dates and occupancy."}\n'
        "Asante.",
    ),
    (
        "am-hotel-nested-evaluation",
        87,
        "Hotel occupancy stays coupled to the named inbound clock.",
        """{
  "language": "Amharic",
  "evaluation": {
    "score_1_100": 87,
    "reason": "Hotel occupancy stays coupled to the named inbound clock."
  }
}""",
    ),
    (
        "ka-hotel-smart-quotes",
        89,
        "Istanbul hotel nights match the packaged RT.",
        "შეფასება:\n"
        "{“score_1_100”: 89, “reason”: “Istanbul hotel nights match the packaged RT.”}\n",
    ),
    (
        "ja-hotel-reason-alias",
        91,
        "NRT is kept; HND is excluded; hotel dates match.",
        '{"score_1_100": 91, "理由": "NRT is kept; HND is excluded; hotel dates match."}',
    ),
    (
        "ko-hotel-fullwidth-braces",
        86,
        "Tokyo hotel stay matches ICN-NRT dates.",
        '평가:\n｛"score_1_100": 86, "reason": "Tokyo hotel stay matches ICN-NRT dates."｝\n',
    ),
    (
        "ar-hotel-single-quotes",
        84,
        "Cairo hotel nights match the DXB-CAI package.",
        "التقييم:\n"
        "{'score_1_100': 84, 'reason': 'Cairo hotel nights match the DXB-CAI package.'}\n",
    ),
    (
        "is-hotel-newline-in-reason",
        85,
        "Midnight inbound kept; hotel check-in stays 2026-10-09.",
        '{\n  "score_1_100": 85,\n'
        '  "reason": "Midnight inbound kept; hotel check-in stays 2026-10-09.\n'
        'Second line must not invent a score."\n}',
    ),
    (
        "eu-hotel-yaml-prose",
        93,
        "BIO-CDG packaged RT with Paris hotel on the named nights.",
        "Verdict for this Basque hotel plan:\n"
        "score_1_100: 93\n"
        "reason: BIO-CDG packaged RT with Paris hotel on the named nights.\n"
        "Hope this helps.",
    ),
    (
        "open-jaw-yvr-lhr-lgw-nested-string",
        100,
        "Packaged open-jaw keeps YVR-LHR and LGW-YVR as --trip rt.",
        "Open-jaw verdict:\n"
        '{\n  "id": "savage-en-yvr-lhr-lgw-packaged-open-jaw",\n'
        '  "result": "{\\"score_1_100\\": 100, '
        '\\"reason\\": \\"Packaged open-jaw keeps YVR-LHR and LGW-YVR as --trip rt.\\"}"\n'
        "}",
    ),
    (
        "zu-hotel-stringified-content-object",
        82,
        "Cape Town hotel stay matches JNB-CPT dates.",
        '{"score_1_100": 82, "reason": "Cape Town hotel stay matches JNB-CPT dates.",'
        ' "notes": "zu"}',
    ),
    (
        "ta-hotel-fenced-prose-both",
        94,
        "Singapore hotel occupancy mismatch is kept, not collapsed.",
        "இந்த திட்டம்:\n"
        "```json\n"
        '{"score_1_100": 94, '
        '"reason": "Singapore hotel occupancy mismatch is kept, not collapsed."}\n'
        "```\n"
        "முடிவு.",
    ),
    (
        "km-hotel-reason-alias-fallback",
        80,
        "Bangkok hotel nights match the packaged KTI-BKK stay.",
        '{"score_1_100": 80, "មូលហេតុ": "Bangkok hotel nights match the packaged KTI-BKK stay."}',
    ),
    (
        "fr-hotel-trailing-comma-and-fence",
        92,
        "Tokyo hotel stay matches the CDG-NRT aller-retour.",
        "Voici le JSON:\n"
        "```\n"
        '{\n  "score_1_100": 92,\n'
        '  "reason": "Tokyo hotel stay matches the CDG-NRT aller-retour.",\n}\n'
        "```\n",
    ),
]

SKIP: list[tuple[str, str]] = [
    (
        "prose-score-without-keys",
        "This Yoruba hotel plan looks like a 95 to me. Hope this helps.",
    ),
    ("broken-brace", "{not json"),
    ("old-pass-fail", '{"pass": true, "reason": "ok"}'),
    ("score-without-reason", '{"score_1_100": 90}'),
    ("score-word", '{"score_1_100": "ninety", "reason": "ok"}'),
    ("empty", ""),
    (
        "open-jaw-praise-without-json",
        "YVR-LHR-LGW open jaw is a perfect 100. Packaged RT looks right.",
    ),
]
