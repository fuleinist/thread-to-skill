"""Advisory review of the skill library.

Every reason is computed from stored columns or from skill bodies. Nothing is inferred
from a title. Review is read-only by default. `--fix` archives at most, and the optional
duplicate grouping only records metadata, so no content is ever lost.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

from .models import Skill
from .search import duplicate_pairs
from .store import Store
from .util import jaccard, parse_timestamp, tokenize

REASON_ORDER = ("stale", "failing", "unused", "duplicate", "conflicting", "low-confidence")

DEFAULT_STALE_DAYS = 90
DEFAULT_CONFIDENCE_THRESHOLD = 0.4
DEFAULT_DUPLICATE_THRESHOLD = 0.55
DEFAULT_CONFLICT_THRESHOLD = 0.45
DEFAULT_CONFLICT_OVERLAP = 0.6

REASON_ACTIONS = {
    "stale": "refresh the procedure or archive it",
    "failing": "read the procedure, then fix or archive it",
    "unused": "tighten the triggers or archive it",
    "duplicate": "merge the bodies into one skill",
    "conflicting": "reconcile the decision rules",
    "low-confidence": "review the body before the first use",
}

_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+)$")
_NEGATIVE = re.compile(
    r"\b(never|avoid|don't|do\s+not|must\s+not|should\s+not|stop|omit|skip)\b", re.IGNORECASE
)
_POSITIVE = re.compile(r"\b(always|must|should|use|prefer|require|required)\b", re.IGNORECASE)
_POLARITY_TOKENS = {
    "always", "never", "must", "should", "use", "prefer", "avoid", "dont", "do",
    "not", "stop", "omit", "skip", "require", "required", "rule", "rules",
}


@dataclass
class Finding:
    slug: str
    name: str
    status: str
    reasons: list[str]
    age_days: int | None
    confidence: float
    worked: int
    failed: int
    suggested: int
    accepted: int
    detail: str
    action: str
    duplicate_of: str = ""
    duplicate_similarity: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "status": self.status,
            "reasons": list(self.reasons),
            "age_days": self.age_days,
            "confidence": round(self.confidence, 3),
            "worked": self.worked,
            "failed": self.failed,
            "suggested": self.suggested,
            "accepted": self.accepted,
            "detail": self.detail,
            "action": self.action,
        }


def _as_datetime(value: str) -> dt.datetime | None:
    """Parse a stored timestamp into an aware UTC datetime. Return None when unusable."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        normalized = parse_timestamp(text)
        if not normalized or normalized == text:
            return None
        try:
            parsed = dt.datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def age_days(skill: Skill, now: dt.datetime | None = None) -> int | None:
    """Days since the skill was last used, else since it was last updated."""
    reference = _as_datetime(skill.last_used_at) or _as_datetime(skill.updated_at)
    if reference is None:
        return None
    current = now or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    delta = (current - reference).total_seconds() / 86400.0
    return max(0, int(delta))


def _polarity(text: str) -> str:
    if _NEGATIVE.search(text):
        return "negative"
    if _POSITIVE.search(text):
        return "positive"
    return ""


def decision_rules(body: str) -> list[tuple[str, frozenset[str]]]:
    """Extract bullet rules that carry an explicit polarity, as (polarity, tokens)."""
    rules: list[tuple[str, frozenset[str]]] = []
    for line in (body or "").splitlines():
        match = _BULLET.match(line)
        if not match:
            continue
        text = match.group(1).strip()
        polarity = _polarity(text)
        if not polarity:
            continue
        tokens = frozenset(token for token in tokenize(text) if token not in _POLARITY_TOKENS)
        if len(tokens) < 2:
            continue
        rules.append((polarity, tokens))
    return rules


def conflicting_pairs(
    skills: list[Skill],
    pairs: list[tuple[Skill, Skill, float]],
    *,
    overlap_threshold: float = DEFAULT_CONFLICT_OVERLAP,
) -> list[tuple[Skill, Skill, float]]:
    """Near-duplicate pairs whose bullets state opposite rules about the same subject."""
    rules = {skill.slug: decision_rules(skill.body) for skill in skills}
    found: list[tuple[Skill, Skill, float]] = []
    for left, right, similarity in pairs:
        left_rules = rules.get(left.slug, [])
        right_rules = rules.get(right.slug, [])
        if not left_rules or not right_rules:
            continue
        best = 0.0
        for left_polarity, left_tokens in left_rules:
            for right_polarity, right_tokens in right_rules:
                if left_polarity == right_polarity:
                    continue
                overlap = jaccard(left_tokens, right_tokens)
                if overlap >= overlap_threshold:
                    best = max(best, overlap)
        if best:
            found.append((left, right, best))
    return found


def _keeper_and_follower(left: Skill, right: Skill) -> tuple[Skill, Skill]:
    """Pick which skill a duplicate group should keep. Deterministic on confidence then slug."""
    if left.confidence != right.confidence:
        return (left, right) if left.confidence > right.confidence else (right, left)
    return (left, right) if left.slug <= right.slug else (right, left)


def review_skills(
    store: Store,
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    duplicate_threshold: float = DEFAULT_DUPLICATE_THRESHOLD,
    conflict_threshold: float = DEFAULT_CONFLICT_THRESHOLD,
    now: dt.datetime | None = None,
) -> list[Finding]:
    """Return one finding per skill that matches at least one reason, sorted by slug."""
    skills = store.list_skills(limit=1000)
    current = now or dt.datetime.now(dt.timezone.utc)
    reasons: dict[str, list[str]] = {skill.slug: [] for skill in skills}
    details: dict[str, list[str]] = {skill.slug: [] for skill in skills}
    duplicate_of: dict[str, str] = {}
    duplicate_score: dict[str, float] = {}

    pair_threshold = min(duplicate_threshold, conflict_threshold)
    pairs = duplicate_pairs(skills, threshold=pair_threshold)

    for left, right, similarity in pairs:
        if similarity >= duplicate_threshold:
            keeper, follower = _keeper_and_follower(left, right)
            if "duplicate" not in reasons[follower.slug]:
                reasons[follower.slug].append("duplicate")
                details[follower.slug].append(f"{round(similarity, 2)} similar to {keeper.slug}")
                duplicate_of[follower.slug] = keeper.slug
                duplicate_score[follower.slug] = round(similarity, 3)

    for left, right, overlap in conflicting_pairs(skills, pairs):
        for mine, other in ((left, right), (right, left)):
            if "conflicting" in reasons[mine.slug]:
                continue
            reasons[mine.slug].append("conflicting")
            details[mine.slug].append(f"opposite rules vs {other.slug} (overlap {round(overlap, 2)})")

    for skill in skills:
        age = age_days(skill, current)
        if age is not None and age >= stale_days:
            reasons[skill.slug].append("stale")
            details[skill.slug].append(f"last used {age} days ago")
        if skill.failed > skill.worked or skill.status == "needs-review":
            reasons[skill.slug].append("failing")
            details[skill.slug].append(f"worked {skill.worked}, failed {skill.failed}")
        if skill.suggested > 0 and skill.accepted == 0:
            reasons[skill.slug].append("unused")
            details[skill.slug].append(f"suggested {skill.suggested} times, never accepted")
        if skill.confidence < confidence_threshold and skill.worked + skill.failed == 0:
            reasons[skill.slug].append("low-confidence")
            details[skill.slug].append(f"confidence {round(skill.confidence, 2)} with no usage")

    findings: list[Finding] = []
    for skill in sorted(skills, key=lambda item: item.slug):
        if not reasons[skill.slug]:
            continue
        ordered = [reason for reason in REASON_ORDER if reason in reasons[skill.slug]]
        findings.append(
            Finding(
                slug=skill.slug,
                name=skill.name,
                status=skill.status,
                reasons=ordered,
                age_days=age_days(skill, current),
                confidence=skill.confidence,
                worked=skill.worked,
                failed=skill.failed,
                suggested=skill.suggested,
                accepted=skill.accepted,
                detail="; ".join(details[skill.slug]),
                action=REASON_ACTIONS[ordered[0]],
                duplicate_of=duplicate_of.get(skill.slug, ""),
                duplicate_similarity=duplicate_score.get(skill.slug, 0.0),
            )
        )
    return findings


def reason_counts(findings: list[Finding]) -> dict[str, int]:
    """Counts for every reason, including the zeroes, so the JSON shape stays stable."""
    counts = {reason: 0 for reason in REASON_ORDER}
    for finding in findings:
        for reason in finding.reasons:
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def review_payload(
    findings: list[Finding],
    *,
    stale_days: int,
    confidence_threshold: float,
    duplicate_threshold: float,
    conflict_threshold: float,
    checked: int,
) -> dict[str, Any]:
    return {
        "checked": checked,
        "stale_days": stale_days,
        "confidence_threshold": round(confidence_threshold, 3),
        "duplicate_threshold": round(duplicate_threshold, 3),
        "conflict_threshold": round(conflict_threshold, 3),
        "counts": reason_counts(findings),
        "findings": [finding.to_dict() for finding in findings],
    }


def apply_fixes(
    store: Store,
    findings: list[Finding],
    *,
    allow_duplicate_groups: bool = False,
) -> dict[str, Any]:
    """Apply only safe actions. Archive failing skills with zero successes; never delete."""
    archived: list[str] = []
    for finding in findings:
        if "failing" not in finding.reasons:
            continue
        if finding.status != "needs-review" or finding.worked != 0:
            continue
        store.archive_skill(finding.slug)
        archived.append(finding.slug)

    grouped: list[dict[str, Any]] = []
    if allow_duplicate_groups:
        for finding in findings:
            if not finding.duplicate_of:
                continue
            skill = store.get_skill(finding.slug)
            if skill is None:
                continue
            metadata = dict(skill.metadata or {})
            review = dict(metadata.get("review") or {})
            review["duplicate_of"] = finding.duplicate_of
            review["similarity"] = finding.duplicate_similarity
            metadata["review"] = review
            store.update_skill_fields(finding.slug, metadata=metadata)
            grouped.append(
                {
                    "slug": finding.slug,
                    "duplicate_of": finding.duplicate_of,
                    "similarity": finding.duplicate_similarity,
                }
            )

    return {"archived": archived, "grouped": grouped}