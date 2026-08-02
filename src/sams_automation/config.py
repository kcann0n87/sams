"""Load and validate configuration and the account/profile list."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ImapConfig:
    host: str
    port: int
    ssl: bool
    username: str
    password: str
    mailbox: str
    from_contains: list[str]
    timeout_seconds: int
    poll_interval_seconds: int


@dataclass
class VerificationConfig:
    mode: str  # "code" or "link"
    code_regex: str
    link_regex: str


@dataclass
class BrowserConfig:
    headless: bool
    slow_mo_ms: int
    timeout_ms: int
    persist_sessions: bool
    state_dir: str
    screenshot_dir: str
    channel: str | None  # e.g. "chrome" to drive real Google Chrome
    user_data_dir: str | None  # persistent profile dir shared across accounts
    stealth: bool  # apply anti-detection init scripts


@dataclass
class PacingConfig:
    between_accounts_min: float
    between_accounts_max: float


@dataclass
class SamsConfig:
    login_url: str
    add_member_url: str
    logout_url: str
    selectors: dict[str, str]
    captcha_marker: str
    captcha_wait_seconds: int


@dataclass
class ProxyConfig:
    enabled: bool
    file: str
    rotation: str  # "sticky" | "round_robin" | "random"


@dataclass
class Config:
    imap: ImapConfig
    verification: VerificationConfig
    browser: BrowserConfig
    pacing: PacingConfig
    sams: SamsConfig
    proxies: ProxyConfig


@dataclass
class Account:
    """One primary login plus the secondary member to register under it."""

    primary_email: str
    primary_password: str
    secondary_first: str
    secondary_last: str
    secondary_email: str
    phone: str = ""
    # Password for the NEW account created after the invite code (flow "B").
    secondary_password: str = ""
    # The complimentary-membership form has no address fields; these are kept
    # optional in case another flow needs them.
    address1: str = ""
    address2: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    # Free-form extra columns are preserved here in case the real form needs them.
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.primary_email} -> {self.secondary_email}"


@dataclass
class WalmartAccount:
    """One Walmart login we're adding a phone number to."""

    email: str
    password: str
    proxy: str = ""      # blank = use the shared rotation from proxies.txt
    notes: str = ""
    phone: str = ""      # filled in once a number has been added

    @property
    def label(self) -> str:
        return self.email


REQUIRED_WALMART_COLUMNS = ["email", "password"]


def load_walmart_accounts(path: str | Path) -> list[WalmartAccount]:
    """Read the Walmart account list, in whatever shape it's written.

    The format is `email:password`, one per line:

        you@gmail.com:hunter2
        you@gmail.com               (password optional — code sign-in)

    Everything after the FIRST colon is the password, so a password may itself
    contain colons or commas without being split. A comma-separated line is
    still read when it has no colon at all, so older files keep working.

    Deliberately forgiving otherwise: the realistic input is a list pasted from
    somewhere else, and rejecting it over a header row or a stray blank line is
    friction for no benefit. The only thing required is an address — signing in
    with the emailed one-time code, the preferred path, never uses a password.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Walmart account list not found: {path}. "
            "Copy walmart_accounts.example.csv to it and fill it in."
        )

    accounts: list[WalmartAccount] = []
    problems: list[str] = []
    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # A header line names the columns rather than holding data.
        low = line.lower().replace(" ", "")
        if low.startswith("email,password") or low.startswith("email:password"):
            continue

        # Colon is the separator, and only the FIRST one counts — an email
        # never contains one, and a password often contains commas or further
        # colons that must survive intact. Comma is a fallback for older files
        # written before this, and only when there's no colon to go on.
        if ":" in line:
            fields = [f.strip() for f in line.split(":", 1)]
        else:
            fields = [f.strip() for f in line.split(",")]
        email = fields[0] if fields else ""
        password = fields[1] if len(fields) > 1 else ""
        if not email or "@" not in email:
            problems.append(f"line {lineno}: no email address in {line[:40]!r}")
            continue
        # A password is optional: signing in with the emailed one-time code
        # doesn't need one, and that's the preferred path.
        accounts.append(
            WalmartAccount(
                email=email,
                password=password,
                proxy=fields[2].strip() if len(fields) > 2 else "",
                notes=fields[3].strip() if len(fields) > 3 else "",
            )
        )

    if not accounts:
        detail = ("\n  " + "\n  ".join(problems[:5])) if problems else ""
        raise ValueError(
            f"No usable accounts in {path}. Each line should be "
            f"'email:password', or just an email when signing in with the "
            f"emailed code.{detail}"
        )
    if problems:
        print(f"  Skipped {len(problems)} bad line(s) in {path}:")
        for problem in problems[:5]:
            print(f"    {problem}")
        if len(problems) > 5:
            print(f"    ... and {len(problems) - 5} more")
    return accounts


REQUIRED_ACCOUNT_COLUMNS = [
    "primary_email",
    "primary_password",
    "secondary_first",
    "secondary_last",
    "secondary_email",
]


# Keys entered through the web UI land here rather than in config.yaml: it's a
# machine-written file, so no comments or formatting to preserve, and one file
# to keep out of git. Git-ignored, same as every other secret.
SMS_KEYS_FILE = "sms_keys.json"


def load_sms_settings(
    path: str | Path, keys_path: str | Path = SMS_KEYS_FILE
) -> dict[str, Any]:
    """Read the `sms_providers:` section, merged with web-UI-saved keys.

    Deliberately not part of `load_config`: checking number availability needs
    no IMAP or browser setup, and 5sim's price feed needs no account at all, so
    `sms-check` stays useful before the rest of the config exists.

    Precedence is keys file > config.yaml > environment, i.e. most-explicit
    wins — a key typed into the UI overrides a stale exported one.
    """
    settings: dict[str, Any] = {}
    path = Path(path)
    if path.exists():
        raw: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
        settings = raw.get("sms_providers") or {}

    for name, entry in _load_sms_keys(keys_path).items():
        merged = dict(settings.get(name) or {})
        # Only non-empty values override; clearing a field in the UI writes ""
        # and should fall back to config/env rather than blanking them.
        merged.update({k: v for k, v in entry.items() if v not in (None, "")})
        settings[name] = merged
    return settings


def _load_sms_keys(keys_path: str | Path) -> dict[str, dict[str, Any]]:
    keys_path = Path(keys_path)
    if not keys_path.exists():
        return {}
    try:
        data = json.loads(keys_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def save_sms_key(
    provider: str,
    api_key: str | None = None,
    username: str | None = None,
    *,
    enabled: bool | None = None,
    protocol: str | None = None,
    base_url: str | None = None,
    keys_path: str | Path = SMS_KEYS_FILE,
) -> None:
    """Write one provider's credentials to the keys file, creating it if needed.

    Only the fields passed are touched, so saving a username doesn't wipe a key.

    `protocol` and `base_url` are what let the web UI add a provider the code
    has never heard of: an entry carrying both is built as a custom adapter, so
    a site discovered by probing survives a restart without editing YAML.
    """
    keys_path = Path(keys_path)
    data = _load_sms_keys(keys_path)
    entry = dict(data.get(provider) or {})
    if api_key is not None:
        entry["api_key"] = api_key.strip()
    if username is not None:
        entry["username"] = username.strip()
    if enabled is not None:
        entry["enabled"] = bool(enabled)
    if protocol is not None:
        entry["protocol"] = protocol.strip()
    if base_url is not None:
        entry["base_url"] = base_url.strip().rstrip("/")
    data[provider] = entry
    keys_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    # Secrets: keep it readable only by the owner, best-effort (no-op on Windows).
    try:
        keys_path.chmod(0o600)
    except OSError:
        pass


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. Copy config.example.yaml to {path} "
            "and fill it in."
        )
    raw: dict[str, Any] = yaml.safe_load(path.read_text()) or {}

    def section(name: str) -> dict[str, Any]:
        if name not in raw or raw[name] is None:
            raise ValueError(f"Config section '{name}' is missing in {path}")
        return raw[name]

    imap = section("imap")
    verification = section("verification")
    browser = section("browser")
    pacing = section("pacing")
    sams = section("sams")
    proxies = raw.get("proxies") or {}

    cfg = Config(
        imap=ImapConfig(
            host=imap["host"],
            port=int(imap.get("port", 993)),
            ssl=bool(imap.get("ssl", True)),
            username=imap["username"],
            password=imap["password"],
            mailbox=imap.get("mailbox", "INBOX"),
            from_contains=[s.lower() for s in imap.get("from_contains", [])],
            timeout_seconds=int(imap.get("timeout_seconds", 180)),
            poll_interval_seconds=int(imap.get("poll_interval_seconds", 5)),
        ),
        verification=VerificationConfig(
            mode=verification.get("mode", "code"),
            code_regex=verification.get("code_regex", r"\b(\d{6})\b"),
            link_regex=verification.get(
                "link_regex", r"(https?://[^\s\"'<>]*samsclub\.com[^\s\"'<>]*)"
            ),
        ),
        browser=BrowserConfig(
            headless=bool(browser.get("headless", False)),
            slow_mo_ms=int(browser.get("slow_mo_ms", 0)),
            timeout_ms=int(browser.get("timeout_ms", 30000)),
            persist_sessions=bool(browser.get("persist_sessions", True)),
            state_dir=browser.get("state_dir", "state"),
            screenshot_dir=browser.get("screenshot_dir", "screenshots"),
            channel=browser.get("channel") or None,
            user_data_dir=browser.get("user_data_dir") or None,
            stealth=bool(browser.get("stealth", True)),
        ),
        pacing=PacingConfig(
            between_accounts_min=float(pacing.get("between_accounts_min", 20)),
            between_accounts_max=float(pacing.get("between_accounts_max", 60)),
        ),
        sams=SamsConfig(
            login_url=sams["login_url"],
            add_member_url=sams["add_member_url"],
            logout_url=sams.get("logout_url", ""),
            selectors=sams.get("selectors", {}),
            captcha_marker=sams.get("captcha_marker", ""),
            captcha_wait_seconds=int(sams.get("captcha_wait_seconds", 300)),
        ),
        proxies=ProxyConfig(
            enabled=bool(proxies.get("enabled", False)),
            file=proxies.get("file", "proxies.txt"),
            rotation=proxies.get("rotation", "sticky"),
        ),
    )

    if cfg.verification.mode not in ("code", "link"):
        raise ValueError(
            f"verification.mode must be 'code' or 'link', got '{cfg.verification.mode}'"
        )
    if cfg.proxies.rotation not in ("sticky", "round_robin", "random"):
        raise ValueError(
            "proxies.rotation must be 'sticky', 'round_robin', or 'random', "
            f"got '{cfg.proxies.rotation}'"
        )
    return cfg


def load_accounts(path: str | Path) -> list[Account]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Accounts file not found: {path}. Copy accounts.example.csv to {path} "
            "and fill in your profile list."
        )

    accounts: list[Account] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError(f"{path} has no header row.")
        missing = [c for c in REQUIRED_ACCOUNT_COLUMNS if c not in reader.fieldnames]
        if missing:
            raise ValueError(
                f"{path} is missing required column(s): {', '.join(missing)}"
            )

        known = set(Account.__dataclass_fields__) - {"extra"}
        for i, row in enumerate(reader, start=2):  # row 1 is the header
            row = {k: (v or "").strip() for k, v in row.items() if k is not None}
            for col in REQUIRED_ACCOUNT_COLUMNS:
                if not row.get(col):
                    raise ValueError(
                        f"{path} row {i}: required column '{col}' is empty."
                    )
            extra = {k: v for k, v in row.items() if k not in known}
            accounts.append(
                Account(
                    primary_email=row["primary_email"],
                    primary_password=row["primary_password"],
                    secondary_first=row["secondary_first"],
                    secondary_last=row["secondary_last"],
                    secondary_email=row["secondary_email"],
                    phone=row.get("phone", ""),
                    secondary_password=row.get("secondary_password", ""),
                    address1=row.get("address1", ""),
                    address2=row.get("address2", ""),
                    city=row.get("city", ""),
                    state=row.get("state", ""),
                    zip=row.get("zip", ""),
                    extra=extra,
                )
            )
    return accounts
