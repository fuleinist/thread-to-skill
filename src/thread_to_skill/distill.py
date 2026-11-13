"""Distillation: turn threads into a validated, reusable skill definition."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .filters import FilterError, MessageFilters, apply_filters
from .models import Skill, Thread
from .offline import offline_distill
from .providers import ManualProvider, Provider, ProviderError
from .render import build_description, render_reference, write_skill_bundle
from .search import find_duplicate
from .store import Store
from .synthesis import apply_synthesis, attribute_step, cluster_threads, synthesize
from .taxonomy import category_label, normalize_category, normalize_tags
from .util import clamp, extract_json_object, now_iso, slugify, truncate, unique_slug


class DistillError(RuntimeError):
    pass


class DuplicateSkill(DistillError):
    def __init__(self, existing: Skill, similarity: float):
        super().__init__(f"Similar skill already exists: {existing.slug} ({similarity:.0%} similar)")
        self.existing = existing
        self.similarity = similarity


@dataclass
class ManualPromptReady(Exception):
    path: Path

    def __str__(self) -> str:
        return f"Prompt written to {self.path}"


SYSTEM_PROMPT = """You are a knowledge engineer who turns messy team conversations into compact, reusable agent skills.

Your job is extraction, not summarization. A good skill is a procedure a future agent can execute without re-reading the thread. It captures:
- when the procedure applies (and when it does not)
- ordered steps with real commands, versions, file paths, and environment constraints
- decision rules and their reasons
- pitfalls, failure modes, and anti-patterns
- verification steps that prove the work succeeded
- unresolved questions and disagreement

Hard rules:
1. Never invent facts, commands, versions, names, or outcomes that are not supported by the thread.
2. Preserve the participants' precise terminology when it carries meaning.
3. Attribute uncertain or contested claims instead of presenting them as consensus.
4. Cite the message IDs that support each step when possible.
5. Do not include secrets, emails, phone numbers, tokens, or private URLs. The input is redacted, but stay alert.
6. If the thread contains no reusable procedure, return low confidence and explain why in open_questions.
7. Write for an agent: imperative, specific, concise. Do not write a meeting recap.
8. Keep the final skill under roughly 2,500 words. Put depth in the procedure, not in prose.
9. Return only a single JSON object. No markdown fences, no commentary.

The skill is stored in the open Agent Skills format and may be loaded by Claude Code, Cursor, OpenClaw, Hermes Agent, or DeepSeek Harness. The frontmatter is generated for you; you produce the structured content."""


SCHEMA_HINT = """Return this JSON shape:
{
  "title": "Short human title (max 80 chars)",
  "slug": "optional-kebab-case-slug",
  "summary": "2-3 sentence description of the reusable capability",
  "category": "one category key from the list",
  "tags": ["lowercase-kebab", "max 8"],
  "triggers": ["phrases that should cause an agent to load this skill", "max 10"],
  "when_to_use": ["specific situations"],
  "when_not_to_use": ["specific exclusions"],
  "procedure": [
    {"step": "Imperative step", "details": "optional detail or command", "evidence": ["message-id"]}
  ],
  "decision_rules": [
    {"if": "condition", "then": "action", "because": "reason from the thread"}
  ],
  "pitfalls": [
    {"pitfall": "what goes wrong", "avoid": "how to avoid it", "evidence": ["message-id"]}
  ],
  "verification": ["observable check that proves success"],
  "examples": [
    {"input": "a realistic request", "output": "what the agent should do or produce"}
  ],
  "open_questions": ["unresolved disagreement or missing information"],
  "confidence": 0.0
}"""


SYNTHESIZE_HINT = """The conversations are grouped into CLUSTERS. Threads in one cluster describe the same capability.

Synthesis rules:
1. Merge the threads in a cluster into one procedure. Drop a step only when another thread in the same cluster states the same step with no extra detail.
2. Give every procedure step a "source_threads" list of the thread IDs that support it.
3. When two threads give opposite instructions about the same subject ("always retry" against "never retry"), keep both steps and add a "conflicts" entry. Never average a contradiction away.
4. Set "newer_thread" only when the thread headers make the order clear. Otherwise leave it empty and record the doubt in open_questions.
5. Keep a step that only one thread states. A single source is not a reason to drop it.

Extra JSON fields:
  "procedure": [{"step": "...", "details": "...", "source_threads": ["thread-id"], "evidence": ["message-id"]}],
  "conflicts": [{"step": "...", "source_threads": ["thread-id"], "conflicts_with": "...", "conflicting_source_threads": ["thread-id"], "reason": "...", "newer_thread": ""}]"""


def build_prompt(threads: list[Thread], *, max_chars: int = 60_000, synthesize: bool = False) -> str:
    from .taxonomy import describe_categories

    groups: list[list[Thread]] = cluster_threads(threads) if synthesize else [threads]
    blocks: list[str] = []
    used = 0
    for group_index, group in enumerate(groups, start=1):
        for thread in group:
            header = (
                (f"CLUSTER {group_index}\n" if synthesize else "")
                + f"THREAD {thread.id}\n"
                f"source: {thread.source}\nchannel: {thread.channel}\n"
                f"messages: {thread.message_count}\nparticipants: {thread.speaker_list()}\n"
            )
            body = thread.content_text()
            remaining = max_chars - used - len(header)
            if remaining <= 1000:
                blocks.append(header + "\n[truncated: additional threads omitted]")
                break
            if len(body) > remaining:
                head = body[: int(remaining * 0.55)]
                tail = body[-int(remaining * 0.35):]
                body = head + "\n\n[... middle of thread omitted for length ...]\n\n" + tail
            blocks.append(header + "\n" + body)
            used += len(header) + len(body)
    intro = "Distill the following conversation(s) into one reusable agent skill.\n\n"
    if synthesize:
        intro += SYNTHESIZE_HINT + "\n\n"
    return (
        intro
        + f"CATEGORY TAXONOMY\n{describe_categories()}\n\n"
        + f"{SCHEMA_HINT}\n\n"
        + "CONVERSATIONS\n\n" + "\n\n---\n\n".join(blocks)
    )


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [value]
    return [value]


def _text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _steps(value: Any) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for item in _as_list(value):
        if isinstance(item, str):
            step = item.strip()
            if step:
                steps.append({"step": step, "details": "", "evidence": []})
        elif isinstance(item, dict):
            step = _text(item.get("step") or item.get("action") or item.get("title"))
            if step:
                evidence = [str(entry) for entry in _as_list(item.get("evidence")) if str(entry).strip()]
                record: dict[str, Any] = {"step": step, "details": _text(item.get("details")), "evidence": evidence}
                # Only synthesis sets this key. A default run must not gain it.
                sources = [
                    str(entry)
                    for entry in _as_list(item.get("source_threads"))
                    if str(entry).strip()
                ]
                if sources:
                    record["source_threads"] = sources
                steps.append(record)
    return steps


def _conflicts(value: Any) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for item in _as_list(value):
        if not isinstance(item, dict):
            continue
        step = _text(item.get("step"))
        other = _text(item.get("conflicts_with"))
        if not step or not other:
            continue
        record: dict[str, Any] = {
            "step": step,
            "source_threads": [
                str(entry) for entry in _as_list(item.get("source_threads")) if str(entry).strip()
            ],
            "conflicts_with": other,
            "conflicting_source_threads": [
                str(entry)
                for entry in _as_list(item.get("conflicting_source_threads"))
                if str(entry).strip()
            ],
            "reason": _text(item.get("reason")),
            "newer_thread": _text(item.get("newer_thread")),
        }
        kind = _text(item.get("kind"))
        if kind:
            record["kind"] = kind
        other_kind = _text(item.get("conflicting_kind"))
        if other_kind:
            record["conflicting_kind"] = other_kind
        conflicts.append(record)
    return conflicts


def _rules(value: Any) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    for item in _as_list(value):
        if isinstance(item, dict):
            condition = _text(item.get("if") or item.get("condition"))
            action = _text(item.get("then") or item.get("action"))
            because = _text(item.get("because") or item.get("reason"))
            if condition or action:
                # Keep the per-rule confidence so an inferred decision stays
                # distinguishable from one the team stated outright.
                try:
                    confidence = float(item.get("confidence", 1.0))
                except (TypeError, ValueError):
                    confidence = 1.0
                rules.append(
                    {"if": condition, "then": action, "because": because, "confidence": round(confidence, 2)}
                )
        elif isinstance(item, str) and item.strip():
            rules.append({"if": "", "then": item.strip(), "because": "", "confidence": 1.0})
    return rules


def _pitfalls(value: Any) -> list[dict[str, str]]:
    pitfalls: list[dict[str, str]] = []
    for item in _as_list(value):
        if isinstance(item, dict):
            pitfall = _text(item.get("pitfall") or item.get("problem") or item.get("title"))
            avoid = _text(item.get("avoid") or item.get("fix"))
            if pitfall:
                pitfalls.append({"pitfall": pitfall, "avoid": avoid, "evidence": [str(x) for x in _as_list(item.get("evidence"))]})
        elif isinstance(item, str) and item.strip():
            pitfalls.append({"pitfall": item.strip(), "avoid": "", "evidence": []})
    return pitfalls


def _examples(value: Any) -> list[dict[str, str]]:
    examples: list[dict[str, str]] = []
    for item in _as_list(value):
        if isinstance(item, dict):
            input_text = _text(item.get("input") or item.get("request"))
            output_text = _text(item.get("output") or item.get("result"))
            if input_text or output_text:
                examples.append({"input": input_text, "output": output_text})
        elif isinstance(item, str) and item.strip():
            examples.append({"input": item.strip(), "output": ""})
    return examples[:3]


def normalize_data(
    data: dict[str, Any],
    threads: list[Thread],
    *,
    category_override: str = "",
    tags_extra: list[str] | None = None,
    slug_override: str = "",
) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise DistillError("Distillation did not return a JSON object")
    title = truncate(_text(data.get("title"), threads[0].title if threads else "Conversation skill"), 80)
    summary = _text(data.get("summary") or data.get("description"))
    if not summary:
        summary = f"Reusable procedure distilled from {sum(thread.message_count for thread in threads)} messages."
    summary = truncate(summary, 400)

    category = category_override or _text(data.get("category"))
    category = normalize_category(category)
    if category == "uncategorized" and threads:
        # A safe fallback derived from tags/source, never guessed from the title alone.
        fallback = normalize_category((_as_list(data.get("tags")) or [""])[0])
        category = fallback if fallback != "uncategorized" else "uncategorized"

    extra_tags = list(tags_extra or [])
    if threads:
        extra_tags.extend([thread.source for thread in threads[:2]])
        extra_tags.extend([thread.channel for thread in threads[:2] if thread.channel])
    tags = normalize_tags([str(item) for item in _as_list(data.get("tags"))], extra_tags)
    triggers = []
    for item in _as_list(data.get("triggers")):
        value = _text(item)
        value = re_sub_use_when(value)
        if value and value.lower() not in {existing.lower() for existing in triggers}:
            triggers.append(value[:120])
    if not triggers:
        triggers = [tag.replace("-", " ") for tag in tags[:5]]
    triggers = triggers[:10]

    when_to_use = [_text(item) for item in _as_list(data.get("when_to_use")) if _text(item)]
    if not when_to_use:
        when_to_use = [f"Use when {trigger}." for trigger in triggers[:4]]
    if not when_to_use:
        when_to_use = [f"Use when working on {category_label(category).lower()} and the source conversation is relevant."]
    when_not_to_use = [_text(item) for item in _as_list(data.get("when_not_to_use")) if _text(item)]

    procedure = _steps(data.get("procedure"))
    if not procedure:
        procedure = [{"step": "Re-read the source thread and apply the agreed approach.", "details": "", "evidence": []}]

    confidence = data.get("confidence", 0.5)
    try:
        confidence_value = float(confidence)
    except (TypeError, ValueError):
        confidence_value = 0.5
    if confidence_value > 1:
        confidence_value = confidence_value / 100.0
    message_count = sum(thread.message_count for thread in threads)
    evidence_factor = clamp(message_count / 30.0)
    confidence_value = clamp(0.8 * confidence_value + 0.2 * evidence_factor, 0.05, 0.95)
    if message_count < 3:
        confidence_value = min(confidence_value, 0.55)

    provenance = data.get("provenance") if isinstance(data.get("provenance"), dict) else {}
    message_ids = [str(item) for item in _as_list(provenance.get("message_ids"))]
    if not message_ids:
        message_ids = [message.id for thread in threads for message in thread.messages if message.id][:120]
    participants = [str(item) for item in _as_list(provenance.get("participants"))]
    if not participants:
        participants = sorted({message.author for thread in threads for message in thread.messages})
    raw_sources = _as_list(provenance.get("sources"))
    sources = []
    for thread in threads:
        sources.append({"source": thread.source, "channel": thread.channel, "thread_id": thread.id, "messages": thread.message_count})
    if raw_sources:
        by_id = {str(item.get("thread_id")): item for item in raw_sources if isinstance(item, dict)}
        for source in sources:
            if source["thread_id"] in by_id:
                source.update({key: value for key, value in by_id[source["thread_id"]].items() if key not in source})

    slug_source = slug_override or _text(data.get("slug_hint")) or _text(data.get("slug")) or title
    normalized = {
        "title": title,
        "summary": summary,
        "category": category,
        "tags": tags,
        "triggers": triggers,
        "when_to_use": when_to_use,
        "when_not_to_use": when_not_to_use,
        "procedure": procedure,
        "decision_rules": _rules(data.get("decision_rules")),
        "pitfalls": _pitfalls(data.get("pitfalls")),
        "verification": [_text(item) for item in _as_list(data.get("verification")) if _text(item)],
        "examples": _examples(data.get("examples")),
        "open_questions": [_text(item) for item in _as_list(data.get("open_questions")) if _text(item)],
        "confidence": round(confidence_value, 3),
        "provenance": {
            "message_ids": message_ids,
            "participants": participants,
            "thread_ids": [thread.id for thread in threads],
            "sources": sources,
        },
        "extractor": _text(data.get("extractor"), "llm"),
        "slug_hint": slugify(slug_source, 44),
    }
    if not normalized["verification"]:
        normalized["verification"] = [
            "Confirm the preconditions from the source thread still hold.",
            "Run the smallest relevant test or check and compare it with the expected outcome.",
            "Record the outcome with `t2s feedback <slug> --outcome worked` or `failed`.",
        ]
    # Both keys exist only when synthesis produced them, so a default run keeps
    # the exact same data shape it had before.
    conflicts = _conflicts(data.get("conflicts"))
    if conflicts:
        normalized["conflicts"] = conflicts
    if isinstance(data.get("synthesis"), dict):
        normalized["synthesis"] = dict(data["synthesis"])
    normalized["description"] = build_description(normalized)
    return normalized


def re_sub_use_when(value: str) -> str:
    import re

    return re.sub(r"^use\s+when\s+", "", value, flags=re.I).strip().rstrip(".")


def _complete_synthesis(data: dict[str, Any], threads: list[Thread]) -> dict[str, Any]:
    """Fill the gaps a model leaves in synthesis mode.

    The model owns the wording. This deterministic pass guarantees two things
    the issue treats as non-negotiable: every step carries a source thread, and
    a contradiction is never dropped. A model-supplied conflict list wins, so
    its reasoning stays intact.
    """
    result = dict(data)
    steps = _steps(result.get("procedure"))
    for step in steps:
        if not step.get("source_threads"):
            step["source_threads"] = attribute_step(step["step"], threads)
    result["procedure"] = steps
    _, detected, clusters = synthesize(threads)
    conflicts = _conflicts(result.get("conflicts")) or detected
    result["conflicts"] = conflicts
    result["synthesis"] = {
        "enabled": True,
        "clusters": clusters,
        "conflicts": len(conflicts),
        "steps": len(steps),
    }
    return result


def distill_threads(
    threads: list[Thread],
    provider: Provider,
    *,
    config: Config,
    category: str = "",
    tags: list[str] | None = None,
    slug: str = "",
    filters: MessageFilters | None = None,
    synthesize: bool = False,
) -> dict[str, Any]:
    if not threads:
        raise DistillError("No threads selected")
    filter_record: dict[str, Any] = {"applied": False}
    if filters is not None and filters.active:
        try:
            threads, filter_record = apply_filters(threads, filters)
        except FilterError as exc:
            raise DistillError(str(exc)) from exc
    prompt = build_prompt(threads, synthesize=synthesize)
    if provider.mode == "offline":
        data = offline_distill(threads)
        if synthesize:
            data = apply_synthesis(threads, data)
    elif isinstance(provider, ManualProvider):
        target = config.prompts_dir / f"distill-{threads[0].id}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(SYSTEM_PROMPT + "\n\n" + prompt + "\n", encoding="utf-8")
        raise ManualPromptReady(target)
    else:
        try:
            raw = provider.complete(SYSTEM_PROMPT, prompt, max_tokens=4096)
        except ProviderError:
            raise
        try:
            data = extract_json_object(raw)
        except ValueError as exc:
            debug_path = config.prompts_dir / f"failed-{threads[0].id}.txt"
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            debug_path.write_text(raw, encoding="utf-8")
            raise DistillError(f"Could not parse provider output as JSON ({exc}). Raw output saved to {debug_path}") from exc
        data.setdefault("extractor", provider.name)
        if synthesize:
            data = _complete_synthesis(data, threads)
    normalized = normalize_data(
        data, threads, category_override=category, tags_extra=tags or [], slug_override=slug
    )
    if filter_record.get("applied"):
        normalized["provenance"]["filters"] = filter_record
    return normalized


def merge_data(existing: Skill, incoming: dict[str, Any], threads: list[Thread]) -> dict[str, Any]:
    original = existing.metadata.get("data") if isinstance(existing.metadata, dict) else {}
    if not isinstance(original, dict) or not original.get("procedure"):
        original = {
            "title": existing.title,
            "summary": existing.summary,
            "category": existing.category,
            "tags": existing.tags,
            "triggers": existing.triggers,
            "when_to_use": [existing.description],
            "when_not_to_use": [],
            "procedure": [{"step": "Read the existing skill and apply its procedure.", "details": "", "evidence": []}],
            "decision_rules": [],
            "pitfalls": [],
            "verification": [],
            "examples": [],
            "open_questions": [],
            "confidence": existing.confidence,
            "provenance": {"message_ids": [], "participants": [], "thread_ids": existing.source_threads, "sources": []},
        }
    merged = json.loads(json.dumps(original))
    merged["tags"] = normalize_tags(list(merged.get("tags") or []) + list(incoming.get("tags") or []))
    merged["triggers"] = list(dict.fromkeys(list(merged.get("triggers") or []) + list(incoming.get("triggers") or [])))[:12]
    for section in ("when_to_use", "when_not_to_use", "verification", "open_questions"):
        current = list(merged.get(section) or [])
        for item in incoming.get(section) or []:
            if item not in current:
                current.append(item)
        merged[section] = current
    for section, key in (("procedure", "step"), ("pitfalls", "pitfall"), ("decision_rules", "then"), ("examples", "input")):
        current = list(merged.get(section) or [])
        by_key = {
            str(item.get(key, "")).lower(): item
            for item in current
            if isinstance(item, dict)
        }
        for item in incoming.get(section) or []:
            if not isinstance(item, dict):
                continue
            item_key = str(item.get(key, "")).lower()
            match = by_key.get(item_key)
            if match is None:
                current.append(item)
                by_key[item_key] = item
                continue
            # A step that both sides state keeps both sources.
            sources = list(match.get("source_threads") or [])
            for source in item.get("source_threads") or []:
                if source not in sources:
                    sources.append(source)
            if sources:
                match["source_threads"] = sources
        merged[section] = current
    merged["confidence"] = round(max(float(merged.get("confidence") or 0), float(incoming.get("confidence") or 0)), 3)
    provenance = dict(merged.get("provenance") or {})
    provenance["thread_ids"] = list(dict.fromkeys(list(provenance.get("thread_ids") or []) + [thread.id for thread in threads]))
    provenance["message_ids"] = list(dict.fromkeys(list(provenance.get("message_ids") or []) + list((incoming.get("provenance") or {}).get("message_ids") or [])))[:240]
    merged["provenance"] = provenance
    merged["description"] = build_description(merged)
    merged["slug_hint"] = existing.slug
    return merged


def save_skill(
    config: Config,
    store: Store,
    data: dict[str, Any],
    threads: list[Thread],
    *,
    on_duplicate: str = "warn",
    existing: Skill | None = None,
) -> Skill:
    duplicate, similarity = find_duplicate(
        store,
        title=str(data.get("title", "")),
        summary=str(data.get("summary", "")),
        tags=list(data.get("tags") or []),
        threshold=0.55 if not existing else 2.0,
    )
    if duplicate and not existing and on_duplicate != "new":
        if on_duplicate == "skip":
            raise DuplicateSkill(duplicate, similarity)
        if on_duplicate == "merge":
            data = merge_data(duplicate, data, threads)
            existing = duplicate
        else:
            data["related_skill"] = duplicate.slug
            data["duplicate_similarity"] = round(similarity, 3)

    slug_base = existing.slug if existing else str(data.get("slug_hint") or data.get("title", "skill"))
    slug = slug_base if existing else unique_slug(slug_base, store.all_slugs())
    created_at = existing.created_at if existing else now_iso()
    updated_at = now_iso()
    version = (existing.version + 1) if existing else 1
    path, body, metadata = write_skill_bundle(
        config.skills_dir,
        slug,
        data,
        threads,
        version=version,
        created_at=created_at,
        updated_at=updated_at,
    )
    skill = Skill(
        slug=slug,
        name=slug,
        title=str(data.get("title") or slug),
        description=str(data.get("description") or data.get("summary") or ""),
        summary=str(data.get("summary") or ""),
        category=str(data.get("category") or "uncategorized"),
        tags=list(data.get("tags") or []),
        triggers=list(data.get("triggers") or []),
        version=version,
        confidence=float(data.get("confidence") or 0.5),
        status="active",
        path=str(path),
        source_threads=[thread.id for thread in threads] + (existing.source_threads if existing else []),
        created_at=created_at,
        updated_at=updated_at,
        last_used_at=existing.last_used_at if existing else "",
        suggested=existing.suggested if existing else 0,
        accepted=existing.accepted if existing else 0,
        worked=existing.worked if existing else 0,
        failed=existing.failed if existing else 0,
        body=body,
        metadata=metadata,
    )
    # Preserve unique source thread order.
    skill.source_threads = list(dict.fromkeys(skill.source_threads))
    store.save_skill(skill)
    store.record_event("distilled", skill_slug=slug, data={"threads": skill.source_threads, "version": version})
    return skill