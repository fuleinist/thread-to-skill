"""Install proactive suggestion hooks for supported agent harnesses.

Each integration is a thin adapter over ``t2s suggest``. The hook process
prints nothing when no skill matches, so a quiet repository stays quiet.

``t2s hooks doctor`` verifies an installed hook without running the host: the
file is present in the scope the host reads, it parses, the command it names
resolves to a real binary, and one sample ``suggest`` call reaches the
workspace from the working directory the host would use.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from .util import atomic_write_json, atomic_write_text, ensure_dir, now_iso, truncate

CLAUDE_COMMAND = "t2s suggest --format claude --threshold 0.22"
CURSOR_COMMAND = "t2s suggest --format cursor --threshold 0.22"
DEEPSEEK_COMMAND = "t2s suggest --format deepseek --threshold 0.22"
ALL_HOOK_HARNESSES = ["claude", "cursor", "deepseek", "hermes", "openclaw"]

# The event each host fires for a proactive suggestion. Used by the doctor to
# name where an entry was found.
HOOK_EVENTS = {
    "claude": "UserPromptSubmit",
    "cursor": "beforeSubmitPrompt",
    "deepseek": "UserPromptSubmit",
    "hermes": "pre_llm_call",
    "openclaw": "before_prompt_build",
}

# Every host reads its config from a project root or from the user's home. Some
# hosts keep the plugin directory under an environment-selected root.
DEFAULT_CONFIG_DIRS = {
    "claude": ".claude",
    "cursor": ".cursor",
    "deepseek": ".dsh",
    "hermes": ".hermes",
    "openclaw": ".openclaw",
}
HOST_ROOT_ENV = {"hermes": "HERMES_HOME", "openclaw": "OPENCLAW_STATE_DIR"}


@dataclass
class HookResult:
    harness: str
    path: Path
    status: str
    detail: str = ""

    def line(self) -> str:
        return f"{self.status:<9} {self.harness:<9} {self.path} {self.detail}".rstrip()


@dataclass
class HookFile:
    """One file a host reads. `required` files must exist for the hook to work."""

    path: Path
    required: bool = True


def _host_root(harness: str, scope: str, project_root: Path | None, home: Path | None = None) -> Path:
    """Where a host reads its settings: the project root, or the user's home."""
    if scope == "project":
        return Path(project_root or Path.cwd())
    return Path(home).expanduser() if home else Path.home()


def _config_root(harness: str, scope: str, project_root: Path | None, home: Path | None = None) -> Path:
    """The settings directory for one harness in one scope."""
    root = _host_root(harness, scope, project_root, home) / DEFAULT_CONFIG_DIRS[harness]
    env_name = HOST_ROOT_ENV.get(harness)
    if scope == "user" and env_name and os.environ.get(env_name):
        return Path(os.environ[env_name]).expanduser()
    return root


def _hook_files(harness: str, scope: str, project_root: Path | None, home: Path | None = None) -> list[HookFile]:
    """The files a hook install writes for one harness in one scope."""
    if harness not in DEFAULT_CONFIG_DIRS:
        raise ValueError(f"Unsupported hook harness: {harness}")
    root = _config_root(harness, scope, project_root, home)
    if harness == "claude":
        return [HookFile(root / "settings.json")]
    if harness == "cursor":
        return [HookFile(root / "hooks.json")]
    if harness == "deepseek":
        return [
            HookFile(root / "hooks.json"),
            HookFile(root / "thread2skill-bridge.yaml", required=False),
        ]
    plugin = root / "plugins" / "thread2skill"
    if harness == "hermes":
        return [HookFile(plugin / "plugin.yaml"), HookFile(plugin / "__init__.py")]
    return [
        HookFile(plugin / "openclaw.plugin.json"),
        HookFile(plugin / "package.json"),
        HookFile(plugin / "index.js"),
    ]


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
    path = _hook_files("claude", scope, project_root)[0].path
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
    path = _hook_files("cursor", scope, project_root)[0].path
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
    spec = _hook_files("deepseek", scope, project_root)
    path = spec[0].path
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
    path = _hook_files("hermes", scope, project_root)[0].path.parent
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
    path = _hook_files("openclaw", scope, project_root)[0].path.parent
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


# --------------------------------------------------------------------------- doctor

DOCTOR_SAMPLE_PROMPT = (
    "Run the existing test suite before changing anything and follow the "
    "repository's conventions."
)
DOCTOR_TIMEOUT_SECONDS = 8.0
DOCTOR_STATUSES = ("ok", "missing", "invalid", "command-not-found", "no-match", "error")
# `missing` and `no-match` describe a quiet scope, not a broken hook: a hook
# that prints nothing when nothing matches works as designed, and a host with no
# hook file has nothing to verify. The statuses below are failures.
BROKEN_DOCTOR_STATUSES = {"invalid", "command-not-found", "error"}
SUPPORTED_FORMATS = {"text", "json", "claude", "cursor", "deepseek"}

RunCommand = Callable[[str, list[str], Path, float], "subprocess.CompletedProcess"]


@dataclass
class DoctorResult:
    """One host's report: static checks, plus the outcome of the sample call."""

    harness: str
    path: str
    status: str
    detail: str = ""
    event: str = ""
    files: list[dict[str, object]] = field(default_factory=list)
    command: str = ""
    sample_command: str = ""
    binary: str = ""
    cwd: str = ""
    prompt: str = ""
    output: str = ""

    def line(self) -> str:
        return f"{self.status:<17} {self.harness:<9} {self.path} {self.detail}".rstrip()


def _detail(event: str, message: str) -> str:
    return f"{event} · {message}"


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse the manifest shape these hooks use: comments, `key: value` pairs,
    and scalar list items. Anything else raises ValueError. This is a validity
    check for the files the installer writes, not a general YAML reader.
    """
    data: dict[str, Any] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if "\t" in raw:
            raise ValueError(f"line {number}: tab indentation")
        line = raw.rstrip()
        if line.lstrip().startswith("-"):
            item = line.lstrip()[1:].strip()
            if not item:
                raise ValueError(f"line {number}: empty list item")
            data.setdefault("items", []).append(item.strip("'\""))
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if not key:
            raise ValueError(f"line {number}: empty key")
        data[key] = value.strip().strip("'\"")
    if not data:
        raise ValueError("no mapping or list item found")
    return data


def _parse_hook_file(path: Path) -> tuple[Any, str]:
    """Parse one hook file statically. Returns (data, error)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"cannot read {path.name}: {exc}"
    kind = path.suffix.lower()
    if kind == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            return None, f"{path.name} is not valid JSON: {exc}"
        if not isinstance(data, dict):
            return None, f"{path.name} must contain a JSON object"
        return data, ""
    if kind == ".yaml":
        try:
            return _parse_simple_yaml(text), ""
        except ValueError as exc:
            return None, f"{path.name} is not valid YAML: {exc}"
    if kind == ".py":
        try:
            compile(text, str(path), "exec")
        except SyntaxError as exc:
            return None, f"{path.name} is not valid Python: {exc}"
        return text, ""
    # A host entry module is not parsed here; it is only read as text.
    return text, ""


def _walk_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def _command_from_source(text: str) -> str:
    """Rebuild the command a plugin module builds at runtime."""
    fmt = re.search(r'"--format",\s*"([a-z]+)"', text)
    threshold = re.search(r'"--threshold",\s*"([0-9.]+)"', text)
    if '"suggest"' not in text or not fmt or not threshold:
        return ""
    return f't2s suggest "<prompt>" --format {fmt.group(1)} --threshold {threshold.group(1)}'


def _check_command(command: str) -> tuple[str, str, str, str]:
    """Split a configured command into (binary, format, threshold, error)."""
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        return "", "", "", f"the configured command cannot be parsed: {exc}"
    if not parts:
        return "", "", "", "the configured command is empty"
    binary = parts[0]
    if "suggest" not in parts[1:]:
        return binary, "", "", "the configured command does not call `suggest`"
    fmt = threshold = ""
    for index, part in enumerate(parts):
        if part in {"--format", "--threshold"} and index + 1 < len(parts):
            if part == "--format":
                fmt = parts[index + 1]
            else:
                threshold = parts[index + 1]
    if not fmt:
        return binary, "", "", "the configured command has no --format"
    if fmt not in SUPPORTED_FORMATS:
        return binary, fmt, threshold, f"--format {fmt} is not one of {', '.join(sorted(SUPPORTED_FORMATS))}"
    if not threshold:
        return binary, fmt, "", "the configured command has no --threshold"
    try:
        value = float(threshold)
    except ValueError:
        return binary, fmt, threshold, f"--threshold {threshold} is not a number"
    if not 0.0 < value <= 1.0:
        return binary, fmt, threshold, f"--threshold {threshold} is outside 0 to 1"
    return binary, fmt, threshold, ""


def _resolve_binary(name: str) -> tuple[str, str]:
    """Resolve the command the hook names. `T2S_BIN` wins over `PATH`."""
    candidate = os.environ.get("T2S_BIN") or ""
    if not candidate:
        if "/" in name or "\\" in name:
            candidate = str(Path(name).expanduser())
        else:
            candidate = shutil.which(name) or ""
    if not candidate:
        return "", f"{name} is not on PATH; install the CLI or set T2S_BIN"
    path = Path(candidate).expanduser()
    if not path.is_file():
        return str(path), f"{path} is not a file"
    if not os.access(str(path), os.X_OK):
        return str(path), f"{path} is not executable"
    return str(path), ""


def _default_runner(binary: str, args: list[str], cwd: Path, timeout: float) -> "subprocess.CompletedProcess":
    return subprocess.run([binary, *args], capture_output=True, text=True, cwd=str(cwd), timeout=timeout)


def _run_sample(
    runner: RunCommand,
    binary: str,
    args: list[str],
    cwd: Path,
    timeout: float,
) -> tuple["subprocess.CompletedProcess | None", str, str]:
    """Run the single sample call. Returns (completed, error, status)."""
    try:
        completed = runner(binary, args, cwd, timeout)
    except subprocess.TimeoutExpired:
        return None, f"the sample call timed out after {timeout:g}s", "error"
    except OSError as exc:
        return None, f"cannot execute {binary}: {exc}", "command-not-found"
    if completed.returncode != 0:
        detail = truncate((completed.stderr or "").strip(), 160) or f"exit code {completed.returncode}"
        return completed, f"the sample call exited {completed.returncode}: {detail}", "error"
    return completed, "", ""


def _snippet(text: str) -> str:
    """First two non-empty lines, so a suggestion line stays visible."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()][:2]
    return truncate(" | ".join(lines), 120)


def _match_report(text: str) -> tuple[bool, str]:
    """True when the sample call produced a suggestion, not an empty payload."""
    text = (text or "").strip()
    if not text:
        return False, "printed nothing"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return True, _snippet(text)
    if isinstance(parsed, dict):
        suggestions = parsed.get("suggestions")
        if isinstance(suggestions, list) and not suggestions:
            return False, "no skill crossed the threshold"
        source = parsed.get("hookSpecificOutput") or parsed
        for key in ("additionalContext", "user_message"):
            if isinstance(source, dict) and isinstance(source.get(key), str) and not source[key].strip():
                return False, "returned an empty context"
    return True, _snippet(text)


def doctor_hooks(
    harness: str = "all",
    *,
    scope: str = "user",
    project_root: Path | None = None,
    home: Path | None = None,
    prompt: str = DOCTOR_SAMPLE_PROMPT,
    timeout: float = DOCTOR_TIMEOUT_SECONDS,
    run: RunCommand | None = None,
) -> list[DoctorResult]:
    """Report whether installed hooks are present, parse, and can run.

    The host is never executed. The only dynamic step is one `t2s suggest` call
    with the sample prompt, run from the working directory the host would use;
    its outcome is reported for every detected hook.
    """
    if scope not in {"user", "project"}:
        raise ValueError("scope must be user or project")
    runner = run or _default_runner
    names = ALL_HOOK_HARNESSES if harness == "all" else [harness]
    cwd = Path(project_root or Path.cwd()) if scope == "project" else Path.cwd()

    results: list[DoctorResult] = []
    detected: list[tuple[DoctorResult, str, str]] = []

    for name in names:
        event = HOOK_EVENTS[name]
        spec = _hook_files(name, scope, project_root, home)
        result = DoctorResult(
            harness=name,
            path=str(spec[0].path),
            status="",
            event=event,
            cwd=str(cwd),
            prompt=prompt,
        )
        results.append(result)

        present = [entry for entry in spec if entry.path.is_file()]
        if not present:
            result.status = "missing"
            result.detail = _detail(event, f"no hook file here; run `t2s hooks install --harness {name}`")
            continue

        for entry in spec:
            if not entry.path.is_file():
                result.files.append({"path": str(entry.path), "ok": False, "detail": "not written"})
                if entry.required:
                    result.status = "invalid"
                    result.detail = _detail(event, f"incomplete hook, {entry.path.name} is missing")
                    break
        if result.status:
            continue

        parsed: dict[Path, Any] = {}
        problems: list[str] = []
        for entry in spec:
            if not entry.path.is_file():
                # Optional files are reported above and are not parsed when absent.
                continue
            data, error = _parse_hook_file(entry.path)
            result.files.append({"path": str(entry.path), "ok": not error, "detail": error})
            if error:
                problems.append(error)
            else:
                parsed[entry.path] = data
        if problems:
            result.status = "invalid"
            result.detail = _detail(event, "; ".join(problems))
            continue

        command = ""
        for entry in spec:
            data = parsed[entry.path]
            if isinstance(data, str):
                command = _command_from_source(data)
            else:
                command = next((text for text in _walk_strings(data) if "t2s" in text and "suggest" in text), "")
            if command:
                break
        if not command:
            result.status = "missing"
            result.detail = _detail(
                event,
                f"no t2s entry in {spec[0].path.name}; run `t2s hooks install --harness {name}`",
            )
            continue

        binary_name, fmt, threshold, error = _check_command(command)
        if error:
            result.status = "invalid"
            result.detail = _detail(event, error)
            continue

        result.command = command
        binary, resolve_error = _resolve_binary(binary_name)
        result.binary = binary
        if resolve_error:
            result.status = "command-not-found"
            result.detail = _detail(event, resolve_error)
            continue

        result.sample_command = f"{binary} suggest <sample> --format {fmt} --threshold {threshold}"
        detected.append((result, fmt, threshold))

    if not detected:
        return results

    first, fmt, threshold = detected[0]
    completed, failure, failure_status = _run_sample(
        runner, first.binary, ["suggest", prompt, "--format", fmt, "--threshold", threshold], cwd, timeout
    )
    for entry in detected:
        result, _, _ = entry
        if failure:
            result.status = failure_status
            result.detail = _detail(result.event, failure)
        else:
            matched, report = _match_report(completed.stdout if completed else "")
            result.status = "ok" if matched else "no-match"
            result.detail = _detail(result.event, f"sample call from {result.cwd}: {report}")
        result.output = truncate((completed.stdout or "").strip(), 200) if completed else ""

    return results