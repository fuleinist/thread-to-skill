"""Cluster related threads and merge their steps into one procedure.

This module backs ``t2s distill --synthesize``. It is deterministic and has no
model dependency: threads are compared by token overlap, and two steps conflict
when they give opposite instructions about the same subject words.

Conflicts are reported, never resolved. A later thread is named as newer only
when both sides carry a timestamp.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .models import Thread
from .offline import offline_distill
from .util import tokenize

DEFAULT_CLUSTER_THRESHOLD = 0.18
DEFAULT_MERGE_THRESHOLD = 0.72
DEFAULT_CONFLICT_THRESHOLD = 0.75
MIN_SHARED_SUBJECT_TOKENS = 2
MIN_ATTRIBUTION_TOKENS = 2

# The offline extractor emits these when a thread states no procedure. They
# carry no thread-specific content, so a synthesis drops them instead of
# attributing generic advice to one conversation.
FALLBACK_STEPS = (
    "Re-read the source thread and confirm the agreed approach",
    "Apply the smallest reversible change that addresses the problem",
    "Verify the result and record whether the skill worked",
)

# Read on raw text, before tokenization, because the stop-word list already
# drops "do", "not", "don't", and "shouldn't".
NEGATIVE_CUES = (
    "never",
    "do not",
    "don't",
    "dont",
    "avoid",
    "must not",
    "should not",
    "shouldn't",
    "cannot",
    "can't",
    "no longer",
)
POSITIVE_CUES = ("always", "must", "ensure", "require", "required")

# Removed from the subject so a negated and a plain step compare equal.
POLARITY_WORDS = {
    "never",
    "always",
    "avoid",
    "must",
    "ensure",
    "require",
    "required",
    "dont",
    "cannot",
    "cant",
}

NEGATIVE = "negative"
POSITIVE = "positive"
NEUTRAL = "neutral"


def _has_word(text: str, needle: str) -> bool:
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", text))


def _thread_tokens(thread: Thread) -> set[str]:
    tokens: set[str] = set()
    for message in thread.messages:
        for token in tokenize(message.text or ""):
            if not token.isdigit():
                tokens.add(token)
    return tokens


def similarity(left: Thread, right: Thread) -> float:
    """Token Jaccard over the two threads. Cheap, deterministic, dependency-free."""
    first = _thread_tokens(left)
    second = _thread_tokens(right)
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


def cluster_threads(
    threads: list[Thread], *, threshold: float = DEFAULT_CLUSTER_THRESHOLD
) -> list[list[Thread]]:
    """Greedy single-pass clustering that preserves input order."""
    clusters: list[list[Thread]] = []
    for thread in threads:
        best_index = -1
        best_score = 0.0
        for index, cluster in enumerate(clusters):
            score = max(similarity(thread, member) for member in cluster)
            if score > best_score:
                best_index = index
                best_score = score
        if best_index >= 0 and best_score >= threshold:
            clusters[best_index].append(thread)
        else:
            clusters.append([thread])
    return clusters


def step_polarity(text: str) -> str:
    low = (text or "").lower()
    for cue in NEGATIVE_CUES:
        if _has_word(low, cue):
            return NEGATIVE
    for cue in POSITIVE_CUES:
        if _has_word(low, cue):
            return POSITIVE
    return NEUTRAL


def _subject(text: str) -> set[str]:
    return {token for token in tokenize(text) if token not in POLARITY_WORDS}


def steps_conflict(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """True when one step forbids what the other requires, for the same subject.

    Overlap is measured against the shorter subject, not the union. Two people
    rarely describe one instruction with the same number of words, and a
    conflict that hides behind one extra adjective is a conflict all the same.
    """
    first = step_polarity(str(left.get("step") or ""))
    second = step_polarity(str(right.get("step") or ""))
    if first == second:
        return False
    if NEGATIVE not in (first, second):
        return False
    left_subject = _subject(str(left.get("step") or ""))
    right_subject = _subject(str(right.get("step") or ""))
    shared = left_subject & right_subject
    if len(shared) < MIN_SHARED_SUBJECT_TOKENS:
        return False
    shorter = min(len(left_subject), len(right_subject))
    if not shorter:
        return False
    return len(shared) / shorter >= DEFAULT_CONFLICT_THRESHOLD


def _steps_similar(left: dict[str, Any], right: dict[str, Any], *, threshold: float) -> bool:
    # A contradictory pair is never merged: that would average the conflict away.
    if steps_conflict(left, right):
        return False
    # Procedure steps and pitfalls stay in their own sections.
    if str(left.get("kind") or "step") != str(right.get("kind") or "step"):
        return False
    first = set(tokenize(str(left.get("step") or "")))
    second = set(tokenize(str(right.get("step") or "")))
    if not first or not second:
        return False
    return len(first & second) / len(first | second) >= threshold


def _union(first: Iterable[str], second: Iterable[str]) -> list[str]:
    ordered: list[str] = []
    for item in list(first) + list(second):
        value = str(item).strip()
        if value and value not in ordered:
            ordered.append(value)
    return ordered


def merge_steps(
    steps: list[dict[str, Any]], *, threshold: float = DEFAULT_MERGE_THRESHOLD
) -> list[dict[str, Any]]:
    """Fold near-duplicate steps. The merged step keeps every contributing source."""
    merged: list[dict[str, Any]] = []
    for step in steps:
        target = next(
            (candidate for candidate in merged if _steps_similar(candidate, step, threshold=threshold)),
            None,
        )
        record = {
            "step": str(step.get("step") or ""),
            "details": str(step.get("details") or ""),
            "evidence": _union(step.get("evidence") or [], []),
            "source_threads": _union(step.get("source_threads") or [], []),
            "kind": str(step.get("kind") or "step"),
        }
        if target is None:
            if record["step"]:
                merged.append(record)
            continue
        target["source_threads"] = _union(target["source_threads"], record["source_threads"])
        target["evidence"] = _union(target["evidence"], record["evidence"])
        if len(record["step"]) > len(target["step"]):
            target["step"] = record["step"]
        if record["details"] and record["details"] not in target["details"]:
            target["details"] = (target["details"] + " " + record["details"]).strip()
    return merged


def _latest(
    thread_ids: Iterable[str], by_id: dict[str, Thread]
) -> tuple[str, str]:
    stamps = [
        (by_id[thread_id].ended_at, thread_id)
        for thread_id in thread_ids
        if thread_id in by_id and by_id[thread_id].ended_at
    ]
    if not stamps:
        return ("", "")
    return max(stamps)


def _newer_thread(
    left: dict[str, Any], right: dict[str, Any], by_id: dict[str, Thread]
) -> str:
    left_stamp, left_id = _latest(left.get("source_threads") or [], by_id)
    right_stamp, right_id = _latest(right.get("source_threads") or [], by_id)
    if not left_stamp or not right_stamp or left_stamp == right_stamp:
        return ""
    if left_stamp > right_stamp:
        return left_id
    return right_id


def find_conflicts(
    steps: list[dict[str, Any]], threads: list[Thread]
) -> list[dict[str, Any]]:
    """Compare steps from different threads and return every contradiction."""
    by_id = {thread.id: thread for thread in threads}
    conflicts: list[dict[str, Any]] = []
    for index, left in enumerate(steps):
        for right in steps[index + 1:]:
            left_sources = list(left.get("source_threads") or [])
            right_sources = list(right.get("source_threads") or [])
            if not left_sources or not right_sources:
                continue
            if set(left_sources) & set(right_sources):
                # Same thread: a revision, not a cross-thread conflict.
                continue
            if not steps_conflict(left, right):
                continue
            conflicts.append(
                {
                    "step": str(left.get("step") or ""),
                    "source_threads": left_sources,
                    "kind": str(left.get("kind") or "step"),
                    "conflicts_with": str(right.get("step") or ""),
                    "conflicting_source_threads": right_sources,
                    "conflicting_kind": str(right.get("kind") or "step"),
                    "reason": "opposite instruction about the same subject",
                    "newer_thread": _newer_thread(left, right, by_id),
                }
            )
    return conflicts


def attribute_step(
    step_text: str, threads: list[Thread], *, min_shared: int = MIN_ATTRIBUTION_TOKENS
) -> list[str]:
    """Best-effort source thread for a step the model did not attribute."""
    target = set(tokenize(step_text or ""))
    if not target:
        return []
    best_score = 0
    best: list[str] = []
    for thread in threads:
        shared = len(target & _thread_tokens(thread))
        if shared > best_score:
            best_score = shared
            best = [thread.id]
        elif shared == best_score and shared:
            best.append(thread.id)
    if best_score < min_shared:
        return []
    return best[:3]


def synthesize(
    threads: list[Thread], *, threshold: float = DEFAULT_CLUSTER_THRESHOLD
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Cluster the threads, merge each cluster's steps, and report conflicts."""
    clusters = cluster_threads(threads, threshold=threshold)
    all_steps: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for cluster in clusters:
        procedure: list[dict[str, Any]] = []
        pitfalls: list[dict[str, Any]] = []
        for thread in cluster:
            data = offline_distill([thread])
            for step in data.get("procedure") or []:
                step_text = str(step.get("step") or "")
                if step_text in FALLBACK_STEPS:
                    continue
                procedure.append(
                    {
                        "step": step_text,
                        "details": str(step.get("details") or ""),
                        "evidence": _union(step.get("evidence") or [], []),
                        "source_threads": [thread.id],
                        "kind": "step",
                    }
                )
            # A "never do X" instruction lands in pitfalls. Conflicts must span
            # both sections, otherwise the exact case in the issue is invisible.
            for pitfall in data.get("pitfalls") or []:
                pitfalls.append(
                    {
                        "step": str(pitfall.get("pitfall") or ""),
                        "details": str(pitfall.get("avoid") or ""),
                        "evidence": _union(pitfall.get("evidence") or [], []),
                        "source_threads": [thread.id],
                        "kind": "pitfall",
                    }
                )
        merged_procedure = merge_steps(procedure)
        merged_pitfalls = merge_steps(pitfalls)
        merged = merged_procedure + merged_pitfalls
        all_steps.extend(merged)
        summaries.append(
            {
                "thread_ids": [thread.id for thread in cluster],
                "threads": len(cluster),
                "steps": len(merged_procedure),
                "pitfalls": len(merged_pitfalls),
            }
        )
    conflicts = find_conflicts(all_steps, threads)
    return all_steps, conflicts, summaries


def apply_synthesis(threads: list[Thread], data: dict[str, Any]) -> dict[str, Any]:
    """Replace the procedure with the synthesized one and record the conflicts."""
    steps, conflicts, clusters = synthesize(threads)
    result = dict(data)
    result["procedure"] = steps
    result["conflicts"] = conflicts
    result["synthesis"] = {
        "enabled": True,
        "clusters": clusters,
        "conflicts": len(conflicts),
        "steps": len(steps),
    }
    return result