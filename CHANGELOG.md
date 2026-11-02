# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses semantic versioning.

## [Unreleased]

### Added

- `t2s stats --by-source`, `--by-channel`, and `--timeline`: per-source thread and skill counts, a top-channels table limited to channels with at least one skill, and a last-12-ISO-weeks table of weekly ingest and distill counts. `t2s stats --json` always includes the `by_source`, `by_channel`, and `timeline` keys; an empty workspace prints a hint line instead of tables.

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