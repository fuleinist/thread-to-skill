"""Small, dependency-free helpers used across the project."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

# A deliberately conservative stop-word list. It keeps offline extraction
# readable without pretending to be a full NLP pipeline.
STOPWORDS = {
    "a", "about", "after", "again", "against", "all", "also", "am", "an",
    "and", "any", "are", "aren't", "as", "at", "be", "because", "been",
    "before", "being", "below", "between", "both", "but", "by", "can",
    "can't", "cannot", "could", "couldn't", "did", "didn't", "do", "does",
    "doesn't", "doing", "don't", "down", "during", "each", "few", "for",
    "from", "further", "get", "got", "had", "hadn't", "has", "hasn't",
    "have", "haven't", "having", "he", "her", "here", "hers", "herself",
    "him", "himself", "his", "how", "i", "if", "in", "into", "is", "isn't",
    "it", "it's", "its", "itself", "just", "let", "let's", "like", "me",
    "more", "most", "mustn't", "my", "myself", "no", "nor", "not", "now",
    "of", "off", "on", "once", "only", "or", "other", "ought", "our",
    "ours", "ourselves", "out", "over", "own", "same", "shan't", "she",
    "should", "shouldn't", "so", "some", "such", "than", "that", "that's",
    "the", "their", "theirs", "them", "themselves", "then", "there",
    "these", "they", "this", "those", "through", "to", "too", "under",
    "until", "up", "very", "was", "wasn't", "we", "were", "weren't",
    "what", "when", "where", "which", "while", "who", "whom", "why",
    "will", "with", "won't", "would", "wouldn't", "you", "your", "yours",
    "yourself", "yourselves", "ok", "okay", "yeah", "yep", "yes", "lol",
    "thanks", "thank", "please", "hey", "hi", "hello", "sure", "cool",
    "great", "good", "nice", "right", "well", "thing", "things", "stuff",
    "think", "thought", "know", "see", "going", "go", "make", "made",
    "really", "actually", "basically", "maybe", "probably", "kind",
    "sort", "bit", "lot", "much", "many", "one", "two", "three", "new",
    "use", "using", "used", "work", "works", "working", "need", "needs",
    "want", "wants", "look", "looks", "looked", "say", "said", "says",
    "take", "takes", "took", "come", "comes", "came", "give", "gives",
    "gave", "put", "puts", "try", "tries", "tried", "still", "even",
    "back", "around", "though", "although", "however", "therefore",
}

TOKEN_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9+#._/-]*")
SECRET_SAFE_RE = re.compile(r"[^a-z0-9]+")


def now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def now_iso() -> str:
    return now_utc().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today_iso() -> str:
    return now_utc().date().isoformat()


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def slugify(text: str, max_len: int = 48) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"['’]", "", text)
    text = SECRET_SAFE_RE.sub("-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    if not text:
        text = "untitled-skill"
    if len(text) > max_len:
        cut = text[:max_len].rsplit("-", 1)[0]
        text = cut or text[:max_len]
    return text.strip("-") or "untitled-skill"


def unique_slug(base: str, taken: Iterable[str], max_len: int = 48) -> str:
    taken_set = set(taken)
    candidate = slugify(base, max_len=max_len)
    if candidate not in taken_set:
        return candidate
    for index in range(2, 1000):
        suffix = f"-{index}"
        candidate = slugify(base, max_len=max_len - len(suffix)) + suffix
        if candidate not in taken_set:
            return candidate
    raise ValueError("could not allocate a unique slug")


def stable_id(*parts: Any, length: int = 12) -> str:
    raw = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return digest[:length]


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    ensure_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def truncate(text: str, limit: int, note: str = " …") -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(note))].rstrip() + note


def tokenize(text: str) -> list[str]:
    tokens = [match.group(0).lower() for match in TOKEN_RE.finditer(text or "")]
    return [token for token in tokens if token not in STOPWORDS and len(token) > 1]


def keyword_counts(text: str, limit: int = 12) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for token in tokenize(text):
        if token.isdigit():
            continue
        counts[token] = counts.get(token, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ranked[:limit]


def shingles(text: str, size: int = 3) -> set[str]:
    tokens = tokenize(text)
    if not tokens:
        return set()
    if len(tokens) < size:
        return {" ".join(tokens)}
    return {" ".join(tokens[i:i + size]) for i in range(len(tokens) - size + 1)}


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def cosine_counts(left: dict[str, int], right: dict[str, int]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(value * right.get(key, 0) for key, value in left.items())
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def extract_json_object(text: str) -> dict[str, Any]:
    """Best-effort extraction of the first JSON object in a model response."""
    if not text:
        raise ValueError("empty model response")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("model response did not contain a JSON object")
    candidate = cleaned[start:end + 1]
    for attempt in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
        try:
            parsed = json.loads(attempt)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    raise ValueError("could not parse the model response as JSON")


_ISO_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)


def parse_timestamp(value: Any) -> str:
    """Normalize common Slack, Discord, ISO, and unix timestamps to ISO-8601."""
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        try:
            return _dt.datetime.fromtimestamp(number, _dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return str(value)
    text = str(value).strip()
    if not text:
        return ""
    if re.fullmatch(r"\d{10}(\.\d+)?", text):
        return parse_timestamp(float(text))
    if re.fullmatch(r"\d{13}", text):
        return parse_timestamp(int(text))
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = _dt.datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        return parsed.astimezone(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except ValueError:
        pass
    for fmt in _ISO_FORMATS:
        try:
            parsed = _dt.datetime.strptime(text, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_dt.timezone.utc)
            return parsed.astimezone(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        except ValueError:
            continue
    return text


def human_number(value: int) -> str:
    return f"{value:,}"


def plural(value: int, singular: str, plural_form: str | None = None) -> str:
    if value == 1:
        return singular
    return plural_form or singular + "s"


def relative_time(iso_text: str) -> str:
    if not iso_text:
        return "unknown"
    try:
        then = _dt.datetime.fromisoformat(iso_text.replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return iso_text
    delta = now_utc() - then
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    if seconds < 86400 * 30:
        return f"{seconds // 86400}d ago"
    return then.date().isoformat()


def safe_console_text(text: str) -> str:
    """Avoid Windows console crashes for emoji and other wide characters."""
    if os.name != "nt":
        return text
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("ascii", "replace").decode("ascii")