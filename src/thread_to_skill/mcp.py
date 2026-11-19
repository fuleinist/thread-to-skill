"""A read-only MCP server over stdio for the local skill library.

The server speaks JSON-RPC 2.0 as newline-delimited messages, which is the MCP
stdio transport. It answers four read-only tools and never mutates the
workspace.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from .search import suggest_skills as suggest_skills_impl
from .store import Store

SERVER_NAME = "thread-to-skill"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

DEFAULT_LIMIT = 10
MAX_LIMIT = 100
DEFAULT_SUGGEST_LIMIT = 3
DEFAULT_THRESHOLD = 0.18

INSTRUCTIONS = (
    "Read-only access to a local thread-to-skill library. Use suggest_skills on a "
    "task prompt, then get_skill to read the full SKILL.md before you apply it."
)


# --------------------------------------------------------------------------- schemas


def _limit_schema(default: int) -> dict[str, Any]:
    return {
        "type": "integer",
        "minimum": 1,
        "maximum": MAX_LIMIT,
        "default": default,
        "description": f"Maximum number of rows to return. 1 to {MAX_LIMIT}.",
    }


TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_skills",
        "title": "Search skills",
        "description": (
            "Full-text search over the local skill library. Returns one row per "
            "matching skill with its slug, title, description, and match score."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Words to search for. An empty value returns top skills.",
                },
                "limit": _limit_schema(DEFAULT_LIMIT),
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "get_skill",
        "title": "Get a skill",
        "description": (
            "Return the exact SKILL.md text for one skill, read from disk, plus "
            "the parsed metadata from the local index."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The skill slug, as returned by search_skills or list_skills.",
                }
            },
            "required": ["slug"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "suggest_skills",
        "title": "Suggest skills for a prompt",
        "description": (
            "Rank the skills that apply to a task prompt. Returns the ranked rows "
            "with the reason each one matched. Read-only: it records no usage."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The task or request to match against the skill library.",
                },
                "limit": _limit_schema(DEFAULT_SUGGEST_LIMIT),
                "threshold": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "default": DEFAULT_THRESHOLD,
                    "description": "Lowest match score to report. 0 to 1.",
                },
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "list_skills",
        "title": "List skills",
        "description": (
            "List skill records, newest confidence first. Filter by category, tag, "
            "or status. Archived skills stay hidden unless status is archived."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Category key to filter on, such as testing or delivery.",
                },
                "tag": {"type": "string", "description": "One tag to filter on."},
                "status": {
                    "type": "string",
                    "enum": ["active", "needs-review", "archived"],
                    "description": "Status to filter on. Omit to hide archived skills.",
                },
                "limit": _limit_schema(MAX_LIMIT),
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True},
    },
]

TOOL_NAMES = tuple(tool["name"] for tool in TOOLS)


def tools_list_payload() -> dict[str, Any]:
    """The stable `tools/list` result. Kept separate so tests can pin it."""
    return {"tools": TOOLS}


# --------------------------------------------------------------------------- errors


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _clamp_limit(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_LIMIT, number))


def _text_result(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}
        ],
        "isError": is_error,
    }


# --------------------------------------------------------------------------- server


class McpServer:
    """A single-session MCP server bound to one read-only workspace store."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.initialized = False

    # -- tool implementations -------------------------------------------------

    def _tool_search_skills(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "")
        limit = _clamp_limit(arguments.get("limit"), DEFAULT_LIMIT)
        rows = self.store.search_skills(query, limit=limit)
        payload = [
            {
                "slug": skill.slug,
                "title": skill.title,
                "description": skill.description,
                "category": skill.category,
                "status": skill.status,
                "score": round(float(score), 3),
            }
            for skill, score in rows
        ]
        return _text_result(payload)

    def _tool_get_skill(self, arguments: dict[str, Any]) -> dict[str, Any]:
        slug = str(arguments.get("slug") or "").strip()
        if not slug:
            return _text_result({"error": "slug is required"}, is_error=True)
        skill = self.store.get_skill(slug)
        if skill is None:
            return _text_result({"error": f"Unknown skill: {slug}"}, is_error=True)
        skill_file = Path(skill.path) / "SKILL.md"
        if not skill_file.is_file():
            return _text_result(
                {"error": f"SKILL.md is missing for {slug}: {skill_file}"}, is_error=True
            )
        # Read the bytes and decode, so no newline translation happens.
        body = skill_file.read_bytes().decode("utf-8")
        return _text_result(
            {"slug": skill.slug, "metadata": skill.to_dict(), "body": body}
        )

    def _tool_suggest_skills(self, arguments: dict[str, Any]) -> dict[str, Any]:
        prompt = str(arguments.get("prompt") or "")
        if not prompt.strip():
            return _text_result({"error": "prompt is required"}, is_error=True)
        limit = _clamp_limit(arguments.get("limit"), DEFAULT_SUGGEST_LIMIT)
        try:
            threshold = float(arguments.get("threshold", DEFAULT_THRESHOLD))
        except (TypeError, ValueError):
            threshold = DEFAULT_THRESHOLD
        threshold = max(0.0, min(1.0, threshold))
        rows = suggest_skills_impl(
            self.store,
            prompt,
            limit=limit,
            threshold=threshold,
            record=False,
        )
        return _text_result(rows)

    def _tool_list_skills(self, arguments: dict[str, Any]) -> dict[str, Any]:
        limit = _clamp_limit(arguments.get("limit"), MAX_LIMIT)
        status = str(arguments.get("status") or "").strip()
        skills = self.store.list_skills(
            category=str(arguments.get("category") or "").strip(),
            tag=str(arguments.get("tag") or "").strip(),
            status=status,
            limit=limit,
        )
        return _text_result([skill.to_dict() for skill in skills])

    def _dispatch_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = {
            "search_skills": self._tool_search_skills,
            "get_skill": self._tool_get_skill,
            "suggest_skills": self._tool_suggest_skills,
            "list_skills": self._tool_list_skills,
        }.get(name)
        if handler is None:
            raise KeyError(name)
        return handler(arguments)

    # -- protocol -------------------------------------------------------------

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Handle one decoded JSON-RPC message. Returns a response, or None."""
        method = message.get("method")
        msg_id = message.get("id")
        is_notification = "id" not in message

        if not isinstance(method, str) or not method:
            if is_notification:
                return None
            return _error(msg_id, -32600, "Invalid Request")

        if is_notification:
            # notifications/initialized and notifications/cancelled carry no reply.
            return None

        if method == "initialize":
            return _result(msg_id, self._initialize_result(message.get("params") or {}))

        if method == "ping":
            return _result(msg_id, {})

        if method == "tools/list":
            return _result(msg_id, tools_list_payload())

        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            if not isinstance(name, str) or name not in TOOL_NAMES:
                return _error(msg_id, -32602, f"Unknown tool: {name}")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                return _error(msg_id, -32602, "Tool arguments must be an object")
            try:
                return _result(msg_id, self._dispatch_tool(name, arguments))
            except KeyError:
                return _error(msg_id, -32602, f"Unknown tool: {name}")

        return _error(msg_id, -32601, f"Method not found: {method}")

    def _initialize_result(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        if requested in SUPPORTED_PROTOCOL_VERSIONS:
            version = requested
        else:
            version = DEFAULT_PROTOCOL_VERSION
        self.initialized = True
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
            "instructions": INSTRUCTIONS,
        }

    def handle_line(self, raw: str) -> dict[str, Any] | None:
        """Handle one raw line. A malformed line is a parse error with a null id."""
        text = raw.strip()
        if not text:
            return None
        try:
            message = json.loads(text)
        except ValueError:
            return _error(None, -32700, "Parse error")
        if not isinstance(message, dict):
            return _error(None, -32600, "Invalid Request")
        return self.handle(message)


def serve(store: Store, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """Run the server until EOF. Returns 0 on a clean shutdown."""
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout
    server = McpServer(store)
    while True:
        line = source.readline()
        if not line:
            break
        response = server.handle_line(line)
        if response is None:
            continue
        sink.write(json.dumps(response, ensure_ascii=False) + "\n")
        sink.flush()
    return 0