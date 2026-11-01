# Harness hooks

Thread to Skills can suggest relevant skills before the agent answers. The integration is intentionally thin: every hook calls `t2s suggest` with the current prompt, and the host injects or displays the result in its own supported shape.

## Common contract

```bash
t2s suggest "<prompt>" --format <text|json|claude|cursor|deepseek> [--threshold 0.22]
```

- If no skill matches, the command prints nothing and exits 0. Hooks stay quiet.
- If `t2s` is not on `PATH`, set `T2S_BIN` to its absolute path.
- The hook should time out quickly. The generated configs use 10 seconds.
- Suggestions are recorded as events. Hook-driven suggestions therefore count toward the `suggested` counter.

Install all supported hook configs:

```bash
t2s hooks install --harness all
```

Or one at a time:

```bash
t2s hooks install --harness claude --scope project
t2s hooks install --harness cursor --scope project
t2s hooks install --harness deepseek --scope project
t2s hooks install --harness hermes
t2s hooks install --harness openclaw
```

Existing files are backed up before writing. Managed entries are removed and re-added, so re-running does not duplicate them.

---

## Claude Code

**File:** `~/.claude/settings.json` (user) or `<project>/.claude/settings.json` (project)

`t2s suggest --format claude` emits:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "Relevant skills from `thread-to-skill`: ..."
  }
}
```

Generated entry:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "t2s suggest --format claude --threshold 0.22",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

Claude Code treats `additionalContext` as extra prompt context. The skill files themselves live in `~/.claude/skills/<slug>/`.

---

## Cursor

**File:** `<project>/.cursor/hooks.json` or `~/.cursor/hooks.json`

`t2s suggest --format cursor` emits:

```json
{
  "continue": true,
  "user_message": "Relevant skills from `thread-to-skill`: ..."
}
```

Generated entry:

```json
{
  "version": 1,
  "hooks": {
    "beforeSubmitPrompt": [
      {
        "command": "t2s suggest --format cursor --threshold 0.22",
        "timeout": 10
      }
    ]
  }
}
```

**Important limitation:** Cursor’s `beforeSubmitPrompt` output supports `continue` and `user_message`, but not an additional-context field. The suggestion is therefore surfaced to the user, not injected into the model context. Installed skills also land in `.cursor/rules/*.mdc`, where Cursor can discover them as rules. If Cursor adds an injection field for this event, `t2s` can emit it without changing the hook shape.

Cursor rule install:

```bash
t2s install <slug> --harness cursor --scope project
```

This writes `<project>/.cursor/rules/<slug>.mdc` with `alwaysApply: false`.

---

## DeepSeek Harness

**File:** `<project>/.dsh/hooks.json` (or user-level `~/.dsh/hooks.json`)

DeepSeek Harness supports Claude Code-compatible command hooks through the `@deepseek-ai/dsh-hooks-claude-code` bridge. `t2s hooks install --harness deepseek` writes:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "t2s suggest --format deepseek --threshold 0.22",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

It also writes `thread2skill-bridge.yaml` beside the hook file:

```yaml
- name: '@deepseek-ai/dsh-hooks-claude-code'
  config:
    configPath: ./.dsh/hooks.json
    projectDir: .
```

Merge that row into the plugin composition that DeepSeek Harness uses, then start a session in the project. The bridge runs the command and attaches the returned context. Verify with a prompt that should match a skill.

Skills install to `.dsh/skills` (project) or `${DSH_HOME:-~/.dsh}/skills` (user).

---

## Hermes Agent

**Directory:** `~/.hermes/plugins/thread2skill/` (or `${HERMES_HOME}/plugins/thread2skill/`)

Hermes plugins use `plugin.yaml` plus a Python module with `register(ctx)`. `t2s hooks install --harness hermes` writes:

```yaml
name: thread2skill
version: 0.1.0
description: Inject relevant Thread to Skills skills before LLM calls
provides_hooks:
  - pre_llm_call
```

```python
def pre_llm_call(user_message: str = "", **kwargs):
    if len(user_message or "") < 20:
        return None
    text = _suggest(user_message)
    return {"context": text} if text else None


def register(ctx):
    ctx.register_hook("pre_llm_call", pre_llm_call)
```

Enable and validate:

```bash
hermes plugins doctor ~/.hermes/plugins/thread2skill
hermes plugins enable thread2skill
```

Hermes injects the returned context into the current user message. It runs in both CLI and gateway sessions. Skills install to `~/.hermes/skills/<category>/<slug>/`, matching Hermes’ category-partitioned layout.

---

## OpenClaw

**Directory:** `~/.openclaw/plugins/thread2skill/` (or `${OPENCLAW_STATE_DIR}/plugins/thread2skill/`)

OpenClaw plugins use `openclaw.plugin.json`, a `package.json`, and an entry module. `t2s hooks install --harness openclaw` writes:

```json
{
  "id": "thread2skill",
  "name": "Thread to Skills",
  "description": "Suggest locally distilled skills before each agent turn.",
  "activation": { "onStartup": true },
  "configSchema": { "type": "object", "additionalProperties": false }
}
```

The plugin registers `before_prompt_build` and returns:

```js
return { prependContext: output.slice(0, 4000) };
```

Install and enable:

```bash
openclaw plugins install --link ~/.openclaw/plugins/thread2skill --force
openclaw plugins enable thread2skill
```

Non-bundled plugins need conversation access for prompt hooks. Merge this into `openclaw.json`:

```json
{
  "plugins": {
    "entries": {
      "thread2skill": {
        "enabled": true,
        "hooks": { "allowConversationAccess": true }
      }
    }
  }
}
```

Then reload:

```bash
openclaw plugins reload thread2skill
openclaw plugins inspect thread2skill --runtime --json
```

OpenClaw skills install to `~/.openclaw/skills` (user) or `skills/` in the workspace (project).

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Hook runs but nothing appears | `t2s suggest "<prompt>" --threshold 0.22` must return text. Check `t2s list` and `t2s doctor`. |
| `t2s: command not found` | Put `t2s` on `PATH`, or set `T2S_BIN` to its absolute path. |
| Hook duplicated after reinstall | Re-run `t2s hooks install`; managed entries are removed first. Restore the `.bak-*` file if a merge went wrong. |
| Claude Code ignores context | Confirm the JSON shape includes `hookSpecificOutput.additionalContext`. Check the hook’s timeout. |
| Cursor shows nothing | `beforeSubmitPrompt` may only display `user_message` when `continue` is false. Use installed `.cursor/rules` for discovery, or ask the agent to search `t2s`. |
| Hermes plugin not loaded | Run `hermes plugins doctor` and `hermes plugins enable thread2skill`. |
| OpenClaw plugin not firing | Check `plugins.entries.thread2skill.hooks.allowConversationAccess` and `openclaw plugins inspect ... --runtime`. |
| DeepSeek hook not firing | Confirm the `dsh-hooks-claude-code` bridge is mounted with the generated `configPath`. |

Hooks are trusted local code. Review the generated files before enabling them in a shared environment.