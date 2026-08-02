"""Switch which browser the Walmart flow drives, without hand-editing YAML.

    .venv/bin/python tools/set_channel.py chromium
    .venv/bin/python tools/set_channel.py chrome

Edits the one line in place — comments and everything else in config.yaml are
left exactly as they are, which a load-and-dump round trip would not do.
"""
import pathlib
import re
import sys

import yaml

choice = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
if choice not in ("chromium", "chrome"):
    sys.exit(f"usage: {sys.argv[0]} chromium|chrome")

cfg = pathlib.Path("config.yaml")
if not cfg.exists():
    sys.exit("no config.yaml here — run this from the project directory")

text = cfg.read_text()
# Only the walmart block's channel: the one above it belongs to the Sam's Club
# flow, which needs real Chrome and must not be changed by this.
head, sep, tail = text.partition("\nwalmart:")
if not sep:
    sys.exit("config.yaml has no walmart: block — run tools/rebuild_config.py first")

pattern = re.compile(r'^(\s*)channel:[^\n]*$', re.M)
tail, count = pattern.subn(lambda m: f'{m.group(1)}channel: "{choice}"', tail, count=1)
if count:
    cfg.write_text(head + sep + tail)
else:
    # No channel key at all. Add one right under the walmart: header so it is
    # explicit rather than relying on the default.
    cfg.write_text(head + sep + f'\n  channel: "{choice}"' + tail)

data = yaml.safe_load(cfg.read_text()) or {}
now = (data.get("walmart") or {}).get("channel")
print(f"walmart.channel = {now}")
if now != choice:
    sys.exit("that didn't take — open config.yaml and set walmart.channel by hand")
print("Restart the run for it to take effect.")
