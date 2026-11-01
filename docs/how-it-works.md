# How it works

Thread to Skills is a local compiler:

```text
conversation export
      │
      ▼
ingest ──► redact ──► normalize ──► distill ──► render ──► index
                                                         │
                                                         ▼
                                              suggest / install / feedback
```

## 1. Workspace

`t2s init` creates a workspace (default `./.thread2skill`, overridable with `--home` or `T2S_HOME`):

```text
.thread2skill/
├── config.json      # provider, redaction, gap, custom patterns
├── index.db         # SQLite + FTS5
├── skills/          # generated skill bundles
├── threads/         # normalized, redacted threads
└── prompts/         # manual-provider prompts and failed LLM output
```

`config.json` is intentionally small:

```json
{
  "version": 1,
  "provider": "auto",
  "redact": true,
  "gap_minutes": 60,
  "redact_patterns": []
}
```

## 2. Ingest

`t2s ingest` accepts files, directories, and ZIPs:

| Input | Detection | Thread reconstruction |
|---|---|---|
| Slack workspace export | `channels.json` / `users.json` or channel folders | `thread_ts` replies; standalone messages grouped by quiet gap |
| DiscordChatExporter JSON | `guild` + `channel` + `messages` | `reference.messageId` reply chains |
| WhatsApp `.txt` | iOS or Android timestamp lines | quiet-gap windows; multiline messages joined |
| Telegram JSON | `messages` with `from` fields | `reply_to_message_id` chains; quiet-gap fallback |
| JSONL | one object per line | `thread_id` / `conversation_id`, otherwise quiet-gap windows |
| Generic JSON | `messages` list or `{thread: [...]}` | `thread_id` / `conversation_id`, otherwise a single conversation |
| Markdown | `.md` / `.markdown` | one document, section headings preserved |
| Plain text | `.txt` that is not a WhatsApp export | one document |

All timestamps are normalized to UTC ISO-8601. Slack and Discord mentions, channel links, and custom emoji are cleaned into readable text. Messages without text or attachments are dropped.

The default quiet gap is 60 minutes. Change it with `--gap-minutes` or `config.json`.

## 3. Redaction

Redaction runs before a normalized thread is written. Built-in patterns cover:

- private keys, GitHub/Slack/AWS/Google/OpenAI/Anthropic tokens, JWTs, bearer tokens
- credentials embedded in URLs
- email addresses and phone numbers
- private IPv4 addresses
- credit-card-shaped numbers (Luhn-checked)

Each replacement is labeled, and the CLI reports counts. Custom regexes can be added to `redact_patterns` in `config.json`; they are applied after the built-ins.

Redaction is a safety net, not a proof. Review generated skills before sharing them outside the team. `t2s init --no-redact` exists for synthetic or public data only.

## 4. Distill

Two modes:

### Offline (default when no API key is present)

A deterministic extractor scores sentences by cues:

- fixes: “the fix is”, “the solution is”, “root cause is”
- decisions: “we decided”, “decided:”, “we agreed”
- actions: “use”, “run”, “add”, “pin”, “rollback”, “deploy”, “check” …
- pitfalls: “don’t”, “never”, “gotcha”, “watch out”
- verification: “verify”, “confirm”, “assert”, “should see”

It then:

- splits explicit comma/colon checklists into ordered steps
- removes explanations and future promises from the procedure
- keeps conditions and rationale as decision rules
- deduplicates steps and pitfalls
- assigns a category from a stable taxonomy
- suggests tags and triggers
- extracts source message IDs and participants

Offline output is a **draft**. It is deterministic, testable, and useful, but it does not understand nuance the way a model can.

### LLM providers

| Provider | Flag | Credentials | Notes |
|---|---|---|---|
| OpenAI-compatible | `--provider openai` | `OPENAI_API_KEY` | `--base-url` for compatible gateways; JSON mode with fallback |
| Anthropic | `--provider anthropic` | `ANTHROPIC_API_KEY` | Messages API |
| Ollama | `--provider ollama` | none | `OLLAMA_HOST`, default `http://localhost:11434` |
| Command | `--provider command` | none | Prompt on stdin, JSON on stdout |
| Manual | `--provider manual` | none | Writes a prompt file for an external agent |
| Auto | `--provider auto` | — | OpenAI key, then Anthropic key, then offline |

The model is asked for one JSON object with the procedure schema. The response is parsed leniently (fenced JSON and trailing commas are tolerated), normalized, and validated before rendering. Raw failed responses are saved to `prompts/` for debugging.

## 5. Normalize

Both modes pass through the same normalization:

- category must be a known taxonomy key; unknown values become `uncategorized`
- tags are lowercased, hyphenated, deduplicated, and capped at 8
- triggers are deduplicated and capped
- confidence is clamped and blended with message volume
- a `description` is generated for the Agent Skills frontmatter
- provenance keeps thread IDs, message IDs, participants, and per-thread message counts

## 6. Duplicate detection

Before saving, the candidate is compared with existing active skills using Jaccard similarity over title, summary, and tags.

- `< 0.55` — create a new skill
- `>= 0.55` — behavior depends on `--on-duplicate`:
  - `warn` (default) — create the new skill and record the related skill
  - `skip` — do not create; exit code 2
  - `merge` — fold new steps, rules, pitfalls, tags, triggers, and provenance into the existing skill; increment its version
  - `new` — create anyway without the duplicate warning

## 7. Render

A skill bundle contains:

```text
skills/<slug>/
├── SKILL.md
├── references/thread.md
└── metadata.json
```

`SKILL.md` uses YAML frontmatter and these body sections:

- `# Title`
- `> Summary`
- `## When to use` / `## When not to use`
- `## Procedure`
- `## Decision rules`
- `## Pitfalls`
- `## Verification`
- `## Examples` (when present)
- `## Open questions` (when present)
- `## Source`

`references/thread.md` is the redacted transcript used as evidence. `metadata.json` is the machine-readable record used by the registry and future re-renders.

## 8. Index and suggest

SQLite stores threads, skills, and events. An FTS5 virtual table indexes skill name, description, tags, and body; if FTS5 is unavailable, search falls back to `LIKE`.

`t2s suggest` scores a prompt against active skills:

- trigger match: strong positive
- tag match: medium positive
- name/title/description token match: small positive
- normalized by prompt length
- blended with stored confidence and observed success rate
- inactive skills are heavily down-weighted

The default threshold is `0.18`; hooks use `0.22` to reduce noise. Suggestions are recorded as events and increment the `suggested` counter.

## 9. Install

`t2s install` copies the bundle into the native skill root for each harness and writes a `.thread2skill-install.json` marker. Existing unmanaged directories are skipped unless `--force` is used.

Cursor uses a `.mdc` rule file in `.cursor/rules` instead of a skill directory.

## 10. Hooks and feedback

Hooks call `t2s suggest` with the current prompt and inject the result in the host’s supported shape. See [hooks.md](hooks.md).

`t2s feedback` adjusts confidence:

| Outcome | Delta |
|---|---|
| `worked` | +0.05 |
| `failed` | −0.08 |
| `accepted` | +0.02 |
| `suggested` | 0 |

Confidence is clamped to `[0.03, 0.98]`. Three failures with zero successes set `needs-review`; a later success can restore `active` when successes outnumber failures.

## 11. Limits

- Offline extraction is heuristic; use an LLM for nuanced or contested threads.
- A thread can be wrong. The skill preserves provenance so a human can check it.
- Redaction cannot catch every sensitive value, especially unusual formats.
- Hook APIs differ by host and version; verify after installing.
- v0.1 does not rewrite skill content automatically. Merging new evidence is explicit.
- There is no remote sync, authentication, or multi-user server. The workspace is a local directory.