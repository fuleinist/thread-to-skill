---
name: Bug report
about: Report a reproducible problem
title: "[bug] "
labels: bug
assignees: ""
---

## What happened

A clear description of the problem.

## Reproduction

```bash
# exact commands
```

If an export is involved, describe its format and shape. **Do not attach a real conversation or real secrets.** Use a synthetic fixture.

## Expected behavior

What should have happened.

## Actual behavior

What happened instead. Include the full error output with secrets removed.

## Environment

- `t2s --version`:
- Python:
- OS:
- Provider: `offline` / `openai` / `anthropic` / `ollama` / `command` / `manual`
- Harness (if relevant): Claude Code / Cursor / OpenClaw / Hermes / DeepSeek Harness / other

## Validation

- [ ] I ran `t2s doctor`.
- [ ] I ran `t2s validate --all` for the generated skill.
- [ ] I removed secrets and personal data from this report.