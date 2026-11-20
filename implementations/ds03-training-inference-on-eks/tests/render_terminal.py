"""Render a recorded transcript as a terminal-window screenshot (PNG).

The picture is drawn from the recorded text of commands that were really run, after redaction.
It is a faithful rendering of terminal output, not a capture of a screen, and the document says so.
"""
import pathlib
import re
import textwrap

from PIL import Image, ImageDraw, ImageFont

FONT = "/System/Library/Fonts/Menlo.ttc"
BG, FG, DIM = (30, 33, 40), (220, 223, 228), (130, 137, 150)
GREEN, RED, BLUE, YELLOW = (152, 195, 121), (224, 108, 117), (97, 175, 239), (229, 192, 123)
BAR = (52, 56, 66)
COLS = 118
PAD = 18
SIZE = 15


def _style(line: str):
    if line.startswith("$ "):
        return GREEN
    if line.startswith("[PASS]"):
        return GREEN
    if line.startswith("[FAIL]"):
        return RED
    if line.startswith("#"):
        return DIM
    if re.search(r"\b(blocked|denied|forbidden|Forbidden|refused|Timeout|timed out)\b", line):
        return YELLOW
    return FG


def render(title: str, blocks: list, out_path: pathlib.Path, max_lines: int = 60):
    """blocks: list of (kind, text) where kind is 'cmd', 'out' or 'check'."""
    lines = []
    for kind, text in blocks:
        if kind == "cmd":
            wrapped = textwrap.wrap("$ " + text, COLS, subsequent_indent="    ", break_long_words=True) or ["$"]
            lines += [(w, GREEN) for w in wrapped]
        elif kind == "check":
            ok = text.startswith("[PASS]")
            lines += [(w, GREEN if ok else RED) for w in textwrap.wrap(text, COLS, subsequent_indent="       ")]
            continue  # checks are printed as one block, with no blank line between them
        else:
            for raw in text.splitlines() or [""]:
                wrapped = textwrap.wrap(raw, COLS, subsequent_indent="  ", break_long_words=True, drop_whitespace=False) or [""]
                lines += [(w, _style(w)) for w in wrapped]
        lines.append(("", FG))
    if len(lines) > max_lines:
        lines = lines[: max_lines - 1] + [(f"... ({len(lines) - max_lines + 1} more lines in the full transcript)", DIM)]
    font = ImageFont.truetype(FONT, SIZE)
    cw = font.getlength("M")
    lh = SIZE + 6
    width = int(cw * COLS + PAD * 2)
    height = int(lh * len(lines) + PAD * 2 + 34)
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width, 34], fill=BAR)
    for i, c in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
        d.ellipse([14 + i * 22, 11, 26 + i * 22, 23], fill=c)
    tf = ImageFont.truetype(FONT, 13)
    d.text((width // 2 - tf.getlength(title) / 2, 9), title, fill=(190, 195, 205), font=tf)
    y = 34 + PAD
    for text, color in lines:
        d.text((PAD, y), text, fill=color, font=font)
        y += lh
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, optimize=True)
    return out_path
