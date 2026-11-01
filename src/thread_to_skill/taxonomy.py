"""A small, opinionated category and tag taxonomy.

The taxonomy is intentionally stable. Categories give skills a home in
filesystems that partition skills by category (notably Hermes Agent), while
tags stay free-form so teams can use their own vocabulary.
"""

from __future__ import annotations

import re
from typing import Iterable

CATEGORIES: dict[str, dict[str, object]] = {
    "engineering/debugging": {
        "label": "Debugging",
        "keywords": ["bug", "debug", "error", "exception", "stack", "trace", "repro", "root cause", "fix", "broken", "crash", "flaky"],
    },
    "engineering/testing": {
        "label": "Testing",
        "keywords": ["test", "pytest", "jest", "vitest", "coverage", "fixture", "mock", "ci", "integration", "e2e", "assert", "flaky", "xdist", "duplicate key"],
    },
    "engineering/architecture": {
        "label": "Architecture",
        "keywords": ["architecture", "design", "adr", "schema", "interface", "api", "module", "service", "refactor", "migration", "boundary"],
    },
    "engineering/release": {
        "label": "Release & Delivery",
        "keywords": ["release", "releases", "deploy", "version", "changelog", "pipeline", "build", "publish", "tag", "hotfix", "canary", "checklist", "freeze", "smoke"],
    },
    "engineering/security": {
        "label": "Security Engineering",
        "keywords": ["security", "auth", "token", "secret", "credential", "vulnerability", "cve", "permission", "encryption", "xss", "csrf", "injection", "api key", "key rotation", "revoke"],
    },
    "data/analysis": {
        "label": "Data Analysis",
        "keywords": ["data", "dataset", "analysis", "metric", "sql", "query", "dashboard", "report", "chart", "pandas", "warehouse"],
    },
    "data/pipelines": {
        "label": "Data Pipelines",
        "keywords": ["pipeline", "etl", "elt", "airflow", "dbt", "ingest", "batch", "stream", "backfill", "schema drift"],
    },
    "product/planning": {
        "label": "Product Planning",
        "keywords": ["roadmap", "requirement", "spec", "scope", "priority", "milestone", "launch", "feature", "acceptance", "prd"],
    },
    "product/discovery": {
        "label": "Product Discovery",
        "keywords": ["user", "customer", "interview", "research", "feedback", "problem", "opportunity", "persona", "jtbd"],
    },
    "design/ux": {
        "label": "Design & UX",
        "keywords": ["design", "ux", "ui", "figma", "prototype", "usability", "accessibility", "a11y", "flow", "interaction"],
    },
    "design/content": {
        "label": "Content Design",
        "keywords": ["copy", "content", "tone", "voice", "writing", "docs", "documentation", "editorial", "style guide"],
    },
    "operations/incidents": {
        "label": "Incident Response",
        "keywords": ["incident", "outage", "sev", "oncall", "postmortem", "escalation", "mitigation", "status page", "downtime", "rollback", "roll back", "prod", "500", "5xx"],
    },
    "operations/infrastructure": {
        "label": "Infrastructure",
        "keywords": ["infra", "infrastructure", "terraform", "kubernetes", "k8s", "docker", "aws", "gcp", "azure", "server", "network", "dns"],
    },
    "operations/support": {
        "label": "Support Operations",
        "keywords": ["support", "ticket", "customer issue", "troubleshoot", "escalate", "sla", "refund", "account", "bug report"],
    },
    "communication/writing": {
        "label": "Writing & Communication",
        "keywords": ["write", "draft", "email", "announcement", "update", "summary", "memo", "blog", "message", "stakeholder"],
    },
    "communication/meetings": {
        "label": "Meetings",
        "keywords": ["meeting", "agenda", "notes", "decision", "action item", "standup", "retro", "sync", "follow-up"],
    },
    "growth/marketing": {
        "label": "Marketing",
        "keywords": ["marketing", "campaign", "seo", "content", "social", "launch", "newsletter", "growth", "analytics", "positioning"],
    },
    "growth/sales": {
        "label": "Sales",
        "keywords": ["sales", "prospect", "lead", "pricing", "proposal", "crm", "pipeline", "objection", "demo", "contract"],
    },
    "people/management": {
        "label": "People & Management",
        "keywords": ["hiring", "interview", "performance", "feedback", "manager", "team", "one-on-one", "promotion", "onboarding"],
    },
    "personal/productivity": {
        "label": "Personal Productivity",
        "keywords": ["personal", "workflow", "habit", "calendar", "inbox", "task", "todo", "note", "automation", "routine"],
    },
    "uncategorized": {
        "label": "Uncategorized",
        "keywords": [],
    },
}

TAG_RE = re.compile(r"[^a-z0-9]+")
MAX_TAGS = 8


def normalize_category(value: str | None) -> str:
    if not value:
        return "uncategorized"
    raw = value.strip().lower().replace(" ", "-")
    if raw in CATEGORIES:
        return raw
    if "/" not in raw:
        for key in CATEGORIES:
            if key.endswith("/" + raw):
                return key
    return "uncategorized"


def category_label(key: str) -> str:
    return str(CATEGORIES.get(key, CATEGORIES["uncategorized"])["label"])


def category_root(key: str) -> str:
    return normalize_category(key).split("/", 1)[0]


def score_category(text: str) -> tuple[str, dict[str, int]]:
    low = (text or "").lower()
    scores: dict[str, int] = {}
    for key, meta in CATEGORIES.items():
        if key == "uncategorized":
            continue
        keywords = meta.get("keywords", [])
        score = 0
        for keyword in keywords:  # type: ignore[union-attr]
            if " " in keyword:
                score += low.count(keyword) * 3
            else:
                score += len(re.findall(r"\b" + re.escape(keyword) + r"\b", low)) * 2
        if score:
            scores[key] = score
    if not scores:
        return "uncategorized", {}
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return ordered[0][0], dict(ordered)


def normalize_tag(value: str) -> str:
    tag = TAG_RE.sub("-", (value or "").strip().lower()).strip("-")
    tag = re.sub(r"-{2,}", "-", tag)
    return tag[:32].strip("-")


def normalize_tags(values: Iterable[str], extra: Iterable[str] = ()) -> list[str]:
    seen: list[str] = []
    for value in list(values) + list(extra):
        tag = normalize_tag(str(value))
        if tag and tag not in seen:
            seen.append(tag)
        if len(seen) >= MAX_TAGS:
            break
    return seen


def all_category_keys() -> list[str]:
    return list(CATEGORIES.keys())


def describe_categories() -> str:
    lines = []
    for key, meta in CATEGORIES.items():
        if key == "uncategorized":
            continue
        lines.append(f"- {key}: {meta['label']} — {', '.join(meta['keywords'][:8])}")  # type: ignore[index]
    return "\n".join(lines)