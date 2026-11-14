"""Token usage capture and the price table behind cost estimates.

A distillation run records what the provider reported. When a provider reports
nothing, the prompt size is estimated from its length and the record is labelled
estimated. A model with no entry in the price table is reported as unknown. It
is never reported as free, because a price we do not know is not a price of zero.
"""

from __future__ import annotations

from typing import Any

# Prices are USD per 1,000,000 tokens. These are reference rates, not quotes from
# a provider. Change PRICE_TABLE_LAST_UPDATED whenever the table is revised.
PRICE_TABLE_LAST_UPDATED = "2026-11-15"

PRICE_TABLE: dict[str, dict[str, float]] = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "claude-3-5-sonnet": {"input": 3.00, "output": 15.00},
    "claude-3-5-haiku": {"input": 0.80, "output": 4.00},
    "llama3.1": {"input": 0.06, "output": 0.24},
}

CHARS_PER_TOKEN = 4


def _as_int(value: Any) -> int | None:
    """Return an integer token count, or None when the value is not one."""
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _price_key(model: str) -> str:
    """Longest PRICE_TABLE prefix that matches a model name, else an empty string.

    A dated model id such as `gpt-4o-mini-2024-07-18` is priced as its base
    model. The longest matching prefix wins, so the more specific entry is used.
    """
    needle = str(model or "").strip().lower()
    best = ""
    for prefix in PRICE_TABLE:
        if needle.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    return best


def estimate_tokens(text: str) -> int:
    """Rough token count from length, about four characters per token, minimum 1."""
    return max(1, len(str(text)) // CHARS_PER_TOKEN)


def capture(provider: Any, model: str, prompt: str) -> dict[str, Any]:
    """Build the usage record for one distillation.

    The provider owns the real counts when it reports them through `last_usage`.
    When it does not, the prompt is estimated and the record is marked estimated.
    A completion count is never invented: an unreported one stays 0.
    """
    provider_name = str(getattr(provider, "name", "") or "")
    model_name = str(model or "")
    reported = getattr(provider, "last_usage", None)
    if isinstance(reported, dict):
        prompt_tokens = _as_int(reported.get("prompt_tokens"))
        completion_tokens = _as_int(reported.get("completion_tokens"))
        if prompt_tokens is not None or completion_tokens is not None:
            return {
                "prompt_tokens": prompt_tokens if prompt_tokens is not None else 0,
                "completion_tokens": completion_tokens if completion_tokens is not None else 0,
                "model": model_name,
                "provider": provider_name,
                "estimated": False,
            }
    return {
        "prompt_tokens": estimate_tokens(prompt),
        "completion_tokens": 0,
        "model": model_name,
        "provider": provider_name,
        "estimated": True,
    }


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> dict[str, Any]:
    """Estimate the USD cost of a usage record against the price table.

    `priced_as` names the table entry used. An unmatched model returns status
    `unknown` with `usd` set to None, never 0.0.
    """
    name = str(model or "")
    key = _price_key(name)
    if not key:
        return {"usd": None, "status": "unknown", "model": name, "priced_as": ""}
    rates = PRICE_TABLE[key]
    tokens_in = max(0, _as_int(prompt_tokens) or 0)
    tokens_out = max(0, _as_int(completion_tokens) or 0)
    usd = (tokens_in * rates["input"] + tokens_out * rates["output"]) / 1_000_000.0
    return {
        "usd": round(usd, 6),
        "status": "known",
        "model": name,
        "priced_as": key,
    }
