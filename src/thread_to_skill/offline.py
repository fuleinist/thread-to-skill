"""Deterministic, offline distillation heuristics.

This is intentionally not a language model. It is a competent extractor that
finds decisions, procedures, pitfalls, and checks in a conversation so the
tool is useful with zero configuration and testable in CI. For nuanced
distillation, use an LLM provider.
"""

from __future__ import annotations

import re
from typing import Iterable

from .models import Thread
from .taxonomy import category_label, normalize_category, normalize_tags, score_category
from .util import clamp, keyword_counts, slugify, tokenize, truncate

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
CUE_ACTION = (
    "we should", "you should", "let's", "we need to", "make sure", "the fix",
    "the solution", "the workaround", "step ", "first,", "first ", "then ",
    "next,", "instead", "use ", "run ", "add ", "remove ", "change ", "update ",
    "set ", "check ", "verify ", "configure ", "install ", "deploy ", "restart ",
    "pin ", "revert ", "roll back", "rollback", "try ", "do ", "only retry",
    "freeze ", "post ", "tag ", "watch ", "introduce ", "revoke ",
)
CUE_FIX = ("the fix is", "the solution is", "the workaround is", "to fix", "root cause is")
CUE_DECISION = (
    "we decided", "decided to", "decided:", "the decision", "we agreed",
    "agreed to", "let's go with", "we'll use", "we will use", "the plan is",
    "we're going with",
)
CUE_PITFALL = (
    "don't", "do not", "avoid", "gotcha", "watch out", "careful", "be careful",
    "the problem is", "this breaks", "it breaks", "failed because", "mistake",
    "anti-pattern", "never ", "not supported", "doesn't work", "does not work",
)
CUE_VERIFY = (
    "verify", "verification", "check that", "confirm", "make sure", "test that",
    "validate", "should see", "expected", "assert", "smoke test", "run the test",
    "i added a test", "added a test",
)
GREETING = ("thanks", "thank you", "hi ", "hello", "hey ", "lgtm", "sounds good", "cool", "nice", "perfect")
CODE_HINT = (
    "```", "$ ", "npm ", "pnpm ", "yarn ", "pip ", "python ", "git ", "docker ",
    "kubectl ", "curl ", "make ", "cargo ", "go test", "pytest ", "psql ",
)
EXPLANATION_PREFIXES = (
    "the failure is", "that would explain", "i reproduced", "probably", "right.",
    "so the retry", "because ", "the problem is", "it turns out", "turns out",
    "the reason is", "this is because", "the migration", "the release job",
    "the error is", "i will", "i'll", "we'll",
)
WEAK_TAGS = {
    "create", "creates", "make", "makes", "get", "gets", "set", "sets", "use",
    "uses", "add", "adds", "run", "runs", "fix", "fixes", "new", "one", "two",
    "thing", "things", "stuff", "need", "needs", "want", "wants", "id", "ids",
    "let", "lets", "going", "gonna", "like", "just", "also", "still", "even",
    "standardize", "standardise", "minutes", "minute",
}
VERB_STARTS = {
    "add", "apply", "assign", "check", "confirm", "cover", "create", "deploy",
    "do", "ensure", "freeze", "introduce", "keep", "list", "make", "open", "pin",
    "post", "revoke", "roll", "run", "set", "tag", "update", "use", "verify",
    "wait", "watch", "write",
}
WEAK_SLUG_WORDS = {
    "returning", "keeps", "right", "is", "are", "was", "were", "has", "have",
    "had", "got", "get", "gets", "put", "puts", "make", "makes", "made",
    "about", "into", "over", "under", "after", "before", "with", "from",
    "lets", "let", "we", "our", "need", "needs", "should", "could", "would",
}

# Signals for the implicit-decision pass. These feed inference only, never the
# explicit cue path, so explicit output is unchanged.
CUE_COMMIT = (
    "go with", "let's use", "stick with", "works for me", "ship it", "do that",
)
CUE_PROPOSAL = ("let's", "we should", "we could")
CUE_AFFIRMATIVE = ("ok", "okay", "sounds good", "+1", "agreed", "yes", "yep", "sure")
NEGATED_BEFORE = re.compile(r"(?:don'?t|do not|never|avoid|stop|refuse|can'?t|shouldn'?t)\s+$")
NOUN_IT_IS = re.compile(r"(?<![a-z0-9])([a-z][a-z0-9_-]*) it is(?![a-z0-9])")
WEAK_IT_IS_NOUNS = {
    "a", "i", "an", "that", "this", "these", "those", "which", "what", "when",
    "where", "how", "why", "who", "it", "so", "then", "there",
}
VERSION_SHAPE = re.compile(r"(?<![\d.])v?\d+\.\d+(?:\.\d+)?(?![\d.])")
FLAG_SHAPE = re.compile(r"(?<![-\w])--[a-z0-9_]{1,}")
ENV_NAME_SHAPE = re.compile(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b")
BACKTICK_COMMAND = re.compile(r"`[^`]+`")
AFFIRMATIVE_MAX_WORDS = 5
TERSE_MAX_WORDS = 11
INFERRED_MAX_WORDS = 20
RULE_LIMIT = 5


def _sentences(text: str) -> list[str]:
    parts: list[str] = []
    for chunk in SENTENCE_SPLIT.split(text or ""):
        cleaned = re.sub(r"\s+", " ", chunk).strip(" -•\t")
        if 12 <= len(cleaned) <= 600:
            parts.append(cleaned)
    return parts


def _has_cue(sentence: str, cues: Iterable[str]) -> bool:
    low = sentence.lower()
    for cue in cues:
        needle = cue.strip()
        if not needle:
            continue
        # Word-boundary matching prevents "DO NOTHING" from matching "do not"
        # and keeps short cues from firing inside unrelated words.
        if re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", low):
            return True
    return False


def _score(sentence: str, index: int) -> float:
    low = sentence.lower()
    score = 0.0
    if _has_cue(low, CUE_ACTION):
        score += 3.0
    if _has_cue(low, CUE_FIX):
        score += 3.5
    if _has_cue(low, CUE_DECISION):
        score += 3.0
    if _has_cue(low, CUE_PITFALL):
        score += 2.5
    if _has_cue(low, CUE_VERIFY):
        score += 2.0
    if any(hint in low for hint in CODE_HINT):
        score += 2.0
    if any(low.startswith(greeting) for greeting in GREETING):
        score -= 3.0
    if sentence.endswith("?"):
        score -= 1.0
    if 30 <= len(sentence) <= 220:
        score += 1.0
    if re.match(r"^(?:step\s*\d+|\d+[.)])\s", low):
        score += 2.0
    score += 0.4 if index == 0 else 0.0
    return score


def _pick(sentences: list[str], predicate, limit: int) -> list[str]:
    scored = []
    for index, sentence in enumerate(sentences):
        if predicate(sentence):
            scored.append((_score(sentence, index), sentence))
    scored.sort(key=lambda item: item[0], reverse=True)
    output: list[str] = []
    seen = set()
    for _, sentence in scored:
        key = sentence.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(sentence)
        if len(output) >= limit:
            break
    return output


def _is_verify(sentence: str) -> bool:
    return _has_cue(sentence, CUE_VERIFY) or sentence.lower().startswith("verification:")


def _is_pitfall(sentence: str) -> bool:
    return _has_cue(sentence, CUE_PITFALL) or sentence.lower().startswith(("gotcha", "pitfall", "watch out"))


def _is_decision(sentence: str) -> bool:
    return _has_cue(sentence, CUE_DECISION)


def _flat_line(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _rule_key(text: str) -> str:
    """Normalized `then` text used to compare inferred rules with explicit ones."""
    return re.sub(r"[^\w]+", " ", (text or "").lower()).strip()


def _is_commitment(text: str) -> bool:
    low = (text or "").lower()
    for cue in CUE_COMMIT:
        match = re.search(r"(?<![a-z0-9])" + re.escape(cue) + r"(?![a-z0-9])", low)
        # "Don't do that" is a pitfall, not a commitment.
        if match and not NEGATED_BEFORE.search(low[:match.start()]):
            return True
    noun = NOUN_IT_IS.search(low)
    return bool(noun) and noun.group(1) not in WEAK_IT_IS_NOUNS


def _is_affirmative(text: str) -> bool:
    low = _flat_line(text).lower().strip(" .!?,")
    if not low or len(low.split()) > AFFIRMATIVE_MAX_WORDS:
        return False
    return _has_cue(low, CUE_AFFIRMATIVE)


def _is_resolution_shape(text: str) -> bool:
    low = (text or "").lower()
    return bool(
        VERSION_SHAPE.search(low)
        or FLAG_SHAPE.search(low)
        or ENV_NAME_SHAPE.search(text or "")
        or BACKTICK_COMMAND.search(text or "")
    )


def _is_proposal_shape(text: str) -> bool:
    low = (text or "").lower()
    return _is_commitment(low) or _has_cue(low, CUE_PROPOSAL) or _is_resolution_shape(text)


def _matching_clause(text: str, predicate) -> str:
    for chunk in SENTENCE_SPLIT.split(text or ""):
        clause = re.sub(r"\s+", " ", chunk).strip(" -•\t")
        if clause and predicate(clause):
            return clause
    return _flat_line(text)


def _inferred_rules(threads: list[Thread], explicit_rules: list[dict]) -> list[dict]:
    """Second, message-aware pass for decisions that were never stated outright.

    Three signals, all deliberately narrow: a commitment phrase, a short
    affirmative from a different participant right after someone else's
    proposal, and a terse resolution that answers a question. Inference is
    guesswork, so these rules carry lower confidence than explicit ones.
    """
    taken = {_rule_key(rule["then"]) for rule in explicit_rules}
    rules: list[dict] = []
    for thread in threads:
        for index, message in enumerate(thread.messages):
            text = _flat_line(message.text)
            words = len(text.split())
            if not text or words > INFERRED_MAX_WORDS:
                continue
            previous = thread.messages[max(0, index - 2):index]
            then = ""
            because = ""
            if _is_commitment(text):
                then = _clean_step(_matching_clause(text, _is_commitment))
                because = "inferred from commitment phrase"
            elif _is_affirmative(text):
                proposal = next(
                    (
                        item
                        for item in reversed(previous)
                        if item.author != message.author and _is_proposal_shape(item.text)
                    ),
                    None,
                )
                if proposal:
                    then = _clean_step(
                        _matching_clause(_flat_line(proposal.text), _is_proposal_shape)
                    )
                    because = "inferred from agreement"
            elif (
                words <= TERSE_MAX_WORDS
                and _is_resolution_shape(text)
                and previous
                and previous[-1].text.rstrip().endswith("?")
            ):
                then = _clean_step(_matching_clause(text, _is_resolution_shape))
                because = "inferred from commitment phrase"
            if not then:
                continue
            key = _rule_key(then)
            if not key or key in taken:
                continue
            taken.add(key)
            rules.append({"if": "", "then": then.rstrip(" ."), "because": because, "confidence": 0.5})
            if len(rules) >= RULE_LIMIT:
                break
    return rules


def _is_explanation(sentence: str) -> bool:
    low = sentence.lower()
    return any(low.startswith(prefix) for prefix in EXPLANATION_PREFIXES)


def _clean_step(sentence: str) -> str:
    cleaned = re.sub(r"^(?:so|and|but|then|also|okay|ok|well)[,\s]+", "", sentence, flags=re.I)
    cleaned = re.sub(r"^(?:we|i)\s+(?:should|need to|decided to|agreed to)\s+", "", cleaned, flags=re.I)
    cleaned = re.sub(r"^(?:the\s+)?(?:fix|solution|workaround)\s+is\s+(?:to\s+)?", "", cleaned, flags=re.I)
    cleaned = re.sub(r"^decided\s*:\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"^let'?s\s+", "", cleaned, flags=re.I)
    cleaned = re.sub(r"^follow-up\s*:\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"^rollback command\s*:\s*(.+)$", r"Run `\1`", cleaned, flags=re.I)
    cleaned = re.sub(r"^there (?:are|is)\s+", "", cleaned, flags=re.I)
    cleaned = cleaned.strip(" .:;")
    return cleaned[:1].upper() + cleaned[1:] if cleaned else cleaned


def _clean_pitfall(sentence: str) -> str:
    cleaned = re.sub(r"^(?:gotcha|pitfall|careful|warning|note)\s*:\s*", "", sentence, flags=re.I)
    cleaned = re.sub(r"^(?:and|but|also)\s+", "", cleaned, flags=re.I)
    cleaned = cleaned.strip(" .")
    return cleaned[:1].upper() + cleaned[1:] if cleaned else cleaned


def _clean_verification(sentence: str) -> str:
    cleaned = re.sub(r"^verification\s*:\s*", "", sentence, flags=re.I)
    cleaned = re.sub(r"^i\s+added\s+(?:a|an)\s+test\s+that\s+", "Add a regression test that ", cleaned, flags=re.I)
    cleaned = re.sub(r"^(?:we|i)\s+should\s+", "", cleaned, flags=re.I)
    cleaned = cleaned.strip(" .")
    return cleaned[:1].upper() + cleaned[1:] if cleaned else cleaned


def _display_title(raw: str) -> str:
    text = re.sub(r"^\[[^\]]+\]\s*[^:]{1,60}:\s*", "", raw or "").strip()
    text = re.split(r"(?<=[.!?])\s+", text)[0].strip()
    text = re.sub(
        r"^(?:our|we need (?:a|an)?|we should|we must|let'?s|can we|how do we|how can we)\s+",
        "",
        text,
        flags=re.I,
    )
    text = text.strip(" .:;!?")
    if not text:
        return ""
    return text[:1].upper() + text[1:]


def _slug_from_title(title: str) -> str:
    tokens = [token for token in tokenize(title) if not token.isdigit() and token not in WEAK_SLUG_WORDS]
    seen: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.append(token)
        if len(seen) >= 4:
            break
    slug = slugify("-".join(seen) or title, 44)
    parts = slug.split("-")
    while parts and parts[-1] in WEAK_SLUG_WORDS:
        parts.pop()
    return "-".join(parts) or slug


def _split_sequence(sentence: str) -> list[str]:
    """Split an explicit checklist or comma series into steps."""
    text = sentence.strip().rstrip(".")
    body = text
    colon = text.find(":")
    if 0 <= colon < len(text) - 20:
        body = text[colon + 1:].strip()
    parts = re.split(r"\s*(?:,|;|\bthen\b|\band then\b)\s*", body)
    cleaned: list[str] = []
    for part in parts:
        item = re.sub(r"^(?:and|then)\s+", "", part.strip(" ."), flags=re.I)
        if item:
            cleaned.append(_clean_step(item))
    if len(cleaned) < 3:
        return []
    verb_count = sum(
        1 for item in cleaned if item.split() and item.split()[0].lower() in VERB_STARTS
    )
    # Split only when most items are imperative. A list of nouns is a checklist
    # detail, not a sequence of independent steps.
    if verb_count / len(cleaned) < 0.6:
        return []
    if all(len(item.split()) <= 14 for item in cleaned):
        return cleaned
    return []


def _summary_from(sentences: list[str], threads: list[Thread]) -> str:
    fixes = [
        sentence for sentence in sentences
        if _has_cue(sentence, CUE_FIX) and not _is_pitfall(sentence) and not _is_verify(sentence)
    ]
    decisions_clean = [
        _clean_step(sentence) for sentence in sentences
        if _is_decision(sentence) and not _is_pitfall(sentence) and not _is_verify(sentence)
    ]
    preferred: list[str] = []
    if fixes:
        preferred.append(fixes[0])
    if decisions_clean:
        preferred.append(decisions_clean[0])
    if preferred:
        summary = " ".join(preferred)
    else:
        fallback = next(
            (
                sentence
                for sentence in sentences
                if not sentence.endswith("?")
                and not _is_verify(sentence)
                and not _is_pitfall(sentence)
                and not any(sentence.lower().startswith(greeting) for greeting in GREETING)
            ),
            "",
        )
        summary = fallback or (
            f"Reusable procedure distilled from {sum(thread.message_count for thread in threads)} "
            f"messages in {threads[0].channel or threads[0].source}."
        )
    summary = re.sub(r"\s+", " ", summary).strip()
    if not summary.endswith((".", "!", "?")):
        summary += "."
    return truncate(summary, 320)


def _triggers_from(title: str, tags: list[str], sentences: list[str]) -> list[str]:
    triggers: list[str] = []
    if 8 <= len(title) <= 140:
        triggers.append(title)
    for tag in tags:
        # Hyphenated, specific tags such as pytest-xdist are useful triggers;
        # generic ones such as on-call are not.
        if "-" in tag and len(tag) >= 8 and tag not in {"on-call"}:
            phrase = tag.replace("-", " ")
            if phrase.lower() not in {item.lower() for item in triggers}:
                triggers.append(phrase)
    for sentence in sentences:
        for match in re.finditer(r"\b(?:when|if)\s+([^,.;]{8,80})", sentence, flags=re.I):
            phrase = re.sub(r"\s+", " ", match.group(1)).strip().strip(" .")
            low = phrase.lower()
            if low.startswith(("you ", "we ", "i ")):
                continue
            if "--" in phrase or "dist " in low or len(phrase.split()) < 3:
                continue
            if phrase.lower() not in {item.lower() for item in triggers}:
                triggers.append(phrase)
        if len(triggers) >= 5:
            break
    return [item for item in triggers if item][:5]


def _commands(text: str) -> list[str]:
    commands: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or not stripped:
            continue
        if stripped.startswith("$ "):
            commands.append(stripped[2:].strip())
        elif re.match(r"^(?:npm|pnpm|yarn|pip|python|git|docker|kubectl|curl|make|cargo|go|pytest)\s", stripped):
            commands.append(stripped)
        if len(commands) >= 4:
            break
    return commands


def offline_distill(threads: list[Thread]) -> dict:
    # Extract from message bodies only. Rendered transcripts include
    # "[id] author (timestamp):" prefixes that would otherwise pollute titles.
    combined = "\n".join(
        message.text for thread in threads for message in thread.messages if message.text
    )
    sentences = _sentences(combined)
    actions = _pick(
        sentences,
        lambda s: (
            _has_cue(s, CUE_ACTION)
            and not _is_verify(s)
            and not _is_pitfall(s)
            and not _is_decision(s)
            and not _is_explanation(s)
        ),
        10,
    )
    pitfalls = _pick(sentences, _is_pitfall, 5)
    checks = _pick(sentences, _is_verify, 4)
    decisions = _pick(sentences, _is_decision, 5)
    commands = _commands(combined)

    raw_title = _display_title(threads[0].title if threads else "")
    title = raw_title or "Conversation playbook"
    keywords = keyword_counts(combined, limit=18)

    def good_tag(value: str) -> bool:
        if value in WEAK_TAGS or len(value) < 3:
            return False
        if value in {"2xx", "3xx", "4xx", "5xx"}:
            return True
        if re.match(r"^\d", value):
            return False
        if re.search(r"-(?:am|pm)$", value) or value.endswith("ms"):
            return False
        return True

    title_tags = [token for token in tokenize(title) if not token.isdigit() and good_tag(token)]
    keyword_tags = [word for word, _ in keywords if good_tag(word)]
    tags = normalize_tags(title_tags[:4] + keyword_tags)
    category_leaf = normalize_category(score_category(combined)[0]).split("/")[-1]
    if category_leaf and category_leaf != "uncategorized":
        tags = normalize_tags(tags + [category_leaf])

    category, category_scores = score_category(combined)
    if category == "uncategorized" and tags:
        fallback = normalize_category(tags[0])
        if fallback != "uncategorized":
            category = fallback

    triggers = _triggers_from(title, tags, sentences)
    if not triggers:
        triggers = tags[:5]

    summary = _summary_from(sentences, threads)

    steps: list[str] = []
    for sentence in actions:
        expanded = _split_sequence(sentence)
        steps.extend(expanded or [_clean_step(sentence)])
    for sentence in decisions:
        cleaned = _clean_step(sentence)
        if re.match(r"^(?:use|add|set|keep|make|run|deploy|roll back|pin|introduce|revoke|freeze|post|tag|watch|check|only retry)", cleaned, flags=re.I):
            steps.append(cleaned)
    for command in commands:
        if not any(command in step for step in steps):
            steps.append(f"Run `{command}`")
    deduped: list[str] = []
    seen_steps = set()
    title_key = title.lower().strip(" .")
    for step in steps:
        key = step.lower().strip(" .")
        if not key or key in seen_steps or key == title_key:
            continue
        if "roll back" in key and any("rollout undo" in existing.lower() for existing in deduped):
            continue
        seen_steps.add(key)
        deduped.append(step.rstrip(" ."))
    steps = deduped[:10]
    if not steps:
        steps = [
            "Re-read the source thread and confirm the agreed approach",
            "Apply the smallest reversible change that addresses the problem",
            "Verify the result and record whether the skill worked",
        ]

    decision_rules = []
    for sentence in decisions:
        cleaned = _clean_step(sentence)
        match = re.search(r"(?:if|when)\s+(.+?)[,;]\s*(?:then\s+)?(.+)", sentence, flags=re.I)
        if match:
            decision_rules.append({"if": match.group(1).strip(), "then": match.group(2).strip(), "because": "", "confidence": 1.0})
        elif cleaned:
            cleaned = re.sub(r"^there (?:are|is)\s+", "", cleaned, flags=re.I)
            decision_rules.append({"if": "", "then": cleaned.rstrip("."), "because": "", "confidence": 1.0})
    for sentence in sentences:
        match = re.search(r"(?:use|prefer)\s+(.+?)\s+(?:when|if)\s+(.+)", sentence, flags=re.I)
        if match and not _is_explanation(sentence):
            decision_rules.append({"if": match.group(2).strip(), "then": f"Use {match.group(1).strip()}", "because": "", "confidence": 1.0})
    unique_rules = []
    seen_rules = set()
    for rule in decision_rules:
        key = (rule["if"].lower(), rule["then"].lower())
        if key not in seen_rules and rule["then"]:
            seen_rules.add(key)
            unique_rules.append(rule)
    # Explicit rules keep their own slots. Inferred rules are appended after
    # them and never merged into an explicit rule; the combined list still caps
    # at five.
    explicit_rules = unique_rules[:RULE_LIMIT]
    decision_rules = (explicit_rules + _inferred_rules(threads, explicit_rules))[:RULE_LIMIT]

    pitfalls_data = []
    seen_pitfalls = set()
    for sentence in pitfalls:
        cleaned = _clean_pitfall(sentence)
        key = cleaned.lower()
        if not cleaned or key in seen_pitfalls or key in seen_steps:
            continue
        seen_pitfalls.add(key)
        pitfalls_data.append({"pitfall": truncate(cleaned, 200), "avoid": "", "evidence": []})

    verification = [_clean_verification(sentence) for sentence in checks]
    verification = [item for item in verification if item][:4]
    if not verification:
        verification = [
            "Confirm the preconditions from the source thread still hold",
            "Run the smallest relevant test or check and compare it with the expected outcome",
            "Record the outcome with `t2s feedback <slug> --outcome worked` or `failed`",
        ]

    message_count = sum(thread.message_count for thread in threads)
    confidence = 0.42
    confidence += min(0.22, message_count / 80.0)
    confidence += 0.08 if decisions else 0.0
    confidence += 0.06 if checks else 0.0
    confidence += 0.05 if commands else 0.0
    confidence += 0.05 if len(steps) >= 3 else 0.0
    confidence -= 0.06 if len([s for s in sentences if s.endswith("?")]) > len(steps) else 0.0
    confidence = round(clamp(confidence, 0.2, 0.88), 2)

    message_ids = [message.id for thread in threads for message in thread.messages if message.id]
    participants = sorted({message.author for thread in threads for message in thread.messages})
    open_questions = []
    if not checks:
        open_questions.append("The source thread did not state an explicit verification step; confirm the expected result with the team.")
    if not decisions:
        open_questions.append("No explicit decision was recorded; the procedure may reflect one participant's suggestion rather than team consensus.")
    return {
        "title": truncate(title, 80),
        "summary": summary,
        "category": category,
        "tags": tags,
        "triggers": triggers,
        "when_to_use": [
            (trigger if re.match(r"^(?:when|if)\b", trigger, flags=re.I) else f"Use when the current task matches: {trigger}.")
            for trigger in triggers[:4]
        ] or [f"Use when working on {category_label(category).lower()} and the source conversation is relevant."],
        "when_not_to_use": ["Do not use when the source thread's environment or constraints differ materially from the current task."],
        "procedure": [{"step": step, "details": "", "evidence": []} for step in steps],
        "decision_rules": decision_rules,
        "pitfalls": pitfalls_data,
        "verification": verification,
        "examples": [],
        "open_questions": open_questions,
        "confidence": confidence,
        "provenance": {
            "message_ids": message_ids,
            "participants": participants,
            "thread_ids": [thread.id for thread in threads],
            "sources": [{"source": thread.source, "channel": thread.channel, "thread_id": thread.id} for thread in threads],
        },
        "extractor": "offline-v1",
        "category_scores": category_scores,
        "slug_hint": _slug_from_title(title),
    }