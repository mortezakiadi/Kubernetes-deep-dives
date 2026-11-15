#!/usr/bin/env python3
"""Shrink draw.io SVG exports for the web.

draw.io embeds the whole source file (content="...") and a PNG fallback for every
text label. Browsers don't need either. Usage: python3 tools/clean_svg.py diagrams/*/*.svg
"""
import re, sys
for f in sys.argv[1:]:
    s = open(f, encoding="utf-8").read()
    s = re.sub(r'(<svg[^>]*?)\s+content="[^"]*"', r"\1", s, count=1, flags=re.S)
    s = re.sub(r"(</foreignObject>)\s*<image [^>]*?/>", r"\1", s)
    s = s.replace("color-scheme: light dark;", "color-scheme: light;")
    open(f, "w", encoding="utf-8").write(s)
    print("cleaned", f)
