"""Skill retrieval, prompt matching, and duplicate detection."""

from __future__ import annotations

import re
from typing import Iterable

from .models import Skill
from .store import Store
from .util import clamp, jaccard, shingles, tokenize

STOP_TRIGGER_WORDS = {
    "the", "and", "for", "with", "this", "that", "from", "when", "what",
    "how", "why", "use", "using", "skill", "thread", "conversation",
}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def match_score(skill: Skill, prompt: str) -> tuple[float, list[str]]:
    low = _normalize(prompt)
    if not low:
        return 0.0, []
    reasons: list[str] = []
    score = 0.0

    for trigger in skill.triggers:
        trigger_low = _normalize(trigger)
        if len(trigger_low) >= 4 and trigger_low in low:
            score += 4.0
            reasons.append(f"trigger:{trigger}")
    for tag in skill.tags:
        tag_low = _normalize(tag.replace("-", " "))
        if len(tag_low) >= 3 and re.search(r"\b" + re.escape(tag_low) + r"\b", low):
            score += 2.0
            reasons.append(f"tag:{tag}")
    for token in set(tokenize(prompt)):
        if token in skill.name.lower():
            score += 1.5
            reasons.append(f"name:{token}")
        elif token in skill.title.lower():
            score += 1.0
            reasons.append(f"title:{token}")
        elif token in skill.description.lower():
            score += 0.6
            reasons.append(f"description:{token}")
        elif token in skill.category.lower():
            score += 0.4
    if not reasons:
        return 0.0, []

    # Normalize by prompt length, then blend confidence and observed success.
    base = score / (len(set(tokenize(prompt))) + 3.0)
    usage_total = skill.worked + skill.failed
    success = (skill.worked + 1.0) / (usage_total + 2.0)
    blended = 0.72 * base + 0.18 * skill.confidence + 0.10 * success
    if skill.status != "active":
        blended *= 0.25
    return clamp(blended), reasons[:6]


def suggest_skills(
    store: Store,
    prompt: str,
    *,
    limit: int = 3,
    threshold: float = 0.18,
    category: str = "",
) -> list[dict[str, object]]:
    candidates = store.list_skills(category=category, status="active", limit=1000)
    ranked: list[tuple[float, Skill, list[str]]] = []
    for skill in candidates:
        score, reasons = match_score(skill, prompt)
        if score >= threshold:
            ranked.append((score, skill, reasons))
    ranked.sort(key=lambda item: (item[0], item[1].confidence), reverse=True)
    results: list[dict[str, object]] = []
    for score, skill, reasons in ranked[:limit]:
        results.append(
            {
                "slug": skill.slug,
                "name": skill.name,
                "title": skill.title,
                "description": skill.description,
                "category": skill.category,
                "tags": skill.tags,
                "confidence": round(skill.confidence, 3),
                "score": round(score, 3),
                "reasons": reasons,
                "path": skill.path,
            }
        )
    if results:
        store.record_event(
            "suggested",
            data={"prompt": prompt[:500], "skills": [item["slug"] for item in results]},
        )
        for item in results:
            skill = store.get_skill(str(item["slug"]))
            if skill:
                store.update_skill_fields(skill.slug, suggested=skill.suggested + 1)
    return results


def duplicate_pairs(skills: Iterable[Skill], threshold: float = 0.55) -> list[tuple[Skill, Skill, float]]:
    items = list(skills)
    prepared = [
        (skill, shingles(" ".join([skill.name, skill.title, skill.summary, " ".join(skill.tags)])))
        for skill in items
    ]
    pairs: list[tuple[Skill, Skill, float]] = []
    for index, (left, left_shingles) in enumerate(prepared):
        for right, right_shingles in prepared[index + 1:]:
            similarity = jaccard(left_shingles, right_shingles)
            if similarity >= threshold:
                pairs.append((left, right, similarity))
    pairs.sort(key=lambda item: item[2], reverse=True)
    return pairs


def find_duplicate(
    store: Store,
    *,
    title: str,
    summary: str,
    tags: list[str],
    threshold: float = 0.55,
) -> tuple[Skill | None, float]:
    candidate_shingles = shingles(" ".join([title, summary, " ".join(tags)]))
    best: tuple[Skill | None, float] = (None, 0.0)
    for skill in store.list_skills(status="active", limit=1000):
        other = shingles(" ".join([skill.name, skill.title, skill.summary, " ".join(skill.tags)]))
        similarity = jaccard(candidate_shingles, other)
        if similarity > best[1]:
            best = (skill, similarity)
    return best if best[1] >= threshold else (None, best[1])


def format_suggestions(results: list[dict[str, object]], *, markdown: bool = False) -> str:
    if not results:
        return ""
    lines: list[str] = []
    if markdown:
        lines.append("Relevant skills from `thread-to-skill`:")
    else:
        lines.append("[thread-to-skill] Relevant skills:")
    for item in results:
        line = f"- {item['name']} ({item['category']}, confidence {item['confidence']}): {item['description']}"
        lines.append(line)
    lines.append("Read a skill's SKILL.md before applying it; record the outcome with `t2s feedback`.")
    return "\n".join(lines)