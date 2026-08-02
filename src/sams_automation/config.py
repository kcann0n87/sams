"""Load and validate configuration and the account/profile list."""

from __future__ import annotations

import csv
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


REQUIRED_ACCOUNT_COLUMNS = [
    "primary_email",
    "primary_password",
    "secondary_first",
    "secondary_last",
    "secondary_email",
]


def load_sms_settings(path: str | Path) -> dict[str, Any]:
    """Read just the `sms_providers:` section.

    Deliberately not part of `load_config`: checking number availability needs
    no IMAP or browser setup, and 5sim's price feed needs no account at all, so
    `sms-check` stays useful before the rest of the config exists.
    """
    path = Path(path)
    if not path.exists():
        return {}
    raw: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
    return raw.get("sms_providers") or {}


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
