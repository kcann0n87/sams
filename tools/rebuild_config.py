"""Rebuild config.yaml from the shipped example, keeping your real values.

For when hand-editing has damaged the file — an editor that hard-wraps can
silently split a long line, and repairing that by hand tends to delete more
than intended. The result may still parse while missing half the selectors,
which fails later and less obviously.

    .venv/bin/python tools/rebuild_config.py

Carries over the credentials and the handful of settings worth keeping, takes
everything else fresh from config.example.yaml, and saves the old file as
config.yaml.broken. Prints the selector list so you can see it's whole.
"""
import pathlib, shutil, sys, yaml

cfg = pathlib.Path("config.yaml")
example = pathlib.Path("config.example.yaml")

# What's worth carrying over. Everything else in the walmart block is
# boilerplate that the example already has, correctly.
old = yaml.safe_load(cfg.read_text()) or {}
w = old.get("walmart") or {}
oi = w.get("imap") or {}
keep = {
    "imap_user": oi.get("username") or "",
    "imap_pass": oi.get("password") or "",
    # chromium, matching the runner's own default. This said "chrome", so a
    # config written before walmart.channel existed came back out of here
    # pinned to Chrome — which is why Chrome kept launching after the runner
    # stopped inheriting browser.channel.
    "channel": w.get("channel") or "chromium",
    "profile": w.get("user_data_dir") or "walmart-profile",
    "icloud_pass": ((old.get("imap") or {}).get("password") or ""),
    "icloud_user": ((old.get("imap") or {}).get("username") or ""),
}

backup = cfg.with_suffix(".yaml.broken")
shutil.copy(cfg, backup)
text = example.read_text()

def put(text, key, value, once=True):
    """Replace the first `key: <anything>` with the given value."""
    import re
    pattern = re.compile(rf'^(\s*){re.escape(key)}:[^\n]*$', re.M)
    return pattern.sub(lambda m: f'{m.group(1)}{key}: "{value}"', text, count=1 if once else 0)

# The walmart imap block is the second `username:`/`password:` pair in the file.
head, sep, tail = text.partition("walmart:")
if keep["imap_user"]:
    tail = put(tail, "username", keep["imap_user"])
    tail = put(tail, "password", keep["imap_pass"])
tail = put(tail, "channel", keep["channel"])
tail = put(tail, "user_data_dir", keep["profile"])
tail = put(tail, "login_continue", "button[type='submit']")
if keep["icloud_user"]:
    head = put(head, "username", keep["icloud_user"])
    head = put(head, "password", keep["icloud_pass"])
text = head + sep + tail

cfg.write_text(text)
data = yaml.safe_load(cfg.read_text())
sel = sorted((data["walmart"].get("selectors") or {}))
print(f"config OK — old file saved as {backup.name}")
print(f"walmart.channel       = {data['walmart'].get('channel')}")
print(f"walmart.user_data_dir = {data['walmart'].get('user_data_dir')}")
print(f"walmart.imap.username = {(data['walmart'].get('imap') or {}).get('username')}")
print(f"selectors ({len(sel)}): {sel}")
missing = [k for k in ("phone_input","phone_submit","code_input","code_submit",
                       "login_email","login_continue","login_use_code") if k not in sel]
print("MISSING:", missing or "none")
