"""Conservative HTML transcript adapter (standard library only).

A saved support-chat page is mostly chrome: navigation, headers, styles, and
scripts. When the page shows a reliable, repeated message shape this adapter
recovers one message per block. When it does not, the cleaned page text is kept
as a single best-effort document instead of a guessed transcript, because one
document beats a wrong split.

Every choice here is deliberately conservative:

- `script`, `style`, `nav`, and `header` nodes are dropped with their text.
- A block counts only when it holds one author node and one timestamp node in
  the same block element, or within two levels of it, close together.
- Blocks must repeat the same shape, and at least `MIN_BLOCKS` of them must be
  found, otherwise the page stays one document.
- Timestamps go through `parse_timestamp`. An unparseable timestamp keeps its
  raw text; a time is never invented.
"""

from __future__ import annotations

import re
import warnings
from html.parser import HTMLParser
from pathlib import Path

from ..models import Message, Thread
from ..util import parse_timestamp
from .common import first_line, group_messages, make_thread

SOURCE = "html"

# Page chrome. These nodes are removed together with their text content.
STRIPPED_TAGS = {"script", "style", "nav", "header"}

# A split is only trusted when this many blocks share the same shape.
MIN_BLOCKS = 3

# Author and timestamp nodes may sit in the same block element, or within two
# levels of it, and must be close together inside that block.
PAIR_DEPTH = 2
PAIR_WINDOW = 2

# A block must carry real text, so a bare name-and-time line is not a message.
MIN_BODY_CHARS = 8

# Author and timestamp lines are short; longer text is treated as body.
MAX_LABEL_CHARS = 60

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}

BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "dd", "div", "dl", "dt",
    "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "li", "main",
    "ol", "p", "section", "table", "td", "tr", "ul",
}

# Stand-in for a <br> so line breaks survive text normalization.
NEWLINE = "\x00"

_MONTHS = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"
_TIME = r"\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:\s*(?:AM|PM|am|pm))?"
_DATE = (
    r"(?:\d{4}-\d{2}-\d{2}"
    rf"|\d{{1,2}}[./-]\d{{1,2}}[./-]\d{{4}}"
    rf"|\d{{1,2}}\s+(?:{_MONTHS})\w*\s+\d{{4}}"
    rf"|(?:{_MONTHS})\w*\s+\d{{1,2}},?\s+\d{{4}})"
)
TIMESTAMP_RE = re.compile(
    rf"^(?:(?:Today|Yesterday)\s+(?:at\s+)?)?(?:(?:{_DATE})(?:[ T]+{_TIME})?"
    rf"(?:Z|[+-]\d{{2}}:?\d{{2}})?|{_TIME})$"
)
NAME_RE = re.compile(r"^@?[A-Za-z][A-Za-z'’\-]{1,30}(?:[ _][A-Za-z'’-]{1,30}){0,3}$")
AUTHOR_HINT_RE = re.compile(
    r"(?:author|user|sender|speaker|name|handle|nickname|customer|agent|support|who|from)", re.I
)
AUTHOR_LABEL_RE = re.compile(r"^(?:author|from|by|user|speaker|customer|agent)\s*[:\-]\s*(.+)$", re.I)
SPACE_RE = re.compile(r"\s+")
EXTRA_SPACE_RE = re.compile(r"\n{3,}")


class _Node:
    __slots__ = ("tag", "attrs", "children", "skip")

    def __init__(self, tag: str, attrs: dict[str, str], skip: bool) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[_Node | str] = []
        self.skip = skip

    def text(self) -> str:
        parts: list[str] = []
        for child in self.children:
            if isinstance(child, str):
                if child == NEWLINE:
                    continue
                text = _collapse(child)
                if text:
                    parts.append(text)
            elif not child.skip:
                text = child.text()
                if text:
                    parts.append(text)
        return " ".join(parts)

    def own_text(self) -> str:
        """Text written directly on this node, not text from its children."""
        parts = [_collapse(child) for child in self.children if isinstance(child, str) and child != NEWLINE]
        return " ".join(part for part in parts if part).strip()

    def renders(self) -> str:
        """Cleaned text for this node, keeping block elements as paragraphs."""
        parts: list[str] = []
        for child in self.children:
            if isinstance(child, str):
                if child == NEWLINE:
                    parts.append("\n")
                    continue
                text = _collapse(child)
                if text:
                    parts.append(text)
            elif not child.skip:
                text = child.renders()
                if text:
                    parts.append(text)
        separator = "\n\n" if self.tag == "#document" or self.tag in BLOCK_TAGS else " "
        return EXTRA_SPACE_RE.sub("\n\n", separator.join(part for part in parts if part)).strip()


class _TreeBuilder(HTMLParser):
    """Small element tree with chrome nodes marked as skipped."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#document", {}, False)
        self.open_nodes = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        parent = self.open_nodes[-1]
        if tag == "br":
            parent.children.append(NEWLINE)
            return
        if tag in VOID_TAGS:
            return
        node = _Node(
            tag,
            {str(key): str(value or "") for key, value in attrs},
            parent.skip or tag in STRIPPED_TAGS,
        )
        parent.children.append(node)
        self.open_nodes.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.open_nodes[-1].children.append(NEWLINE)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.open_nodes) - 1, 0, -1):
            if self.open_nodes[index].tag == tag:
                del self.open_nodes[index:]
                return

    def handle_data(self, data: str) -> None:
        node = self.open_nodes[-1]
        if node.skip:
            return
        node.children.append(data)


def _collapse(text: str) -> str:
    return SPACE_RE.sub(" ", text).strip()


def _looks_timestamp(text: str) -> bool:
    return bool(text) and len(text) <= MAX_LABEL_CHARS and bool(TIMESTAMP_RE.match(text))


def _looks_author(node: _Node, text: str) -> bool:
    if not text or len(text) > MAX_LABEL_CHARS or "\n" in text:
        return False
    hint = f"{node.attrs.get('class', '')} {node.attrs.get('id', '')}"
    if AUTHOR_HINT_RE.search(hint) and len(text.split()) <= 4:
        return True
    if AUTHOR_LABEL_RE.match(text):
        return True
    return bool(NAME_RE.match(text))


def _classify(node: _Node, text: str) -> str:
    is_time = _looks_timestamp(text)
    is_author = _looks_author(node, text)
    if is_time and is_author:
        return "both"
    if is_time:
        return "timestamp"
    if is_author:
        return "author"
    return "other"


def _units(block: _Node, depth: int = 0) -> list[dict]:
    """Text units for this element's own text and its shallow descendants.

    A unit is one piece of text: text written directly on `block`, or the whole
    text of a child element. A child that holds no text of its own is only a
    wrapper, so the scan looks inside it instead of counting it as a unit. That
    keeps one pair from being counted twice.
    """
    units: list[dict] = []
    for child in block.children:
        if isinstance(child, str):
            text = _collapse(child)
            if text:
                units.append({"node": block, "text": text, "own": True, "depth": depth})
            continue
        if child.skip:
            continue
        text = child.text()
        if not text:
            continue
        if depth < PAIR_DEPTH and not child.own_text():
            units.extend(_units(child, depth + 1))
        else:
            units.append({"node": child, "text": text, "own": False, "depth": depth + 1})
    return units


def _find_pair(block: _Node) -> tuple[list[dict], list[dict]] | None:
    """The single author unit and timestamp unit close together in this block."""
    units = _units(block)
    for unit in units:
        unit["kind"] = _classify(unit["node"], unit["text"])
    picked = [unit for unit in units if unit["kind"] != "other"]
    if len(picked) == 1:
        return ([picked[0]], units) if picked[0]["kind"] == "both" else None
    if len(picked) != 2:
        return None
    author, stamp = picked
    if author["kind"] == "timestamp":
        author, stamp = stamp, author
    if author["kind"] != "author" or stamp["kind"] != "timestamp":
        return None
    positions = {id(unit): index for index, unit in enumerate(units)}
    if abs(positions[id(author)] - positions[id(stamp)]) > PAIR_WINDOW:
        return None
    return ([author, stamp], units)


def _contains(node: _Node, other: _Node) -> bool:
    for child in node.children:
        if isinstance(child, str):
            continue
        if child is other or _contains(child, other):
            return True
    return False


def _detect_blocks(root: _Node) -> list[dict]:
    """Confident message blocks in document order, keeping the tightest one."""
    candidates: list[dict] = []

    def visit(node: _Node) -> None:
        for child in node.children:
            if isinstance(child, str) or child.skip:
                continue
            if child.tag in BLOCK_TAGS:
                found = _find_pair(child)
                if found is not None:
                    pair, units = found
                    body = _block_body(child, pair)
                    if len(body) >= MIN_BODY_CHARS:
                        candidates.append({"node": child, "units": units, "pair": pair, "body": body})
            visit(child)

    visit(root)
    return [
        block
        for block in candidates
        if not any(other is not block and _contains(block["node"], other["node"]) for other in candidates)
    ]


def _largest_group(blocks: list[dict]) -> list[dict]:
    """Blocks that share one repeated message shape, in document order.

    The shape is the author and timestamp arrangement; body text is ignored so
    that a message with an extra wrapper element still matches its neighbours.
    """
    groups: dict[tuple[str, ...], list[dict]] = {}
    for block in blocks:
        signature = tuple(unit["kind"] for unit in block["units"] if unit["kind"] != "other")
        groups.setdefault(signature, []).append(block)
    if not groups:
        return []
    # Groups are built in document order, so max() keeps the first shape found.
    return max(groups.values(), key=len)


def _block_body(block: _Node, excluded_units: list[dict]) -> str:
    """The text left in a block once the author and timestamp lines are removed."""
    excluded_nodes = {unit["node"] for unit in excluded_units if not unit["own"]}
    excluded_texts = {unit["text"] for unit in excluded_units}
    parts: list[str] = []
    for child in block.children:
        if isinstance(child, str):
            if child == NEWLINE:
                parts.append("\n")
                continue
            text = _collapse(child)
            if text and text not in excluded_texts:
                parts.append(text)
        elif child.skip or child in excluded_nodes or child.text() in excluded_texts:
            continue
        else:
            text = _block_body(child, excluded_units)
            if text:
                parts.append(text)
    separator = "\n\n" if block.tag in BLOCK_TAGS else " "
    return EXTRA_SPACE_RE.sub("\n\n", separator.join(part for part in parts if part)).strip()


def _split_name_and_time(text: str) -> tuple[str, str]:
    """A single line that is both a name and a time."""
    match = TIMESTAMP_RE.search(text)
    stamp = match.group(0) if match else ""
    author = text.replace(stamp, " ").strip(" :,-") if stamp else text
    return (author or text, stamp or text)


def _message_from_block(block: dict, index: int) -> Message:
    pair = block["pair"]
    author_unit = pair[0]
    stamp_unit = pair[-1]
    author_text = author_unit["text"]
    time_text = stamp_unit["text"]
    if author_unit["kind"] == "both":
        author_text, time_text = _split_name_and_time(author_text)
    else:
        label = AUTHOR_LABEL_RE.match(author_text)
        if label:
            author_text = label.group(1).strip()
    return Message(
        id=f"html-{index}",
        ts=parse_timestamp(time_text),
        author=author_text or "unknown",
        text=block["body"],
        meta={"html_block": index, "raw_timestamp": time_text},
    )


def load_html(path: Path, gap_minutes: int, channel_override: str = "", title: str = "") -> list[Thread]:
    path = Path(path)
    root = _parse(path)
    blocks = _detect_blocks(root)
    channel = channel_override or path.stem
    raw_path = str(path)
    extra = {"html_heuristic": "blocks", "html_block_count": len(blocks)}

    if len(blocks) < MIN_BLOCKS:
        text = root.renders()
        warnings.warn(
            "HTML input did not show a reliable repeated message structure; "
            "keeping the cleaned page text as one best-effort document.",
            RuntimeWarning,
        )
        return [
            make_thread(
                source=SOURCE,
                channel=channel,
                thread_key="document",
                messages=[Message(id="html-0", ts="", author="document", text=text)],
                title=title or first_line(text) or path.stem,
                raw_path=raw_path,
                extra=dict(extra, html_heuristic="single-document"),
            )
        ]

    chosen = _largest_group(blocks)
    messages = [_message_from_block(block, index) for index, block in enumerate(chosen)]
    groups = group_messages(messages, gap_minutes) or [[messages[0]]]
    threads: list[Thread] = []
    for index, group in enumerate(groups, start=1):
        key = "document" if len(groups) == 1 else f"window:{index}"
        threads.append(
            make_thread(
                source=SOURCE,
                channel=channel,
                thread_key=key,
                messages=group,
                title=title or first_line(group[0].text) or path.stem,
                raw_path=raw_path,
                extra=extra,
            )
        )
    return threads


def _parse(path: Path) -> _Node:
    builder = _TreeBuilder()
    builder.feed(path.read_text(encoding="utf-8-sig", errors="replace"))
    builder.close()
    return builder.root
