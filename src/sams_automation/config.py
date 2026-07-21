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
    # When extracting a link, prefer the <a> whose visible text contains this
    # (e.g. "register"), so we grab the real activation button and not the logo.
    link_text_contains: str = ""
    # Pull the new membership number out of the activation email body.
    member_number_regex: str = ""


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
    # How to sign in the MAIN account:
    #   "password"   -> pick "Enter your password" and use primary_password.
    #   "email_code" -> pick "Email me a verification code"; the code lands in the
    #                   same catch-all inbox we already read, so no password is
    #                   needed (works for accounts that have no password set).
    login_method: str = "password"
    # Regex for the numeric login code email (used when login_method=email_code).
    login_code_regex: str = r"\b(\d{6})\b"


@dataclass
class ActivationConfig:
    """Phase 2 — the NEW secondary member activating their own membership.

    After the main account adds the member (phase 1), the member receives an
    email and finishes setup themselves (sets their own password). This controls
    that second half.
    """

    # Run activation as a SEPARATE browser session, so it behaves like the new
    # member logging in on their own device rather than reusing the primary's
    # signed-in session. Recommended.
    new_session: bool
    # Optional URL a secondary member visits to activate when the email carries a
    # numeric code rather than a click-through link. Leave empty to stay on
    # whatever page the code flow lands on.
    url: str


@dataclass
class ProxyConfig:
    enabled: bool
    file: str
    rotation: str  # "sticky" | "round_robin" | "random"


@dataclass
class ResetConfig:
    """Password-reset mode: request a code by email, then set a new password."""

    # Sam's "forgot password" page.
    forgot_url: str
    # Regex for the numeric reset code in the email.
    code_regex: str


@dataclass
class Config:
    imap: ImapConfig
    verification: VerificationConfig
    browser: BrowserConfig
    pacing: PacingConfig
    sams: SamsConfig
    proxies: ProxyConfig
    activation: ActivationConfig
    reset: ResetConfig


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

# Full recommended header for the account list, in order. Required columns first,
# then the optional ones. Used to generate the blank CSV template in the web UI
# so a family member can fill it in without guessing the column names.
TEMPLATE_COLUMNS = [
    *REQUIRED_ACCOUNT_COLUMNS,
    "phone",
    "secondary_password",
]


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
    activation = raw.get("activation") or {}
    reset = raw.get("reset") or {}

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
            link_text_contains=verification.get("link_text_contains", "") or "",
            member_number_regex=verification.get("member_number_regex", "") or "",
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
            login_method=sams.get("login_method", "password") or "password",
            login_code_regex=sams.get("login_code_regex", r"\b(\d{6})\b"),
        ),
        proxies=ProxyConfig(
            enabled=bool(proxies.get("enabled", False)),
            file=proxies.get("file", "proxies.txt"),
            rotation=proxies.get("rotation", "sticky"),
        ),
        activation=ActivationConfig(
            new_session=bool(activation.get("new_session", True)),
            url=activation.get("url", "") or "",
        ),
        reset=ResetConfig(
            forgot_url=reset.get("forgot_url", "https://www.samsclub.com/login"),
            code_regex=reset.get("code_regex", r"\b(\d{6})\b"),
        ),
    )

    if cfg.verification.mode not in ("code", "link"):
        raise ValueError(
            f"verification.mode must be 'code' or 'link', got '{cfg.verification.mode}'"
        )
    if cfg.sams.login_method not in ("password", "email_code"):
        raise ValueError(
            "sams.login_method must be 'password' or 'email_code', got "
            f"'{cfg.sams.login_method}'"
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


@dataclass
class ResetAccount:
    """One account to run a password reset on."""

    email: str
    new_password: str

    @property
    def label(self) -> str:
        return self.email


RESET_COLUMNS = ["email", "new_password"]


def load_reset_accounts(path: str | Path) -> list["ResetAccount"]:
    """Load the password-reset list: columns email, new_password."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Reset list not found: {path}. Copy resets.example.csv to {path} "
            "and fill in email,new_password for each family account."
        )
    rows: list[ResetAccount] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError(f"{path} has no header row.")
        missing = [c for c in RESET_COLUMNS if c not in reader.fieldnames]
        if missing:
            raise ValueError(
                f"{path} is missing required column(s): {', '.join(missing)}"
            )
        for i, row in enumerate(reader, start=2):
            row = {k: (v or "").strip() for k, v in row.items() if k is not None}
            for col in RESET_COLUMNS:
                if not row.get(col):
                    raise ValueError(
                        f"{path} row {i}: required column '{col}' is empty."
                    )
            rows.append(
                ResetAccount(email=row["email"], new_password=row["new_password"])
            )
    return rows
