# FAQ

## Is this just RAG over chat history?

No. RAG retrieves chunks at query time. Thread to Skills compiles a procedure once: it names the capability, orders the steps, records decision rules and pitfalls, and writes a stable `SKILL.md`. The agent reads the skill, not a pile of similar messages.

## Does it upload my conversations?

In `offline` mode, no. Ingest, redaction, extraction, indexing, and rendering are local. There is no telemetry and no account.

If you choose an API provider, the **redacted** thread is sent to that provider. That is the only network call the tool makes.

## Which exports are supported?

Slack workspace exports, DiscordChatExporter JSON, WhatsApp `.txt` (iOS/Android), Telegram JSON, JSONL, generic JSON, Markdown, plain text, and ZIPs.

If your platform is not supported, export to JSONL with `id`, `timestamp`, `author`, `text`, and `thread_id` fields.

## How good is the offline extractor?

It is a deterministic draft. It is good at explicit fixes, decisions, ordered checklists, pitfalls, and verification steps. It is weaker at implicit context, sarcasm, and disagreement. For important skills, use an LLM provider and review the output.

## Which LLM providers work?

OpenAI-compatible endpoints, Anthropic, Ollama, an arbitrary command, and a manual prompt hand-off. See [CLI reference](cli.md#t2s-distill).

## How do I update a skill when a new thread appears?

```bash
t2s distill <new-thread> --on-duplicate merge
```

This folds new steps, rules, pitfalls, tags, triggers, and provenance into the existing skill, increments its version, and keeps the original creation date.

## How do I avoid duplicate skills?

The converter compares the candidate with active skills using title, summary, and tags. At 55% similarity it warns. Use:

- `--on-duplicate skip` to refuse
- `--on-duplicate merge` to fold
- `--on-duplicate new` to create anyway

## Why did my hook not fire?

Check, in order:

1. `t2s suggest "<the prompt>" --threshold 0.22` returns text.
2. `t2s` is on `PATH`, or `T2S_BIN` points to it.
3. The hook file has the expected event and command.
4. The host was restarted or reloaded after the hook was written.
5. For OpenClaw, `plugins.entries.thread2skill.hooks.allowConversationAccess` is true.
6. For Hermes, the plugin is enabled and passes `hermes plugins doctor`.
7. For DeepSeek Harness, the Claude Code bridge is mounted with the generated `configPath`.

See [hooks.md](hooks.md).

## Does Cursor inject the suggestion into the model context?

Not through `beforeSubmitPrompt` in the documented API. That event supports `continue` and `user_message`, not an additional-context field. Thread to Skills surfaces the suggestion to the user and installs the skill as a `.cursor/rules/*.mdc` rule. Other hosts support direct context injection.

## Is the loop fully autonomous?

No. v0.1 automates retrieval, proactive matching, confidence, and review flags. Content changes are explicit: `t2s distill --on-duplicate merge` or a new distill. This is deliberate. An agent silently rewriting its own instructions is a safety and quality problem.

## Can I delete or archive a skill?

Yes. Run `t2s archive <slug>` to hide a skill but keep its files. Run `t2s unarchive <slug>` to
restore it. Run `t2s delete <slug> --yes` to retire a skill. Delete moves the bundle to
`.thread2skill/trash/` and removes the index row, so you can recover the files later. Add
`--keep-thread` to keep the source threads and provenance.

## Does it work on Windows?

Yes. The test suite runs on Windows, macOS, and Linux. `t2s` avoids POSIX-only path assumptions. Hook commands are plain `t2s` invocations.

## Does it need Node or a database server?

No. Python 3.10+ and the standard library. SQLite is embedded. Node is only needed if you use a Node-based harness plugin, which the harness already provides.

## Is redaction guaranteed?

No. It is a safety net. It catches common tokens, keys, emails, phone numbers, private IPs, credentials in URLs, and Luhn-valid card numbers. Unusual formats can slip through. Review generated skills before sharing them.

## Can I use generated skills commercially?

The converter is MIT. Generated skills are yours, subject to the rights and terms of the source conversation. Do not publish a skill built from content you do not have the right to share.

## Why “thread-to-skill” and “Thread to Skills”?

`thread-to-skill` is the package and CLI name. “Thread to Skills” is the product name. The CLI is `t2s`.

## How do I contribute?

See [CONTRIBUTING.md](../CONTRIBUTING.md). New adapters, providers, harness integrations, fixtures, and docs are welcome. Keep runtime dependencies at zero unless there is a strong reason.