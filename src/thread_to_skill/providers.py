"""Pluggable LLM providers with a zero-dependency offline fallback."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class ProviderError(RuntimeError):
    pass


class Provider:
    name = "base"
    mode = "llm"

    # Token counts the last request reported. None when the provider does not
    # report usage, which is what the usage module treats as an estimate.
    last_usage: dict | None = None

    def complete(self, system: str, prompt: str, *, max_tokens: int = 4096, timeout: int = 120) -> str:
        raise NotImplementedError


class OfflineProvider(Provider):
    name = "offline"
    mode = "offline"

    def complete(self, system: str, prompt: str, *, max_tokens: int = 4096, timeout: int = 120) -> str:
        raise ProviderError("The offline provider is handled by the deterministic extractor.")


class ManualProvider(Provider):
    name = "manual"
    mode = "manual"

    def complete(self, system: str, prompt: str, *, max_tokens: int = 4096, timeout: int = 120) -> str:
        raise ProviderError("The manual provider writes a prompt for an external agent.")


def _token_count(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _usage_block(parsed: dict[str, Any], *, prompt_key: str, completion_key: str) -> dict[str, int] | None:
    """Read a provider usage block into the shared prompt/completion shape.

    Returns None when the provider reported nothing. A provider that reports no
    usage keeps `last_usage` unset so the usage module can label its run as an
    estimate instead of inventing counts.
    """
    usage = parsed.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt_tokens = _token_count(usage.get(prompt_key))
    completion_tokens = _token_count(usage.get(completion_key))
    if prompt_tokens is None and completion_tokens is None:
        return None
    return {"prompt_tokens": prompt_tokens or 0, "completion_tokens": completion_tokens or 0}


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={**headers, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:1200]
        raise ProviderError(f"Provider returned HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise ProviderError(f"Could not reach provider: {exc.reason}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderError("Provider returned non-JSON output") from exc
    if not isinstance(parsed, dict):
        raise ProviderError("Provider returned an unexpected payload")
    return parsed


class OpenAICompatProvider(Provider):
    name = "openai"

    def __init__(self, *, model: str, api_key: str, base_url: str = "https://api.openai.com/v1", json_mode: bool = True):
        if not api_key:
            raise ProviderError("Missing API key. Set OPENAI_API_KEY or pass --api-key-env.")
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.json_mode = json_mode

    def complete(self, system: str, prompt: str, *, max_tokens: int = 4096, timeout: int = 120) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            parsed = _post_json(
                self.base_url + "/chat/completions",
                payload,
                {"Authorization": f"Bearer {self.api_key}"},
                timeout,
            )
        except ProviderError:
            if not self.json_mode:
                raise
            # Some OpenAI-compatible endpoints reject response_format.
            payload.pop("response_format", None)
            parsed = _post_json(
                self.base_url + "/chat/completions",
                payload,
                {"Authorization": f"Bearer {self.api_key}"},
                timeout,
            )
        # The last response wins: a retry that succeeds overwrites the usage of
        # the request that rejected response_format.
        record = _usage_block(parsed, prompt_key="prompt_tokens", completion_key="completion_tokens")
        if record is not None:
            self.last_usage = record
        try:
            return str(parsed["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("OpenAI-compatible response had no message content") from exc


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, *, model: str, api_key: str, base_url: str = "https://api.anthropic.com/v1"):
        if not api_key:
            raise ProviderError("Missing API key. Set ANTHROPIC_API_KEY or pass --api-key-env.")
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def complete(self, system: str, prompt: str, *, max_tokens: int = 4096, timeout: int = 120) -> str:
        parsed = _post_json(
            self.base_url + "/messages",
            {
                "model": self.model,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.2,
            },
            {
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            timeout,
        )
        record = _usage_block(parsed, prompt_key="input_tokens", completion_key="output_tokens")
        if record is not None:
            self.last_usage = record
        blocks = parsed.get("content") or []
        text = "".join(str(block.get("text", "")) for block in blocks if isinstance(block, dict) and block.get("type") == "text")
        if not text:
            raise ProviderError("Anthropic response had no text content")
        return text


class OllamaProvider(Provider):
    name = "ollama"

    def __init__(self, *, model: str, base_url: str = "http://localhost:11434"):
        self.model = model
        self.base_url = base_url.rstrip("/")

    def complete(self, system: str, prompt: str, *, max_tokens: int = 4096, timeout: int = 180) -> str:
        parsed = _post_json(
            self.base_url + "/api/chat",
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.2, "num_predict": max_tokens},
            },
            {},
            timeout,
        )
        record = _usage_block(parsed, prompt_key="prompt_eval_count", completion_key="eval_count")
        if record is not None:
            self.last_usage = record
        message = parsed.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if not content:
            raise ProviderError("Ollama response had no message content")
        return str(content)


class CommandProvider(Provider):
    name = "command"

    def __init__(self, command: str, timeout: int = 180):
        if not command.strip():
            raise ProviderError("--command is required for the command provider")
        self.command = command
        self.timeout = timeout

    def complete(self, system: str, prompt: str, *, max_tokens: int = 4096, timeout: int = 120) -> str:
        combined = system.strip() + "\n\n" + prompt.strip()
        try:
            args = shlex.split(self.command, posix=(os.name != "nt"))
        except ValueError as exc:
            raise ProviderError(f"Could not parse --command: {exc}") from exc
        try:
            completed = subprocess.run(
                args,
                input=combined,
                text=True,
                capture_output=True,
                timeout=timeout or self.timeout,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as exc:
            raise ProviderError(f"Command not found: {args[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"Command timed out after {timeout}s") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[:1200]
            raise ProviderError(f"Command exited {completed.returncode}: {detail}")
        if not completed.stdout.strip():
            raise ProviderError("Command returned no output")
        return completed.stdout


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def default_model(provider: str) -> str:
    return {
        "openai": _env("T2S_MODEL") or "gpt-4o-mini",
        "anthropic": _env("T2S_MODEL") or "claude-3-5-sonnet-latest",
        "ollama": _env("T2S_MODEL") or "llama3.1",
    }.get(provider, "")


def get_provider(
    name: str,
    *,
    model: str = "",
    base_url: str = "",
    api_key_env: str = "",
    command: str = "",
    timeout: int = 120,
    json_mode: bool = True,
) -> Provider:
    name = (name or "auto").strip().lower()
    if name == "auto":
        configured = _env("T2S_PROVIDER")
        if configured:
            name = configured.lower()
        elif _env("OPENAI_API_KEY"):
            name = "openai"
        elif _env("ANTHROPIC_API_KEY"):
            name = "anthropic"
        else:
            name = "offline"
    if name == "offline":
        return OfflineProvider()
    if name == "manual":
        return ManualProvider()
    if name in {"openai", "openai-compatible", "compatible"}:
        key = _env(api_key_env or "OPENAI_API_KEY")
        return OpenAICompatProvider(
            model=model or default_model("openai"),
            api_key=key,
            base_url=base_url or _env("OPENAI_BASE_URL") or "https://api.openai.com/v1",
            json_mode=json_mode,
        )
    if name == "anthropic":
        key = _env(api_key_env or "ANTHROPIC_API_KEY")
        return AnthropicProvider(
            model=model or default_model("anthropic"),
            api_key=key,
            base_url=base_url or _env("ANTHROPIC_BASE_URL") or "https://api.anthropic.com/v1",
        )
    if name == "ollama":
        return OllamaProvider(
            model=model or default_model("ollama"),
            base_url=base_url or _env("OLLAMA_HOST") or "http://localhost:11434",
        )
    if name == "command":
        return CommandProvider(command, timeout=timeout)
    raise ProviderError(f"Unknown provider: {name}")