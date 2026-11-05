# Architecture

## Module map

```text
src/thread_to_skill/
├── cli.py            # argparse CLI, output, command handlers
├── config.py         # workspace discovery, config.json
├── models.py         # Message, Thread, Skill
├── util.py           # slugs, timestamps, hashing, JSON, tokenization
├── taxonomy.py       # stable categories, tags, category scoring
├── redact.py         # built-in and custom redaction
├── adapters/
│   ├── loader.py     # format detection and dispatch
│   ├── common.py     # thread windows, mention cleanup, generic messages
│   ├── slack.py      # Slack workspace exports
│   ├── discord.py    # DiscordChatExporter JSON
│   ├── whatsapp.py   # iOS/Android text exports
│   └── generic.py    # Telegram, JSONL, JSON, Markdown, text
├── providers.py      # OpenAI-compatible, Anthropic, Ollama, command, manual
├── offline.py        # deterministic cue-based extractor
├── distill.py        # prompt building, normalization, dedupe, save
├── render.py         # SKILL.md, references, metadata.json
├── store.py          # SQLite schema, FTS5, events, stats
├── search.py         # matching, suggestions, duplicate detection
├── install.py        # harness skill roots and Cursor rules
├── hooks.py          # Claude, Cursor, DeepSeek, Hermes, OpenClaw
├── feedback.py       # confidence and review status
└── validate.py       # Agent Skills validation and redaction check
```

## Data flow

```text
export path
   │
   ▼
loader.detect_format ──► adapter ──► Thread(messages[])
   │
   ▼
redact_thread ──► threads/<id>.json
   │
   ▼
store.add_thread ──► threads table
   │
   ▼
distill_threads ──► offline_distill OR provider.complete
   │
   ▼
normalize_data ──► duplicate check ──► save_skill
   │
   ▼
render.write_skill_bundle ──► skills/<slug>/
   │
   ▼
store.save_skill ──► skills + skills_fts
```

## Storage

`index.db` uses WAL mode.

### Schema versioning

`PRAGMA user_version` holds the schema version. `store.SCHEMA_VERSION` is the
version this build expects. `store.MIGRATIONS` is an ordered tuple of
`(version, apply)` pairs, and version 1 is the baseline schema.

Opening an older database applies every pending migration. The sequence is:

1. Copy the database to `index.db.bak-<stamp>` with the SQLite backup API.
2. Run the pending migrations inside one `BEGIN IMMEDIATE` transaction.
3. Stamp `user_version` after each step.
4. Commit. A failure rolls back and leaves the backup in place.

The tool refuses a database that is newer than itself. A downgrade could lose
columns it does not know about. `t2s doctor` reports the state, and
`t2s doctor --migrate` performs the upgrade on demand.

Rules for new migrations:

- Append a migration. Never edit a released entry.
- Bump `SCHEMA_VERSION` in the same commit.
- Copy data forward. Keep the old table until the transaction commits, then drop it.
- Never recreate a table destructively to add a column.

### `threads`

| Column | Purpose |
|---|---|
| `id` | Stable hash of source, channel, thread key |
| `source` | `slack`, `discord`, `whatsapp`, `telegram`, `jsonl`, `json`, `markdown`, `text` |
| `channel` | Channel, chat, or document name |
| `title` | Human title |
| `started_at`, `ended_at` | Normalized ISO timestamps |
| `message_count` | Number of messages |
| `participants` | JSON array |
| `path` | Original export path |
| `content_hash` | Hash of the normalized transcript |
| `ingested_at` | Last ingest time |
| `extra` | Source-specific metadata |

### `skills`

| Column | Purpose |
|---|---|
| `slug` | Primary key and directory name |
| `name` | Agent Skills `name` (same as slug) |
| `title`, `description`, `summary` | Human-facing fields |
| `category` | Taxonomy key |
| `tags`, `triggers` | JSON arrays |
| `version` | Incremented on merge |
| `confidence` | 0–1 ranking signal |
| `status` | `active`, `archived`, `needs-review` |
| `path` | Bundle directory |
| `source_threads` | JSON array |
| `created_at`, `updated_at`, `last_used_at` | Timestamps |
| `suggested`, `accepted`, `worked`, `failed` | Counters |
| `body` | Full `SKILL.md` text for FTS |
| `metadata` | Full structured record |

### `events`

Append-only log of `distilled`, `suggested`, and `feedback` events. Used by `t2s stats` and future review workflows.

### `skills_fts`

FTS5 table over `name`, `description`, `tags`, and `body`. Search uses `bm25` ranking when available.

## Design decisions

### Local-first, no server

The tool is a CLI and a directory. There is no account, API, daemon, or database server. A team can put `.thread2skill/` on a shared drive if it wants, but the default assumes one user.

### Zero runtime dependencies

The standard library covers HTTP, JSON, SQLite, regex, and CLI parsing. This keeps installation trivial and makes the test suite fast. Pillow is only used by `scripts/make_assets.py`, not by the package.

### Open format over proprietary store

The primary output is `SKILL.md` in the Agent Skills layout. SQLite is an index, not the source of truth. Deleting the index does not delete skills.

### Deterministic baseline, optional model

Offline extraction makes the tool testable and usable without keys. LLM mode is an enhancement, not a requirement. Both paths share normalization, rendering, and validation.

### Provenance is part of the product

Every skill records source threads, message IDs, participants, and a redacted transcript. A skill without evidence is a liability.

### Human-governed improvement

Hooks and feedback automate discovery and ranking. Content changes are explicit (`--on-duplicate merge` or a new distill). This avoids a loop where an agent silently rewrites its own instructions.

## Extension points

### Add an adapter

1. Implement `load_<format>(path, gap_minutes, channel, title) -> list[Thread]` in `adapters/`.
2. Add detection in `adapters/loader.py`.
3. Add a fixture under `tests/fixtures/`.
4. Add detection and loading tests.

### Add a provider

1. Subclass `Provider` in `providers.py`.
2. Implement `complete(system, prompt, max_tokens, timeout) -> str`.
3. Add it to `get_provider`.
4. Add a unit test for request shaping or response parsing (no network in CI).

### Add a harness

1. Add the skill root to `install.harness_skill_dir`.
2. Add the hook writer to `hooks.py`.
3. Document the exact event and output shape in `docs/hooks.md`.
4. Add a test that installs into a temporary project root and asserts file contents.

## Security boundaries

- Ingest and render are local file operations.
- LLM mode sends redacted text to the configured provider; there is no other network call.
- Hooks execute `t2s` as the current user. Hook files are merged, backed up, and reviewed in tests.
- Skill content is untrusted input for an agent. Treat a generated skill like a pull request: review before installing into a shared host.
- Redaction is best-effort. The validator flags likely secrets in generated skills, but it cannot prove absence.

## Testing

The suite uses `unittest` and synthetic fixtures:

- adapter detection and thread reconstruction
- redaction patterns and idempotency
- offline extraction, normalization, rendering, validation
- CLI end-to-end: init, ingest, distill, list, show, search, suggest, feedback, install, hooks, validate, stats, doctor
- duplicate merge/skip behavior
- hook merge and idempotency

Run:

```bash
python -m unittest discover -s tests -v
```