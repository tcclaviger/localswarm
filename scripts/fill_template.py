#!/usr/bin/env python3
"""Fill {{KEY}} placeholders in a template.

Usage: fill_template.py TEMPLATE OUT KEY=value KEY=@path/to/file ...
A value starting with @ is read from that file. Unfilled placeholders become empty and are
reported on stderr so a missing argument is noticed.
"""
import re, sys

tpl, out, pairs = sys.argv[1], sys.argv[2], sys.argv[3:]
text = open(tpl).read()
vals = {}
for p in pairs:
    k, _, v = p.partition("=")
    vals[k] = open(v[1:]).read() if v.startswith("@") else v
for k, v in vals.items():
    text = text.replace("{{" + k + "}}", v)
left = sorted(set(re.findall(r"\{\{([A-Z_]+)\}\}", text)))
if left:
    print(f"fill_template: unfilled -> empty: {', '.join(left)}", file=sys.stderr)
    text = re.sub(r"\{\{[A-Z_]+\}\}", "", text)
open(out, "w").write(text)
