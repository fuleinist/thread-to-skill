"""Install proactive suggestion hooks for supported agent harnesses.

Each integration is a thin adapter over ``t2s suggest``. The hook process
prints nothing when no skill matches, so a quiet repository stays quiet.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .util import atomic_write_json, atomic_write_text, ensure_dir, now_iso

CLAUDE_COMMAND = "t2s suggest --format claude --threshold 0.22"
CURSOR_COMMAND = "t2s suggest --format cursor --threshold 0.22"
DEEPSEEK_COMMAND = "t2s suggest --format deepseek --threshold 0.22"
ALL_HOOK_HARNESSES = ["claude", "cursor", "deepseek", "hermes", "openclaw"]


@dataclass
class HookResult:
    harness: str
    path: Path
    status: str
    detail: str = ""

    def line(self) -> str:
        return f"{self.status:<9} {self.harness:<9} {self.path} {self.detail}".rstrip()


def _root(scope: str, project_root: Path | None) -> Path:
    return Path(project_root or Path.cwd()) if scope == "project" else Path.home()


def _backup(path: Path) -> None:
    if not path.exists():
        return
    stamp = now_iso().replace(":", "").replace("-", "")
    backup = path.with_name(path.name + f".bak-{stamp}")
    backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _is_ours(entry: object) -> bool:
    return "t2s suggest" in json.dumps(entry)


def _write(path: Path, data: dict, *, dry_run: bool) -> None:
    if dry_run:
        return
    _backup(path)
    atomic_write_json(path, data)


def _claude(scope: str, project_root: Path | None, *, dry_run: bool) -> HookResult:
    root = _root(scope, project_root)
    path = root / ".claude" / "settings.json" if scope == "project" else root / ".claude" / "settings.json"
    data = _read_json(path)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{path}: hooks must be an object")
    entries = hooks.setdefault("UserPromptSubmit", [])
    if not isinstance(entries, list):
        raise ValueError(f"{path}: hooks.UserPromptSubmit must be a list")
    entries[:] = [entry for entry in entries if not _is_ours(entry)]
    entries.append(
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": CLAUDE_COMMAND,
                    "timeout": 10,
                }
            ],
        }
    )
    _write(path, data, dry_run=dry_run)
    return HookResult("claude", path, "installed" if not dry_run else "dry-run", "UserPromptSubmit")


def _cursor(scope: str, project_root: Path | None, *, dry_run: bool) -> HookResult:
    root = _root(scope, project_root)
    path = root / ".cursor" / "hooks.json"
    data = _read_json(path)
    data["version"] = 1
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{path}: hooks must be an object")
    entries = hooks.setdefault("beforeSubmitPrompt", [])
    if not isinstance(entries, list):
        raise ValueError(f"{path}: hooks.beforeSubmitPrompt must be a list")
    entries[:] = [entry for entry in entries if not _is_ours(entry)]
    entries.append({"command": CURSOR_COMMAND, "timeout": 10})
    _write(path, data, dry_run=dry_run)
    return HookResult("cursor", path, "installed" if not dry_run else "dry-run", "beforeSubmitPrompt")


def _deepseek(scope: str, project_root: Path | None, *, dry_run: bool) -> HookResult:
    root = _root(scope, project_root)
    path = root / ".dsh" / "hooks.json"
    data = _read_json(path)
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{path}: hooks must be an object")
    entries = hooks.setdefault("UserPromptSubmit", [])
    if not isinstance(entries, list):
        raise ValueError(f"{path}: hooks.UserPromptSubmit must be a list")
    entries[:] = [entry for entry in entries if not _is_ours(entry)]
    entries.append(
        {
            "matcher": "",
            "hooks": [{"type": "command", "command": DEEPSEEK_COMMAND, "timeout": 10}],
        }
    )
    _write(path, data, dry_run=dry_run)
    guide = path.parent / "thread2skill-bridge.yaml"
    bridge = (
        "# Merge this row into the DeepSeek Harness plugin composition that mounts\n"
        "# @deepseek-ai/dsh-hooks-claude-code, then set configPath to .dsh/hooks.json.\n"
        "- name: '@deepseek-ai/dsh-hooks-claude-code'\n"
        "  config:\n"
        "    configPath: ./.dsh/hooks.json\n"
        "    projectDir: .\n"
    )
    if not dry_run:
        ensure_dir(guide.parent)
        atomic_write_text(guide, bridge)
    return HookResult("deepseek", path, "installed" if not dry_run else "dry-run", "Claude Code bridge")


HERMES_PLUGIN = '''"""Thread to Skills: inject relevant local skills before an LLM call."""

from __future__ import annotations

import os
import shutil
import subprocess

MAX_CHARS = 4000


def _binary() -> str:
    return os.environ.get("T2S_BIN") or shutil.which("t2s") or ""


def _suggest(prompt: str) -> str:
    binary = _binary()
    if not binary:
        return ""
    try:
        completed = subprocess.run(
            [binary, "suggest", prompt, "--format", "text", "--threshold", "0.22"],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()[:MAX_CHARS]


def pre_llm_call(user_message: str = "", **kwargs):
    if len(user_message or "") < 20:
        return None
    text = _suggest(user_message)
    return {"context": text} if text else None


def register(ctx):
    ctx.register_hook("pre_llm_call", pre_llm_call)
'''


def _hermes(scope: str, project_root: Path | None, *, dry_run: bool) -> HookResult:
    root = _root(scope, project_root)
    if scope == "project":
        path = root / ".hermes" / "plugins" / "thread2skill"
    else:
        hermes_home = Path(os.environ.get("HERMES_HOME", str(root / ".hermes"))).expanduser()
        path = hermes_home / "plugins" / "thread2skill"
    manifest = """name: thread2skill
version: 0.1.0
description: Inject relevant Thread to Skills skills before LLM calls
provides_hooks:
  - pre_llm_call
"""
    if not dry_run:
        ensure_dir(path)
        atomic_write_text(path / "plugin.yaml", manifest)
        atomic_write_text(path / "__init__.py", HERMES_PLUGIN)
    return HookResult("hermes", path, "installed" if not dry_run else "dry-run", "pre_llm_call plugin")


OPENCLAW_PLUGIN_MANIFEST = {
    "id": "thread2skill",
    "name": "Thread to Skills",
    "description": "Suggest locally distilled skills before each agent turn.",
    "activation": {"onStartup": True},
    "configSchema": {"type": "object", "additionalProperties": False},
}

OPENCLAW_PACKAGE = {
    "name": "thread2skill-openclaw",
    "version": "0.1.0",
    "private": True,
    "type": "module",
    "openclaw": {"extensions": ["./index.js"]},
}

OPENCLAW_INDEX = """import { execFileSync } from "node:child_process";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

function promptText(event) {
  if (typeof event?.prompt === "string") return event.prompt;
  if (typeof event?.body === "string") return event.body;
  const messages = Array.isArray(event?.messages) ? event.messages : [];
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (message?.role !== "user") continue;
    if (typeof message.content === "string") return message.content;
    if (Array.isArray(message.content)) {
      return message.content.map((part) => part?.text || "").join(" ");
    }
  }
  return "";
}

export default definePluginEntry({
  id: "thread2skill",
  name: "Thread to Skills",
  description: "Suggest locally distilled skills before each agent turn.",
  register(api) {
    api.on(
      "before_prompt_build",
      (event) => {
        const prompt = promptText(event).trim();
        if (prompt.length < 20) return;
        try {
          const binary = process.env.T2S_BIN || "t2s";
          const output = execFileSync(
            binary,
            ["suggest", prompt, "--format", "text", "--threshold", "0.22"],
            { encoding: "utf8", timeout: 8000 },
          ).trim();
          if (!output) return;
          return { prependContext: output.slice(0, 4000) };
        } catch {
          return;
        }
      },
      { eligibleTriggers: ["user"] },
    );
  },
});
"""


def _openclaw(scope: str, project_root: Path | None, *, dry_run: bool) -> HookResult:
    root = _root(scope, project_root)
    if scope == "project":
        path = root / ".openclaw" / "plugins" / "thread2skill"
    else:
        state = Path(os.environ.get("OPENCLAW_STATE_DIR", str(root / ".openclaw"))).expanduser()
        path = state / "plugins" / "thread2skill"
    if not dry_run:
        ensure_dir(path)
        atomic_write_json(path / "openclaw.plugin.json", OPENCLAW_PLUGIN_MANIFEST)
        atomic_write_json(path / "package.json", OPENCLAW_PACKAGE)
        atomic_write_text(path / "index.js", OPENCLAW_INDEX)
    return HookResult(
        "openclaw",
        path,
        "installed" if not dry_run else "dry-run",
        "before_prompt_build plugin; enable with `openclaw plugins install --link`",
    )


def install_hooks(
    harness: str,
    *,
    scope: str = "user",
    project_root: Path | None = None,
    dry_run: bool = False,
) -> list[HookResult]:
    if scope not in {"user", "project"}:
        raise ValueError("scope must be user or project")
    names = ALL_HOOK_HARNESSES if harness == "all" else [harness]
    results: list[HookResult] = []
    for name in names:
        if name == "claude":
            results.append(_claude(scope, project_root, dry_run=dry_run))
        elif name == "cursor":
            results.append(_cursor(scope, project_root, dry_run=dry_run))
        elif name == "deepseek":
            results.append(_deepseek(scope, project_root, dry_run=dry_run))
        elif name == "hermes":
            results.append(_hermes(scope, project_root, dry_run=dry_run))
        elif name == "openclaw":
            results.append(_openclaw(scope, project_root, dry_run=dry_run))
        else:
            raise ValueError(f"Unsupported hook harness: {name}")
    return results