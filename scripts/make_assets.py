#!/usr/bin/env python3
"""Regenerate README screenshots and the social preview.

This is a development-only helper. It needs Pillow:

    python -m pip install pillow
    python scripts/make_assets.py

The screenshots render the real output captured in examples/demo-output/.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "docs" / "assets"
DEMO = ROOT / "examples" / "demo-output"

W, H = 1500, 900
BG_TOP = (11, 16, 32)
BG_BOTTOM = (17, 24, 39)
CARD = (15, 23, 42)
BAR = (17, 24, 39)
WHITE = (248, 250, 252)
MUTED = (148, 163, 184)
DIM = (100, 116, 139)
GREEN = (74, 222, 128)
CYAN = (125, 211, 252)
PURPLE = (196, 181, 253)
AMBER = (252, 211, 77)
RED = (248, 113, 113)
TEAL = (94, 234, 212)

FONT_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
MONO = FONT_DIR / "consola.ttf"
MONO_BOLD = FONT_DIR / "consolab.ttf"
SANS = FONT_DIR / "segoeui.ttf"
SANS_BOLD = FONT_DIR / "segoeuib.ttf"
SANS_LIGHT = FONT_DIR / "segoeuil.ttf"


def font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        return ImageFont.truetype("arial.ttf", size)


def gradient(size: tuple[int, int], top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    image = Image.new("RGB", size)
    draw = ImageDraw.Draw(image)
    for y in range(size[1]):
        ratio = y / max(1, size[1] - 1)
        color = tuple(int(a + (b - a) * ratio) for a, b in zip(top, bottom))
        draw.line((0, y, size[0], y), fill=color)
    return image


def wrap(text: str, width: int) -> list[str]:
    if not text:
        return [""]
    return textwrap.wrap(text, width=width, replace_whitespace=False, drop_whitespace=False) or [""]


def classify(line: str) -> tuple[tuple[int, int, int], bool]:
    stripped = line.strip()
    if stripped.startswith("$") or stripped.startswith("PS>"):
        return CYAN, True
    if stripped.startswith(("initialized", "ingested", "created", "installed", "ok ")):
        return GREEN, True
    if stripped.lower().startswith("error") or "error:" in stripped.lower():
        return RED, True
    if stripped.startswith(("skipped", "warn", "redaction:")):
        return AMBER, True
    if stripped.startswith(("THREAD", "SLUG", "CATEGORY", "LABEL", "SKILLS")):
        return PURPLE, True
    if stripped.startswith("──"):
        return (51, 65, 85), False
    if stripped.startswith("thread-to-skill") or stripped.startswith("Read a skill"):
        return MUTED, False
    return (203, 213, 225), False


def terminal(lines: list[str], title: str, output: Path, *, height: int = H, font_size: int = 22) -> None:
    image = gradient((W, height), BG_TOP, BG_BOTTOM)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((20, 20, W - 20, height - 20), radius=26, fill=CARD, outline=(30, 41, 59), width=2)
    draw.rounded_rectangle((20, 20, W - 20, 86), radius=26, fill=BAR)
    draw.rectangle((20, 60, W - 20, 86), fill=BAR)
    draw.ellipse((48, 42, 66, 60), fill=(248, 113, 113))
    draw.ellipse((78, 42, 96, 60), fill=(251, 191, 36))
    draw.ellipse((108, 42, 126, 60), fill=(52, 211, 153))
    draw.text((150, 40), title, font=font(SANS, 18), fill=MUTED)

    regular = font(MONO, font_size)
    bold = font(MONO_BOLD, font_size)
    x = 48
    y = 112
    line_height = font_size + 11
    max_chars = int((W - 100) / (font_size * 0.55))
    for raw in lines:
        color, is_bold = classify(raw)
        chunks = wrap(raw, max_chars)
        for index, chunk in enumerate(chunks):
            prefix = "" if index == 0 else "  "
            draw.text((x, y), prefix + chunk, font=bold if is_bold else regular, fill=color)
            y += line_height
        if y > height - 34:
            break
    image.save(output, "PNG")


def read_lines(name: str) -> list[str]:
    path = DEMO / name
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return raw.decode(encoding).splitlines()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").splitlines()


def terminal_ingest() -> None:
    lines = ["$ t2s init --provider offline"] + read_lines("01-init.txt")
    lines += ["", '$ t2s ingest ./slack-export'] + read_lines("02-ingest.txt")
    terminal(lines, "1 · Ingest, redact, and index local conversations", ASSETS / "screenshot-ingest.png", height=980)


def terminal_list() -> None:
    lines = ["$ t2s list"] + read_lines("03-list.txt")
    lines += ["", '$ t2s validate --all'] + read_lines("05-validate.txt")
    terminal(lines, "2 · A searchable, validated skill library", ASSETS / "screenshot-list.png", height=860)


def terminal_suggest() -> None:
    lines = [
        '$ t2s suggest "integration tests are flaky with postgres"',
    ] + read_lines("04-suggest.txt")
    lines += [
        "",
        "$ t2s hooks install --harness claude,cursor,deepseek --scope project",
    ] + read_lines("07-hooks.txt")
    terminal(lines, "3 · Proactive suggestions, before the agent asks", ASSETS / "screenshot-suggest.png", height=900)


def mark(draw: ImageDraw.ImageDraw, x: int, y: int, size: int) -> None:
    draw.rounded_rectangle((x, y, x + size, y + size), radius=int(size * 0.24), fill=(99, 102, 241))
    draw.rounded_rectangle(
        (x + int(size * 0.07), y + int(size * 0.07), x + size - int(size * 0.07), y + size - int(size * 0.07)),
        radius=int(size * 0.21),
        outline=(165, 180, 252),
        width=max(2, size // 70),
    )
    bx, by = x + int(size * 0.23), y + int(size * 0.22)
    bw, bh = int(size * 0.54), int(size * 0.37)
    draw.rounded_rectangle((bx, by, bx + bw, by + bh), radius=int(size * 0.11), fill=WHITE)
    draw.polygon([(bx + int(size * 0.09), by + bh - 2), (bx + int(size * 0.09), by + bh + int(size * 0.13)), (bx + int(size * 0.24), by + bh - 2)], fill=WHITE)
    for i in range(3):
        cx = bx + int(size * 0.14) + i * int(size * 0.13)
        draw.ellipse((cx, by + int(size * 0.14), cx + int(size * 0.045), by + int(size * 0.185)), fill=(79, 70, 229))
    bar_y = by + bh + int(size * 0.06)
    for i, width in enumerate((0.54, 0.43, 0.29)):
        draw.rounded_rectangle(
            (bx, bar_y + i * int(size * 0.055), bx + int(size * width), bar_y + i * int(size * 0.055) + int(size * 0.035)),
            radius=int(size * 0.017),
            fill=WHITE,
        )


def social() -> None:
    image = gradient((1280, 640), (11, 16, 32), (30, 27, 75))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, 1279, 639), radius=0, outline=None)
    mark(draw, 74, 170, 250)

    draw.text((380, 188), "Thread to Skills", font=font(SANS_BOLD, 76), fill=WHITE)
    draw.text((384, 288), "Turn conversations into compounding agent skills.", font=font(SANS, 30), fill=(165, 180, 252))
    draw.text((384, 336), "Local-first · zero dependencies · open Agent Skills", font=font(SANS, 22), fill=MUTED)

    chips = ["Slack", "Discord", "WhatsApp", "Claude Code", "Cursor", "OpenClaw"]
    x = 384
    for chip in chips:
        width = draw.textlength(chip, font=font(SANS, 18)) + 30
        draw.rounded_rectangle((x, 400, x + width, 438), radius=19, fill=(30, 41, 59), outline=(51, 65, 85))
        draw.text((x + 15, 409), chip, font=font(SANS, 18), fill=(226, 232, 240))
        x += int(width) + 12

    draw.line((74, 520, 1206, 520), fill=(51, 65, 85), width=2)
    draw.text((74, 548), "github.com/fuleinist/thread-to-skill", font=font(SANS, 22), fill=MUTED)
    draw.text((1000, 548), "MIT · v0.1.0", font=font(SANS, 22), fill=(100, 116, 139))
    image.save(ASSETS / "social-preview.png", "PNG")


def skill_card() -> None:
    text = (DEMO / "skill-example.md").read_text(encoding="utf-8")
    lines = text.splitlines()[:78]
    image = gradient((W, 1000), BG_TOP, BG_BOTTOM)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((20, 20, W - 20, 980), radius=26, fill=CARD, outline=(30, 41, 59), width=2)
    draw.rounded_rectangle((20, 20, W - 20, 86), radius=26, fill=BAR)
    draw.rectangle((20, 60, W - 20, 86), fill=BAR)
    draw.ellipse((48, 42, 66, 60), fill=(248, 113, 113))
    draw.ellipse((78, 42, 96, 60), fill=(251, 191, 36))
    draw.ellipse((108, 42, 126, 60), fill=(52, 211, 153))
    draw.text((150, 40), "4 · The generated SKILL.md", font=font(SANS, 18), fill=MUTED)

    x = 48
    y = 112
    max_chars = 104
    for raw in lines:
        stripped = raw.strip()
        if stripped == "---":
            draw.line((x, y + 16, W - 48, y + 16), fill=(51, 65, 85), width=2)
            y += 38
            continue
        if stripped.startswith("# "):
            draw.text((x, y + 8), stripped[2:], font=font(SANS_BOLD, 34), fill=WHITE)
            y += 62
            continue
        if stripped.startswith("## "):
            draw.text((x, y + 12), stripped[3:].upper(), font=font(SANS_BOLD, 20), fill=TEAL)
            y += 48
            continue
        if stripped.startswith("> "):
            for chunk in wrap(stripped[2:], max_chars):
                draw.text((x + 16, y), chunk, font=font(SANS, 22), fill=(165, 180, 252))
                y += 34
            y += 8
            continue
        if stripped.startswith("- [ ]"):
            draw.text((x, y), "☐", font=font(SANS, 24), fill=AMBER)
            for chunk in wrap(stripped[5:].strip(), max_chars - 4):
                draw.text((x + 34, y), chunk, font=font(SANS, 22), fill=(203, 213, 225))
                y += 32
            continue
        if stripped.startswith("- "):
            for chunk in wrap(stripped[2:], max_chars - 2):
                draw.text((x + 10, y), "• " + chunk, font=font(SANS, 22), fill=(203, 213, 225))
                y += 32
            continue
        if stripped.startswith(("name:", "description:", "license:", "compatibility:")):
            key, _, value = stripped.partition(":")
            draw.text((x, y), key + ":", font=font(MONO_BOLD, 20), fill=PURPLE)
            for chunk in wrap(value.strip(), max_chars - len(key) - 2):
                draw.text((x + 190, y), chunk, font=font(MONO, 20), fill=(226, 232, 240))
                y += 30
            continue
        if stripped.startswith(("category:", "tags:", "confidence:", "version:", "source:")):
            draw.text((x + 20, y), stripped, font=font(MONO, 19), fill=(148, 163, 184))
            y += 28
            continue
        if not stripped:
            y += 12
            continue
        if re_match_numbered(raw):
            for chunk in wrap(stripped, max_chars):
                draw.text((x, y), chunk, font=font(SANS, 22), fill=(203, 213, 225))
                y += 32
            y += 6
            continue
        for chunk in wrap(stripped, max_chars):
            draw.text((x, y), chunk, font=font(SANS, 21), fill=(203, 213, 225))
            y += 30
        if y > 940:
            break
    image.save(ASSETS / "screenshot-skill.png", "PNG")


def re_match_numbered(raw: str) -> bool:
    import re

    return bool(re.match(r"^\s*\d+\.\s", raw))


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    terminal_ingest()
    terminal_list()
    terminal_suggest()
    skill_card()
    social()
    print("assets written to", ASSETS)


if __name__ == "__main__":
    main()