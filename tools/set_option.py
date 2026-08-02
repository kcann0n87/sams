"""Set one config.yaml option without hand-editing YAML.

    .venv/bin/python tools/set_option.py purchasing.max_attempts 0
    .venv/bin/python tools/set_option.py purchasing.allow_unpriced true
    .venv/bin/python tools/set_option.py walmart.channel chromium
    .venv/bin/python tools/set_option.py purchasing            # show a section

Rewrites the single line, leaving comments and layout alone, then re-reads the
file to prove the value round-tripped. Only touches the named section, so a
key that exists in two blocks can't be changed in the wrong one.
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
if not args:
    sys.exit(f"usage: {sys.argv[0]} <section>.<key> <value>   (or just <section>)")

data = yaml.safe_load(cfg.read_text()) or {}

# A bare section name just prints it, so you can see what's there first.
if "." not in args[0]:
    section = data.get(args[0])
    if not isinstance(section, dict):
        sys.exit(f"no section called {args[0]!r}. top level: {', '.join(sorted(data))}")
    width = max(len(k) for k in section)
    for key, value in section.items():
        if not isinstance(value, (dict, list)):
            print(f"  {args[0]}.{key.ljust(width)} = {value!r}")
    sys.exit(0)

if len(args) != 2:
    sys.exit(f"usage: {sys.argv[0]} <section>.<key> <value>")

section_name, _, key = args[0].partition(".")
raw_value = args[1]
section = data.get(section_name)
if not isinstance(section, dict):
    sys.exit(f"no section called {section_name!r}. top level: {', '.join(sorted(data))}")
if key not in section:
    sys.exit(f"no {section_name}.{key}. keys: {', '.join(sorted(section))}")

# Match the type already in the file, so `0` doesn't become the string "0" and
# quietly mean something else to the code reading it.
existing = section[key]
if isinstance(existing, bool):
    if raw_value.lower() not in ("true", "false"):
        sys.exit(f"{section_name}.{key} is a true/false setting, got {raw_value!r}")
    value, literal = raw_value.lower() == "true", raw_value.lower()
elif isinstance(existing, int) and not isinstance(existing, bool):
    try:
        value = int(raw_value)
    except ValueError:
        sys.exit(f"{section_name}.{key} is a whole number, got {raw_value!r}")
    literal = str(value)
elif isinstance(existing, float):
    try:
        value = float(raw_value)
    except ValueError:
        sys.exit(f"{section_name}.{key} is a number, got {raw_value!r}")
    literal = str(value)
else:
    value, literal = raw_value, json.dumps(raw_value)

text = cfg.read_text()
start = text.find(f"\n{section_name}:")
if start < 0:
    sys.exit(f"config.yaml has no {section_name}: block")
body = text[start + 1:]
# Stop at the next top-level key, so a same-named key in a later section is
# never the one that gets rewritten.
end = re.search(r"^\S", body[len(section_name) + 2:], re.M)
cut = len(body) if not end else len(section_name) + 2 + end.start()
head, block, tail = text[: start + 1], body[:cut], body[cut:]

block, count = re.subn(
    rf"^(\s*){re.escape(key)}:[^\n]*$",
    lambda m: f"{m.group(1)}{key}: {literal}",
    block,
    count=1,
    flags=re.M,
)
if not count:
    sys.exit(f"couldn't find the {key}: line inside {section_name}:")
cfg.write_text(head + block + tail)

check = yaml.safe_load(cfg.read_text()) or {}
now = (check.get(section_name) or {}).get(key)
if now != value:
    sys.exit(f"round-trip failed: file now reads {now!r}, expected {value!r}")
print(f"{section_name}.{key} = {now!r}")
