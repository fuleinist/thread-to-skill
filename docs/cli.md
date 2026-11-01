# CLI reference

```text
t2s [--home PATH] <command> [options]
```

`--home` selects the workspace. Without it, the CLI looks for `.thread2skill` in the current directory and its parents, then `T2S_HOME`, then `./.thread2skill`.

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Error (bad input, provider failure, validation error) |
| `2` | No-op: duplicate skipped, or manual prompt written |
| `130` | Interrupted |

---

## `t2s init`

Create a workspace.

```bash
t2s init
t2s init --provider offline
t2s init --no-redact
t2s init --force
```

| Option | Default | Meaning |
|---|---|---|
| `--provider` | `auto` | `auto`, `offline`, `openai`, `anthropic`, `ollama`, `command`, `manual` |
| `--no-redact` | off | Disable built-in redaction (synthetic/public data only) |
| `--force` | off | Update an existing workspace without deleting skills or threads |

Creates `config.json`, `index.db`, `skills/`, `threads/`, and `prompts/`.

---

## `t2s ingest`

Load exports into the local index.

```bash
t2s ingest ./slack-export
t2s ingest ./discord.json ./whatsapp.txt
t2s ingest ./export.zip --format auto
t2s ingest ./messages.jsonl --gap-minutes 30
t2s ingest ./slack-export --json
```

| Option | Default | Meaning |
|---|---|---|
| `--format` | `auto` | `auto`, `slack`, `discord`, `whatsapp`, `telegram`, `jsonl`, `json`, `markdown`, `text` |
| `--gap-minutes` | `60` | Split unthreaded messages on this quiet gap |
| `--channel` | from export | Override the channel name |
| `--title` | first line | Override the thread title |
| `--json` | off | Machine-readable output |

Each accepted thread is written to `threads/<id>.json` after redaction and upserted into SQLite.

---

## `t2s distill`

Turn one or more threads into a skill.

```bash
t2s distill latest
t2s distill f14de44f6ca5
t2s distill latest --provider offline
t2s distill latest --provider openai --model gpt-4o-mini
t2s distill latest --on-duplicate merge
t2s distill latest --install claude,agents
t2s distill latest --dry-run --json
```

| Option | Default | Meaning |
|---|---|---|
| `threads` | required | Thread IDs, unique prefixes, or `latest` |
| `--provider` | workspace default | Overrides `config.json` |
| `--model` | provider default | Model name |
| `--base-url` | provider default | OpenAI-compatible or Anthropic base URL |
| `--api-key-env` | provider default | Read the key from this environment variable |
| `--command` | — | Command provider; receives the prompt on stdin |
| `--timeout` | `120` | Provider timeout in seconds |
| `--category` | inferred | Force a taxonomy category |
| `--tags` | — | Comma-separated extra tags |
| `--slug` | generated | Explicit slug |
| `--on-duplicate` | `warn` | `warn`, `skip`, `merge`, `new` |
| `--install` | — | Comma-separated harnesses or `all` |
| `--scope` | `user` | `user` or `project` |
| `--project-root` | cwd | Project root for `project` scope |
| `--force` | off | Overwrite an existing unmanaged install |
| `--dry-run` | off | Print the normalized data without saving |
| `--json` | off | Machine-readable output |

Provider notes:

- `auto` uses `OPENAI_API_KEY`, then `ANTHROPIC_API_KEY`, then `offline`.
- `openai` also reads `OPENAI_BASE_URL`.
- `anthropic` also reads `ANTHROPIC_BASE_URL`.
- `ollama` reads `OLLAMA_HOST`.
- `command` reads the full prompt on stdin and must write a JSON object to stdout.
- `manual` writes a prompt to `prompts/` and exits 2.

---

## `t2s list`

```bash
t2s list
t2s list --category engineering/testing
t2s list --tag retry
t2s list --status active
t2s list --json
```

Columns: slug, category, confidence, version, status, uses, title.

---

## `t2s show`

```bash
t2s show checkout-integration-tests-fail
t2s show checkout-integration-tests-fail --raw
t2s show checkout-integration-tests-fail --json
```

`--raw` prints the full `SKILL.md`.

---

## `t2s search`

```bash
t2s search "flaky integration tests"
t2s search postgres --limit 5 --json
```

Uses FTS5 when available, otherwise `LIKE`.

---

## `t2s suggest`

Rank skills for a prompt. This is the command hooks call.

```bash
t2s suggest "integration tests are flaky with postgres"
t2s suggest "release checklist" --threshold 0.1 --limit 5
t2s suggest "on-call handover" --format json
```

| Option | Default | Meaning |
|---|---|---|
| `prompt` | stdin if empty | Prompt text |
| `--format` | `text` | `text`, `json`, `claude`, `cursor`, `deepseek` |
| `--threshold` | `0.18` | Minimum score |
| `--limit` | `3` | Maximum suggestions |
| `--category` | — | Restrict to a category |

No match means no output (exit 0), which keeps hooks quiet.

---

## `t2s feedback`

```bash
t2s feedback <slug> --outcome worked
t2s feedback <slug> --outcome failed --note "migration still drifted"
t2s feedback <slug> --outcome accepted
```

Outcomes: `worked`, `failed`, `accepted`, `suggested`.

Updates confidence, counters, `last_used_at`, and `needs-review` status. Records an event.

---

## `t2s install`

```bash
t2s install <slug> --harness claude
t2s install <slug> --harness all
t2s install <slug> --harness claude,agents --scope project --project-root .
t2s install <slug> --harness cursor --force
```

| Option | Default | Meaning |
|---|---|---|
| `--harness` | `agents` | Comma-separated harnesses or `all` |
| `--scope` | `user` | `user` or `project` |
| `--project-root` | cwd | Project root for `project` scope |
| `--force` | off | Replace an existing unmanaged directory |
| `--json` | off | Machine-readable output |

Harnesses: `agents`, `claude`, `cursor`, `openclaw`, `hermes`, `deepseek`, `codex`, `copilot`.

---

## `t2s hooks install`

```bash
t2s hooks install --harness all
t2s hooks install --harness claude --scope project
t2s hooks install --harness cursor --scope project --dry-run
t2s hooks install --harness hermes
t2s hooks install --harness openclaw
```

| Option | Default | Meaning |
|---|---|---|
| `--harness` | `all` | Comma-separated: `claude`, `cursor`, `deepseek`, `hermes`, `openclaw` |
| `--scope` | `user` | `user` or `project` |
| `--project-root` | cwd | Project root for `project` scope |
| `--dry-run` | off | Report paths without writing |
| `--json` | off | Machine-readable output |

Existing settings are backed up before writing. Managed entries are idempotent.

---

## `t2s validate`

```bash
t2s validate <slug>
t2s validate --all
```

Checks the Agent Skills layout, frontmatter, metadata, references, and likely secrets. Errors exit 1; warnings do not.

---

## `t2s stats`

```bash
t2s stats
t2s stats --json
```

Shows thread/skill counts, status distribution, category distribution, and recent events.

---

## `t2s doctor`

```bash
t2s doctor
t2s doctor --json
```

Checks Python version, workspace, SQLite file, `t2s` on `PATH`, and provider environment variables.

---

## `t2s version`

```bash
t2s version
t2s --version
```