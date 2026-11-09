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
t2s ingest ./slack-export --force
```

| Option | Default | Meaning |
|---|---|---|
| `--format` | `auto` | `auto`, `slack`, `discord`, `whatsapp`, `telegram`, `jsonl`, `json`, `markdown`, `text` |
| `--gap-minutes` | `60` | Split unthreaded messages on this quiet gap |
| `--channel` | from export | Override the channel name |
| `--title` | first line | Override the thread title |
| `--force` | off | Rewrite every thread even when it did not change |
| `--json` | off | Machine-readable output |

Each accepted thread is redacted and compared with the stored `content_hash`.
An unchanged thread is skipped: nothing is written to the database, its
`threads/<id>.json` file is left untouched, and `ingested_at` does not change.
A changed thread, a new thread, or any thread under `--force` is written to
`threads/<id>.json` and upserted into SQLite. If the normalized file is missing,
the thread is written again so the index and the file stay in sync. Every result
item carries a `status` of `added`, `updated`, or `skipped`.

The human output lists only added and updated threads, prints `no changes` when
everything was skipped, and ends with a summary of the three counts.

`--json` prints one object rather than a bare array:

```json
{
  "added": 0,
  "updated": 0,
  "skipped": 1,
  "threads": [
    {
      "id": "f14de44f6ca5",
      "source": "slack",
      "channel": "engineering",
      "title": "Checkout integration tests fail on CI",
      "messages": 10,
      "participants": 3,
      "started_at": "2026-10-30T00:26:40Z",
      "redactions": "github token, email",
      "status": "skipped"
    }
  ]
}
```

Each entry keeps the keys it had before plus `status`.

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
t2s distill latest --since 2026-10-01 --until 2026-10-07
t2s distill latest --participants alice,bob --exclude-bots
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
| `--since` | — | Keep messages dated on or after `YYYY-MM-DD` (inclusive) |
| `--until` | — | Keep messages dated on or before `YYYY-MM-DD` (inclusive) |
| `--participants` | — | Comma-separated authors; case-insensitive substring match |
| `--max-messages` | `0` | Keep the most recent N messages across the selected threads |
| `--exclude-bots` | off | Drop messages from automation accounts |
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

Filter notes:

- Filters run before prompt construction and token budgeting.
- `--max-messages` keeps chronological order and spans all selected threads.
- A filter that removes every message stops the run with an error. It never writes an empty skill.
- `metadata.json` records the window, the participants, and the message counts under `data.provenance.filters`. `SKILL.md` states the same slice in `## Source`.
- A skill distilled from a three-day window does not claim to cover the whole channel.

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

Archived skills are hidden unless you pass `--status archived`.

---

## `t2s show`

```bash
t2s show checkout-integration-tests-fail
t2s show checkout-integration-tests-fail --raw
t2s show checkout-integration-tests-fail --json
```

`--raw` prints the full `SKILL.md`.

---

## `t2s archive`

```bash
t2s archive checkout-integration-tests-fail
```

Sets `status=archived` and keeps the bundle on disk. Archived skills stay out of `t2s list`
and `t2s suggest`. `t2s list --status archived` shows them.

---

## `t2s unarchive`

```bash
t2s unarchive checkout-integration-tests-fail
```

Restores an archived skill to `active`.

---

## `t2s delete`

```bash
t2s delete checkout-integration-tests-fail --yes
t2s delete checkout-integration-tests-fail --yes --keep-thread
```

| Option | Default | Meaning |
|---|---|---|
| `--yes` | off | Skip the confirmation prompt |
| `--keep-thread` | off | Keep the source threads and provenance |

Delete moves the bundle to `.thread2skill/trash/<slug>-<stamp>/` and then removes the row from
`index.db`. It never shreds the bundle. Without `--keep-thread`, it also removes the source threads
that no other skill references, and moves those files into the same trash directory.

In a non-interactive shell the command refuses to run without `--yes`. In an interactive shell it
asks you to type the slug.

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

## `t2s hooks doctor`

```bash
t2s hooks doctor
t2s hooks doctor --harness claude --scope project
t2s hooks doctor --json
```

Checks each host that has a hook file. It reads the file, parses the JSON or
YAML, resolves the `t2s` command, and runs one `suggest` call with a sample
prompt. The host itself never runs.

| Option | Default | Meaning |
|---|---|---|
| `--harness` | `all` | Comma-separated: `claude`, `cursor`, `deepseek`, `hermes`, `openclaw` |
| `--scope` | `user` | `user` or `project` |
| `--project-root` | cwd | Project root for `project` scope |
| `--json` | off | Machine-readable output for CI |

| Status | Meaning |
|---|---|
| `ok` | The hook file is valid and the sample call returned a suggestion |
| `missing` | No hook file is installed for that host |
| `invalid` | The file is malformed, incomplete, or the command is wrong |
| `command-not-found` | The hook exists but `t2s` or `T2S_BIN` does not resolve |
| `no-match` | The sample call ran and found no skill above the threshold |
| `error` | The sample call failed or timed out |

Exit code is 1 when a detected hook is `invalid`, `command-not-found`, or
`error`. A `no-match` result is not a failure.

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
t2s stats --by-source
t2s stats --by-channel
t2s stats --timeline
t2s stats --by-source --by-channel --timeline
```

| Option | Default | Meaning |
|---|---|---|
| `--by-source` | off | Add a "By source" table |
| `--by-channel` | off | Add a "Top channels" table |
| `--timeline` | off | Add a "Timeline" table for the last 12 ISO weeks |
| `--json` | off | Machine-readable output |

Shows thread/skill counts, status distribution, category distribution, and recent events.

The three flags combine. Sections print in order after "By category" and before
"Recent events": By source, Top channels, Timeline.

- `By source` lists each source with its thread count and the number of distinct
  skills referencing at least one of its threads, sorted by threads then source.
  A skill that references threads from two sources counts in both rows.
- `Top channels` shows the top 10 channels with at least one skill, sorted by
  skills, then threads, then channel name.
- `Timeline` reports ingests (threads ingested in the week) and distills (skills
  created in the week) per ISO week, oldest first, ending with the current week.
  Week boundaries use local time.

A section with no rows prints one short line instead of a table. On an empty
workspace the human output prints a single hint line and no tables.

The JSON object always contains `by_source`, `by_channel`, and `timeline`; the
flags do not change the JSON payload. Existing keys stay unchanged.

---

## `t2s review`

```bash
t2s review
t2s review --json
t2s review --fix
t2s review --fix --yes
t2s review --stale-days 30
```

| Option | Default | Meaning |
|---|---|---|
| `--stale-days N` | `90` | Days without use before a skill is stale |
| `--confidence F` | `0.4` | Low-confidence threshold |
| `--duplicate-threshold F` | `0.55` | Similarity that makes a duplicate pair |
| `--conflict-threshold F` | `0.45` | Similarity that makes a conflict candidate |
| `--fix` | off | Archive needs-review skills that have zero successes |
| `--yes` | off | With `--fix`, record duplicate groups in metadata |
| `--json` | off | Machine-readable output |

Review reports the skills that need attention. Each reason comes from stored data.

| Reason | Rule |
|---|---|
| `stale` | `last_used_at` is older than the threshold. The check falls back to `updated_at` when the skill was never used. |
| `failing` | `failed` is greater than `worked`, or `status` is `needs-review`. Equal counts are not failing. |
| `unused` | `suggested` is above zero and `accepted` is zero. |
| `duplicate` | The skill is similar to a skill with higher confidence. |
| `conflicting` | Two near-duplicate skills state opposite decision rules about the same subject. |
| `low-confidence` | `confidence` is below the threshold and the skill has no recorded use. |

A skill can carry more than one reason. Reasons print in the order above.

The table shows the slug, the reasons, the age in days, and a suggested action. Age counts
from `last_used_at`, or from `updated_at` when the skill was never used.

Review reads only. It never writes and it never takes the workspace lock. `--fix` takes
the lock and applies two safe actions:

- It archives a skill only when `status` is `needs-review` and `worked` is zero.
- With `--yes`, it records duplicate groups in the skill metadata under `review.duplicate_of`.

`--fix` never deletes a file. It never merges a body. `t2s unarchive` reverses an archive.

The thresholds live under `review` in `config.json`. A flag overrides the config value.

```json
{
  "review": {
    "stale_days": 90,
    "confidence_threshold": 0.4,
    "duplicate_threshold": 0.55,
    "conflict_threshold": 0.45
  }
}
```

### JSON output

The object has a fixed shape:

- `checked`: the number of non-archived skills that review read.
- `stale_days`, `confidence_threshold`, `duplicate_threshold`, `conflict_threshold`: the values in use.
- `counts`: one key per reason, in reason order. A reason with no finding reports `0`.
- `findings`: one object per skill, sorted by slug.
- `applied`: `{"archived": [], "grouped": []}`. The arrays hold the results of this run.

Each finding has `slug`, `name`, `status`, `reasons`, `age_days`, `confidence`,
`worked`, `failed`, `suggested`, `accepted`, `detail`, and `action`. `age_days` is
`null` when no timestamp can be parsed.

The exit code is `0` when review runs, even when it finds problems. Gate automation
on the `counts` object or on the length of `findings`.

---

## `t2s doctor`

```bash
t2s doctor
t2s doctor --json
t2s doctor --migrate
```

Checks Python version, workspace, SQLite file, schema version, `t2s` on `PATH`,
and provider environment variables.

The `schema` line shows the database version. When the index is older than the
running tool, the line names the pending versions and points at `--migrate`.
`t2s doctor --migrate` backs up the database to `index.db.bak-<stamp>` and
applies the pending migrations. The command prints each applied version and the
backup path. A pre-versioning index is adopted at the current version, then the
command says so. The check fails when a database is newer than the tool.

---

## `t2s version`

```bash
t2s version
t2s --version
```