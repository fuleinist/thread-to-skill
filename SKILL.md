---
name: thread-to-skill
description: "Turn Slack, Discord, WhatsApp, Telegram, JSONL, or Markdown conversations into reusable Agent Skills. Use when the user wants to preserve a team decision, incident fix, release checklist, onboarding procedure, or troubleshooting workflow from a chat export as a skill an agent can load later."
license: MIT
compatibility: "Agent Skills (Claude Code, Cursor, OpenClaw, Hermes Agent, DeepSeek Harness)"
metadata:
  category: "knowledge-management"
  tags: ["agent-skills", "conversation-mining", "slack", "discord", "whatsapp", "knowledge-management", "self-improvement"]
---

# Thread to Skills

Use this skill when a conversation contains a procedure worth keeping: an incident fix, a release checklist, a debugging path, a customer-support runbook, or a decision with a rationale.

## Procedure

1. **Check for an existing skill first.** Run `t2s search "<topic>"` and `t2s list`. If a similar skill exists, prefer `t2s distill <thread> --on-duplicate merge` over creating a duplicate.
2. **Ingest the export.** Run `t2s ingest <path>`. Supported inputs: Slack workspace export, DiscordChatExporter JSON, WhatsApp `.txt`, Telegram JSON, JSONL, generic JSON, Markdown, plain text, and ZIPs.
3. **Confirm redaction.** The CLI redacts tokens, keys, emails, phone numbers, private IPs, and credentials in URLs before storing the normalized thread. Review the reported redactions. Do not use `--no-redact` on real customer data.
4. **Distill.** Run `t2s distill <thread-id>`. Use the default `offline` provider for a deterministic draft, or configure an LLM provider for nuanced extraction. Read the result before installing it.
5. **Validate.** Run `t2s validate <slug>` and fix any errors. Check that the procedure is specific, the pitfalls are real, and the verification steps are observable.
6. **Install.** Run `t2s install <slug> --harness <host>` for the agent host in use. Install into `agents` for the cross-agent root.
7. **Make it proactive.** Run `t2s hooks install --harness <host>` to suggest the skill before a matching prompt. Keep `t2s` on `PATH` or set `T2S_BIN`.
8. **Record outcomes.** After applying a skill, run `t2s feedback <slug> --outcome worked` or `--outcome failed --note "..."`. Repeated failures flag the skill for review.

## Quality bar

- A skill is a procedure, not a meeting recap.
- Preserve the team's exact commands, versions, and constraints.
- Attribute disagreement instead of presenting it as consensus.
- Never invent a step that the thread does not support.
- Keep secrets out of `SKILL.md` and `references/thread.md`.

## Useful commands

```bash
t2s init --provider offline
t2s ingest ./slack-export
t2s distill latest
t2s list
t2s suggest "flaky integration tests"
t2s install <slug> --harness all
t2s hooks install --harness claude,cursor,openclaw,hermes,deepseek
t2s feedback <slug> --outcome worked
t2s validate --all
```