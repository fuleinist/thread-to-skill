# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses semantic versioning.

## [Unreleased]

### Added

- A Matrix adapter. `t2s ingest` now reads an unencrypted Matrix room export. Detection accepts a raw event list or an object with a `messages` list, and the markers include `m.room.message` and `m.room.encrypted`, so an encrypted export reaches this adapter instead of the generic JSON path. Only `m.room.message` events become messages, and membership, topic, state, and reaction events are skipped. A reply resolves through `content.m.relates_to.m.in_reply_to.event_id`, and a reply to a reply lands on the same root. `origin_server_ts` milliseconds become ISO-8601 UTC timestamps, and the sender localpart drops the server suffix. An export that holds Megolm ciphertext stops with an error that asks for a decrypted export, rather than producing an empty thread. `--format matrix` accepts both shapes.

- A Microsoft Teams adapter. `t2s ingest` now reads the Teams HTML "Download history" export and Graph API JSON. Teams HTML is recognized by a Teams marker, so a saved Teams page takes the Teams path and other saved pages keep the generic HTML path. Message containers are found by class token, `data-message-id`, or `data-tid`, because tenant markup varies. The author line, the timestamp, quoted replies, and page chrome are removed from the body, and the channel name comes from the export title when the file name does not carry it. Graph JSON is recognized by `createdDateTime` with `replyToId` or `messageType`, and nested `replies` are flattened once. In both shapes a reply to a reply resolves to the same root thread, an unknown `replyToId` never invents a parent, and an author that carries only a user id resolves through the export's participant list. `--format teams` accepts both shapes. `docs/how-it-works.md` and the README input lists name Teams.

- Locale packs let the offline extractor read non-English threads. The new `thread_to_skill.locales` package holds packs for English, Chinese, Japanese, German, and Spanish, and each pack defines its cue tuples and its extra stop words. `t2s distill --locale` chooses a pack with `en`, `zh`, `ja`, `de`, `es`, or `auto`, and an empty flag reads the `locale` key from `config.json`, which defaults to `auto`. Auto-detection recognizes Japanese kana and Chinese characters, votes on distinctive German and Spanish keywords, and answers `en` below the confidence threshold. An unknown name warns on stderr and falls back to English. English output is unchanged: the English pack is the verbatim copy of the tuples that were in `offline.py`, and those names remain importable from there. `docs/cli.md` documents the flag.

- `t2s import` adopts an existing `SKILL.md` into the workspace. The path may be one skill directory or a parent that holds several. Every skill passes `t2s validate` at error level first, and a rejected skill reports the validator's own errors while the rest still import. The command fills the fields the generator would supply: `metadata.category` from the body when the frontmatter has none, tags from the body keywords, and a `metadata.json` stub that records `source: "imported"`, an empty `threads` list, and a `null` confidence. Hand-written fields are kept. Re-importing is idempotent, because the copy is replaced and the index keeps one row. `--link` indexes the skill where it lives, writes nothing into the source directory, and records the external path. `--dry-run` prints the category, the tags, and the target path and writes nothing. An existing workspace skill with the same name needs `--force`. The validator stops expecting `references/thread.md` and a generated `metadata.confidence` once a skill is marked as imported, so `t2s validate` passes on adopted skills. `docs/cli.md` documents the command.

- `t2s export` and `t2s import-bundle` move a skill library between machines as one portable bundle. A bundle is a gzipped tar with a `manifest.json` and one directory per skill. The export is byte-identical for the same input, because member order, member metadata, and the gzip header are fixed and the manifest carries no timestamp. The manifest records the schema, the tool version, one row per skill, and a SHA-256 digest plus a byte size for every shipped file. `references/` holds raw transcripts, so it stays out of a bundle unless `--include-references` is passed, and `.versions/` snapshots never ship. `--slug` and `--category` narrow what ships. `t2s import-bundle` verifies the manifest, every digest, every size, and the full member list before it writes anything. It refuses unsafe paths, unlisted members, non-regular members, and skills that fail `t2s validate`. An existing slug needs `--force`. Imported skills appear in `list` and `suggest` at once, and a skill that shipped as archived stays archived. `docs/cli.md` documents both commands and the manifest format.

- Release automation for PyPI. A `v*` tag builds the sdist and the wheel, checks the tag against `pyproject.toml`, installs the wheel in a clean virtual environment, and publishes through PyPI Trusted Publishing. A manual run publishes to TestPyPI first. The workflow stops before any upload when the tag and the project version differ, and it attaches both files to the GitHub release. `docs/releasing.md` documents the one-time publisher setup and the dry run.

- `t2s distill --synthesize` groups related threads, merges them into one procedure, and reports contradictions. Threads are clustered by shared vocabulary, so two conversations about the same work produce one skill. A step that several threads state appears once, and `SKILL.md` lists its source thread IDs on a `Sources:` line. When two threads give opposite instructions about the same subject, both steps stay and a `## Conflicts` section names both sources. The section names the newer thread only when the threads carry timestamps. `metadata.json` records the cluster summary under `data.synthesis` and the contradictions under `data.conflicts`. Without the flag, the output is unchanged.

- `t2s distill` filters the input before prompt construction and token budgeting: `--since`, `--until`, `--participants`, `--max-messages`, and `--exclude-bots`. The window, the participant filter, and the kept message counts are stored under `data.provenance.filters` and printed in the `## Source` section. A filter that removes every message stops the run with a clear error instead of writing an empty skill.

- `t2s review` reports stale, failing, unused, duplicate, conflicting, and low-confidence skills. Each reason comes from stored data: `last_used_at`, `worked`, `failed`, `suggested`, `accepted`, and `confidence`. The command is read-only by default and does not take the workspace lock. `--fix` archives a skill only when `status` is `needs-review` and `worked` is zero. `--fix --yes` records duplicate groups in skill metadata. Neither path deletes a file or merges a body. Thresholds live under `review` in `config.json` and every threshold has a flag. `--json` emits a fixed shape with a `counts` object in reason order.
- `t2s archive`, `t2s unarchive`, and `t2s delete`. Archive sets `status=archived` and keeps the bundle. Unarchive restores the skill to `active`. Delete moves the bundle to `.thread2skill/trash/<slug>-<stamp>/` and then removes the index row, so nothing is shredded. `--keep-thread` retains the source threads and their provenance. Delete refuses to run without `--yes` in a non-interactive shell. Default `t2s list` and `t2s search` now hide archived skills.
- `t2s stats --by-source`, `--by-channel`, and `--timeline`: per-source thread and skill counts, a top-channels table limited to channels with at least one skill, and a last-12-ISO-weeks table of weekly ingest and distill counts. `t2s stats --json` always includes the `by_source`, `by_channel`, and `timeline` keys; an empty workspace prints a hint line instead of tables.
- `t2s ingest` skips threads whose `content_hash` matches the stored row: no database write, no `threads/<id>.json` rewrite, and `ingested_at` stays as it was. `--force` rewrites every thread anyway. Each result item reports a `status` of `added`, `updated`, or `skipped`, `t2s ingest --json` prints an object with the three counts plus the `threads` array, and the human output lists only changed threads with a summary of the counts.
- Schema versioning for `index.db`. New databases stamp `PRAGMA user_version` with the current version. Older databases back up to `index.db.bak-<stamp>` before any change. They then apply pending migrations inside one transaction. The tool refuses a database that is newer than itself, with an actionable message. `t2s doctor` reports the version. `t2s doctor --migrate` applies pending migrations on demand.

## [0.1.0] — 2026-11-01

First public version.

### Added

- `t2s` CLI with `init`, `ingest`, `distill`, `list`, `show`, `search`, `suggest`, `feedback`, `install`, `hooks`, `validate`, `stats`, `doctor`, and `version`.
- Adapters for Slack workspace exports, DiscordChatExporter JSON, WhatsApp text exports, Telegram JSON, JSONL, generic JSON, Markdown, plain text, and ZIPs.
- Thread reconstruction from Slack `thread_ts`, Discord reply references, Telegram replies, explicit thread IDs, and quiet-gap windows.
- Local redaction of private keys, GitHub/Slack/AWS/Google/OpenAI/Anthropic tokens, JWTs, bearer tokens, credentials in URLs, emails, phone numbers, private IPs, and Luhn-valid card numbers.
- Deterministic offline distillation of fixes, decisions, ordered steps, decision rules, pitfalls, verification checks, tags, triggers, and provenance.
- Optional LLM distillation through OpenAI-compatible endpoints, Anthropic, Ollama, an arbitrary command, or a manual prompt hand-off.
- Open Agent Skills output: `SKILL.md`, `references/thread.md`, and `metadata.json`.
- Stable category taxonomy and tag/trigger normalization.
- SQLite registry with FTS5 search when available and a `LIKE` fallback.
- Skill matching and proactive suggestions with a confidence/success blend.
- Duplicate detection with `warn`, `skip`, `merge`, and `new` modes.
- Installers for Claude Code, Cursor, OpenClaw, Hermes Agent, DeepSeek Harness, cross-agent `.agents`, Codex, and GitHub Copilot CLI skill roots.
- Hook writers for Claude Code `UserPromptSubmit`, Cursor `beforeSubmitPrompt`, DeepSeek Harness through the Claude Code bridge, Hermes Agent `pre_llm_call`, and OpenClaw `before_prompt_build`.
- Feedback outcomes (`worked`, `failed`, `accepted`, `suggested`) that adjust confidence and flag skills for review.
- Agent Skills validation, redaction checks, workspace stats, and environment diagnostics.
- Synthetic fixtures and a 37-test `unittest` suite.
- README with logo, architecture diagram, self-improvement loop, and screenshots.
- CI across Python 3.10–3.13 on Linux, macOS, and Windows.

### Known limitations

- Offline extraction is heuristic and should be reviewed; use an LLM provider for nuanced threads.
- Cursor’s `beforeSubmitPrompt` supports `continue` and `user_message`, not additional context. The suggestion is user-facing; skills are also installed as `.cursor/rules`.
- Hook APIs are host-version dependent. Verify the hook in the target host after installing.
- v0.1 does not autonomously rewrite skill content. Merging new evidence is explicit.
- There is no archive/delete CLI command yet; the database supports `archived` status.
- A PyPI release is not published yet; install from GitHub.

[0.1.0]: https://github.com/fuleinist/thread-to-skill/releases/tag/v0.1.0