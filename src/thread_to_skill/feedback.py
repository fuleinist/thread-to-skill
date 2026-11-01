"""Usage feedback and the self-improvement loop."""

from __future__ import annotations

from typing import Any

from .models import Skill
from .store import Store
from .util import clamp, now_iso

OUTCOMES = ("worked", "failed", "accepted", "suggested")
DELTAS = {"worked": 0.05, "failed": -0.08, "accepted": 0.02, "suggested": 0.0}


def apply_feedback(store: Store, slug: str, outcome: str, note: str = "") -> Skill:
    if outcome not in OUTCOMES:
        raise ValueError(f"Unknown outcome: {outcome}")
    skill = store.get_skill(slug)
    if not skill:
        raise KeyError(f"Unknown skill: {slug}")
    confidence = clamp(skill.confidence + DELTAS[outcome], 0.03, 0.98)
    counters: dict[str, int] = {
        "suggested": skill.suggested,
        "accepted": skill.accepted,
        "worked": skill.worked,
        "failed": skill.failed,
    }
    counters[outcome] += 1
    status = skill.status
    if counters["failed"] >= 3 and counters["worked"] == 0:
        status = "needs-review"
    elif status == "needs-review" and counters["worked"] > counters["failed"]:
        status = "active"
    store.update_skill_fields(
        slug,
        confidence=round(confidence, 3),
        status=status,
        last_used_at=now_iso() if outcome in {"worked", "failed", "accepted"} else skill.last_used_at,
        updated_at=now_iso(),
        **counters,
    )
    store.record_event("feedback", skill_slug=slug, data={"outcome": outcome, "note": note[:1000]})
    updated = store.get_skill(slug)
    assert updated is not None
    return updated


def feedback_summary(skill: Skill) -> dict[str, Any]:
    total = skill.worked + skill.failed
    return {
        "slug": skill.slug,
        "confidence": round(skill.confidence, 3),
        "status": skill.status,
        "suggested": skill.suggested,
        "accepted": skill.accepted,
        "worked": skill.worked,
        "failed": skill.failed,
        "success_rate": round((skill.worked + 1) / (total + 2), 3),
    }