"""Locale packs for the offline extractor.

Each pack is a plain module of cue tuples for one language. This package picks
the pack for a run: an explicit name wins, otherwise a small detector votes on
the text, and English stays the default and the fallback. The packs import
nothing from the project so there is no import cycle.
"""

from __future__ import annotations

import re
import sys

from . import de, en, es, ja, zh

# Order matters: it is the order available_locales() reports.
PACKS = {
    "en": en,
    "zh": zh,
    "ja": ja,
    "de": de,
    "es": es,
}

# Evidence needed before a non-English pack wins. Short CJK fragments and a
# stray keyword are not enough; below the threshold the answer is always "en".
CJK_MIN_CHARS = 4
KEYWORD_MIN_HITS = 3

# Japanese is decided by the kana range, Chinese by the hanzi range. A kana hit
# in text that is otherwise hanzi is the reliable marker for Japanese.
JA_RE = re.compile(r"[\u3040-\u30ff]")
ZH_RE = re.compile(r"[\u4e00-\u9fff]")

DE_KEYWORDS = (
    "der", "die", "das", "und", "aber", "wenn", "nicht", "mit", "wir", "ich",
    "für", "über", "sich", "dass", "zu",
)
ES_KEYWORDS = (
    "el", "la", "que", "con", "por", "para", "pero", "como", "cuando", "los",
    "una", "está", "muy",
)


def available_locales() -> tuple[str, ...]:
    return ("en", "zh", "ja", "de", "es")


def get_pack(name: str):
    try:
        return PACKS[name]
    except KeyError:
        raise KeyError(f"unknown locale: {name}") from None


def detect_locale(text: str) -> str:
    """Vote on the text. Returns "en" when the evidence is thin."""
    low = (text or "").lower()

    ja_hits = len(JA_RE.findall(low))
    zh_hits = len(ZH_RE.findall(low))
    if ja_hits >= CJK_MIN_CHARS or zh_hits >= CJK_MIN_CHARS:
        return "ja" if ja_hits >= max(zh_hits, CJK_MIN_CHARS) else "zh"

    de_hits = len({word for word in DE_KEYWORDS if re.search(r"\b" + re.escape(word) + r"\b", low)})
    es_hits = len({word for word in ES_KEYWORDS if re.search(r"\b" + re.escape(word) + r"\b", low)})
    if de_hits >= KEYWORD_MIN_HITS and de_hits > es_hits:
        return "de"
    if es_hits >= KEYWORD_MIN_HITS and es_hits > de_hits:
        return "es"
    return "en"


def resolve_locale(explicit: str, text: str) -> str:
    """Resolve the pack name for a run.

    An explicit name wins. An unknown name warns on stderr and falls back to
    English. Empty or "auto" runs detection, and detection answers "en" when it
    is not confident.
    """
    name = str(explicit or "").strip().lower()
    if not name or name == "auto":
        return detect_locale(text)
    if name in PACKS:
        return name
    print(f"warning: unknown locale {name!r}, falling back to en", file=sys.stderr)
    return "en"
