"""Set one walmart selector in config.yaml without hand-editing YAML.

    .venv/bin/python tools/set_selector.py login_email "input#email"
    .venv/bin/python tools/set_selector.py login_use_code ""
    .venv/bin/python tools/set_selector.py --list

Selectors are the part of the config that changes most, and they are exactly
the part an editor's hard-wrap ruins — a long CSS selector split across two
lines still parses and quietly means something else. This rewrites the single
line and then re-reads the file to prove it round-tripped.
"""
import json
import pathlib
import re
import sys

import yaml

cfg = pathlib.Path("config.yaml")
if not cfg.exists():
    sys.exit("no config.yaml here — run this from the project directory")

args = sys.argv[1:]
data = yaml.safe_load(cfg.read_text()) or {}
current = ((data.get("walmart") or {}).get("selectors")) or {}

if not args or args[0] in ("--list", "-l"):
    width = max((len(k) for k in current), default=0)
    for key in sorted(current):
        value = current[key]
        print(f"  {key.ljust(width)} = {value!r}" + ("" if value else "   <- not set"))
    sys.exit(0)

if len(args) != 2:
    sys.exit(f"usage: {sys.argv[0]} <name> <selector>   (or --list)")

name, value = args[0], args[1]
if name not in current:
    known = ", ".join(sorted(current))
    sys.exit(f"no walmart selector called {name!r}.\nknown: {known}")

text = cfg.read_text()
# Scope to the walmart block: several selector names also exist in the Sam's
# Club block above, and this must never touch those.
head, sep, tail = text.partition("\nwalmart:")
if not sep:
    sys.exit("config.yaml has no walmart: block — run tools/rebuild_config.py first")

# json.dumps produces a double-quoted scalar that YAML reads back identically,
# which matters for selectors containing quotes of their own.
pattern = re.compile(rf'^(\s*){re.escape(name)}:[^\n]*$', re.M)
tail, count = pattern.subn(lambda m: f"{m.group(1)}{name}: {json.dumps(value)}", tail, count=1)
if not count:
    sys.exit(f"couldn't find the {name}: line inside the walmart block")
cfg.write_text(head + sep + tail)

check = yaml.safe_load(cfg.read_text()) or {}
now = ((check.get("walmart") or {}).get("selectors") or {}).get(name)
if now != value:
    sys.exit(f"round-trip failed: file now reads {now!r}, expected {value!r}")
print(f"walmart.selectors.{name} = {now!r}")
