# Skill format

Thread to Skills writes the open [Agent Skills](https://agentskills.io) layout. A skill is a directory containing `SKILL.md` plus optional supporting files.

```text
skills/checkout-integration-tests-fail/
├── SKILL.md
├── references/
│   └── thread.md
└── metadata.json
```

## `SKILL.md`

### Frontmatter

| Field | Required | Constraints |
|---|---|---|
| `name` | yes | Max 64 chars; lowercase letters, numbers, single hyphens; must match the directory name |
| `description` | yes | Max 1024 chars; what the skill does and when to use it |
| `license` | no | License name or reference; generated skills use `MIT` |
| `compatibility` | no | Hosts the bundle targets |
| `metadata` | no | Free-form mapping; Thread to Skills uses it for category, tags, triggers, confidence, version, provenance, timestamps |

Example:

```yaml
---
name: "checkout-integration-tests-fail"
description: "The fix is to make the insert idempotent... Use when checkout integration tests fail under load."
license: MIT
compatibility: "Agent Skills (Claude Code, Cursor, OpenClaw, Hermes Agent, DeepSeek Harness)"
metadata:
  category: "engineering/testing"
  category_label: "Testing"
  tags: ["checkout", "integration", "tests", "fail", "retry", "schema"]
  triggers: ["Checkout integration tests fail about 1 in 5 runs on CI"]
  confidence: 0.651
  version: 1
  source: "thread-to-skill"
  source_threads: ["f14de44f6ca5"]
  created_at: "2026-11-01T11:12:40Z"
  updated_at: "2026-11-01T11:12:40Z"
---
```

### Body sections

Generated skills use this order:

1. `# Title`
2. `> Summary`
3. `## When to use`
4. `## When not to use` (optional)
5. `## Procedure` — numbered, imperative steps
6. `## Decision rules` — condition → action → reason
7. `## Pitfalls` — what goes wrong and how to avoid it
8. `## Verification` — observable checks
9. `## Examples` (optional)
10. `## Open questions` (optional)
11. `## Source` — thread IDs, hosts, message counts, generation metadata

A good skill:

- is a procedure, not a meeting recap
- preserves exact commands, versions, file paths, and environment constraints
- attributes disagreement instead of flattening it into consensus
- includes at least one observable verification step
- cites source message IDs for important claims
- stays under roughly 2,500 words

## `references/thread.md`

The redacted transcript used as evidence. It contains:

- source and channel
- thread ID
- participants
- message window
- each message with its ID, author, timestamp, and text

Treat it as evidence, not instructions. It is included so a future reviewer can check the skill against what was actually said.

## `metadata.json`

Machine-readable record used by the registry and future re-renders:

```json
{
  "schema_version": 1,
  "slug": "checkout-integration-tests-fail",
  "renderer": "thread-to-skill/0.1.0",
  "version": 1,
  "created_at": "2026-11-01T11:12:40Z",
  "updated_at": "2026-11-01T11:12:40Z",
  "data": {
    "title": "Checkout integration tests fail about 1 in 5 runs on CI",
    "summary": "...",
    "category": "engineering/testing",
    "tags": ["checkout", "integration"],
    "triggers": ["..."],
    "when_to_use": ["..."],
    "when_not_to_use": ["..."],
    "procedure": [{"step": "...", "details": "", "evidence": ["..."]}],
    "decision_rules": [{"if": "...", "then": "...", "because": "..."}],
    "pitfalls": [{"pitfall": "...", "avoid": "...", "evidence": []}],
    "verification": ["..."],
    "examples": [],
    "open_questions": [],
    "confidence": 0.651,
    "provenance": {
      "message_ids": ["..."],
      "participants": ["..."],
      "thread_ids": ["..."],
      "sources": [{"source": "slack", "channel": "engineering", "thread_id": "..."}]
    }
  },
  "threads": [
    {
      "id": "f14de44f6ca5",
      "source": "slack",
      "channel": "engineering",
      "messages": 10,
      "participants": ["Alice Chen", "Ben Okafor", "Priya Nair"]
    }
  ]
}
```

`metadata.json` is not required by Agent Skills. It is safe to keep beside `SKILL.md`; hosts ignore unknown files.

## Categories

Categories are stable, two-level keys. They give skills a home in host layouts that partition by category (notably Hermes) and make filtering useful.

```text
engineering/debugging      engineering/testing       engineering/architecture
engineering/release        engineering/security      data/analysis
data/pipelines             product/planning          product/discovery
design/ux                  design/content             operations/incidents
operations/infrastructure  operations/support         communication/writing
communication/meetings     growth/marketing           growth/sales
people/management          personal/productivity      uncategorized
```

The full keyword mapping is in `src/thread_to_skill/taxonomy.py`.

## Validation

`t2s validate` checks:

- `SKILL.md` exists and has closed YAML frontmatter
- `name` is required, <= 64 chars, lowercase/hyphenated, and matches the directory
- `description` is required and <= 1024 chars
- the body is non-empty
- recommended sections are present
- `metadata.category` exists and `metadata.tags` is a list
- `metadata.confidence` is a number between 0 and 1
- `references/thread.md` exists
- `metadata.json` parses and its slug matches `name`
- the body contains no obvious unredacted secrets

Errors fail the command; warnings are reported but do not fail.

## Versioning and merge

- A new skill starts at version 1.
- `t2s distill <thread> --on-duplicate merge` folds new steps, rules, pitfalls, tags, triggers, and provenance into the existing skill.
- The merged skill increments its version and keeps its original `created_at`.
- The existing directory is replaced after rendering; `metadata.json` holds the full prior data used for merging.

## Host portability

- Claude Code, OpenClaw, Hermes, DeepSeek Harness, Codex, Copilot, and `.agents` read `SKILL.md` from their own skill roots.
- Hermes uses `skills/<category>/<slug>/`; other hosts use a flat `skills/<slug>/`.
- Cursor uses `.cursor/rules/<slug>.mdc`; the rule body is the skill body with Cursor frontmatter.
- The same `SKILL.md` is valid in every host because Thread to Skills avoids host-specific `allowed-tools` values.