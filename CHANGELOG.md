# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses semantic versioning.

## [Unreleased]

### Added

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