"""Add a phone number to a Walmart account.

Per account: log in through that account's proxy, navigate to the phone
settings, buy a verification number, enter it, read the code the provider
receives, and submit it.

The ordering matters. The number is bought only once the browser is sitting on
the phone form, because a bought number starts expiring immediately — buying
before a slow login or a CAPTCHA wastes it, and every wasted number is either
money or a cancellation.

Every selector lives in `config.yaml` under `walmart.selectors`, so correcting
the page map never needs a code change. The values shipped are placeholders
until a first run against the real page.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import WalmartAccount


@dataclass
class WalmartConfig:
    login_url: str
    phone_url: str
    selectors: dict[str, str]
    captcha_marker: str = ""
    captcha_wait_seconds: int = 300


@dataclass
class AccountResult:
    account: str
    ok: bool
    phone: str = ""
    code: str = ""
    provider: str = ""
    error: str = ""
    login_method: str = ""     # "code" or "password"
    attempts: list[Any] = field(default_factory=list)


class FlowError(RuntimeError):
    """The page didn't do what the config said it would."""


class WalmartFlow:
    """Drives one browser page through the add-phone journey."""

    def __init__(self, page: Any, cfg: WalmartConfig, shot_dir: str | Path = "screenshots"):
        self.page = page
        self.cfg = cfg
        self.shot_dir = Path(shot_dir)
        self.shot_dir.mkdir(parents=True, exist_ok=True)

    # -- helpers -----------------------------------------------------------
    def _sel(self, key: str) -> str:
        value = (self.cfg.selectors or {}).get(key, "")
        if not value:
            raise FlowError(
                f"no selector configured for '{key}' — set walmart.selectors.{key} "
                "in config.yaml (see the screenshot for what the page looks like)"
            )
        return value

    def shot(self, name: str) -> None:
        try:
            self.page.screenshot(path=str(self.shot_dir / f"walmart-{name}.png"))
        except Exception:
            pass  # a screenshot failing must never fail the run

    def dump(self, name: str) -> None:
        """Screenshot plus the page's HTML.

        A screenshot shows a blocked page or an empty one, but it can't tell you
        what an input is actually called. The HTML can, so save both whenever
        the page is about to be interacted with or has just refused to be.
        """
        self.shot(name)
        try:
            (self.shot_dir / f"walmart-{name}.html").write_text(
                self.page.content(), encoding="utf-8"
            )
        except Exception:
            pass

    def describe(self) -> str:
        """URL, title and a count of the fields — what to say when a fill fails.

        A timeout on `input[type='email']` looks the same whether the page is a
        block page, an empty shell, or the right page with different markup.
        These three numbers tell those apart without opening anything.
        """
        try:
            url = self.page.url
        except Exception:
            url = "?"
        try:
            title = self.page.title()
        except Exception:
            title = "?"
        counts = []
        for label, selector in (
            ("input", "input"),
            ("email-ish", "input[type='email'], input[name*='email' i], input[id*='email' i]"),
            ("password", "input[type='password']"),
            ("button", "button"),
            ("iframe", "iframe"),
        ):
            try:
                counts.append(f"{label}={self.page.locator(selector).count()}")
            except Exception:
                counts.append(f"{label}=?")
        return f"url={url} title={title!r} " + " ".join(counts)

    def _fill(self, key: str, value: str, *, required: bool = True) -> bool:
        selector = (self.cfg.selectors or {}).get(key, "")
        if not selector:
            if required:
                raise FlowError(f"no selector configured for '{key}'")
            return False
        self.page.fill(selector, value)
        return True

    def wait_out_captcha(self) -> None:
        """Pause for a human if a bot check appears. Never tries to solve it."""
        marker = self.cfg.captcha_marker
        if not marker:
            return
        try:
            if self.page.locator(marker).count() == 0:
                return
        except Exception:
            return
        self.shot("captcha")
        print(
            f"\n  !! Bot check on screen. Solve it in the browser window.\n"
            f"     Waiting up to {self.cfg.captcha_wait_seconds}s ...\n"
        )
        deadline = time.monotonic() + self.cfg.captcha_wait_seconds
        while time.monotonic() < deadline:
            try:
                if self.page.locator(marker).count() == 0:
                    return
            except Exception:
                return
            time.sleep(2)
        raise FlowError("bot check was not solved in time")

    # -- steps -------------------------------------------------------------
    def login(
        self, account: WalmartAccount, fetch_email_code: Callable[[], str] | None = None
    ) -> str:
        """Sign in, preferring the emailed one-time code over the password.

        Walmart offers a choice at sign-in: type the password, or have a code
        sent. The code path is the one we want — it doesn't expose the password
        to a bot-check challenge, and it's what the account is set up for.
        The password is only used if no code option is configured or no mailbox
        reader was supplied.

        Returns "code" or "password" for the log.
        """
        self.page.goto(self.cfg.login_url)
        self.wait_out_captcha()
        # Before touching anything. If the email field never appears, this pair
        # of files is the only record of what was actually on screen — the
        # shot below it is taken after the fill and never gets written.
        self.dump("login-page")
        print(f"    login page: {self.describe()}")
        self._fill("login_email", account.email)
        # The decision point: password field, "use a code instead", or a
        # Continue button to a second screen. Whichever it is, this is the
        # screenshot that identifies the selectors, so take it unconditionally.
        self.shot("login-email-entered")

        # Walmart splits email and password across two screens, so a "continue"
        # button between them is optional rather than assumed.
        cont = (self.cfg.selectors or {}).get("login_continue", "")
        if cont:
            self.page.click(cont)
            self.wait_out_captcha()
            self.shot("login-after-continue")

        use_code = (self.cfg.selectors or {}).get("login_use_code", "")
        if use_code and fetch_email_code is not None:
            method = "code"
            self.page.click(use_code)
            self.wait_out_captcha()
            self.shot("login-code-requested")
            code = fetch_email_code()
            if not code:
                raise FlowError(
                    "no sign-in code arrived in the mailbox — check walmart.imap "
                    "and that the code actually went to this account's address"
                )
            self._fill("login_code_input", code)
            self.page.click(self._sel("login_code_submit"))
        else:
            if not account.password:
                raise FlowError(
                    f"{account.email} has no password and the emailed-code path "
                    "isn't configured (set walmart.selectors.login_use_code)"
                )
            method = "password"
            self._fill("login_password", account.password)
            self.page.click(self._sel("login_submit"))

        self.wait_out_captcha()
        self.shot("after-login")

        marker = (self.cfg.selectors or {}).get("logged_in_marker", "")
        if marker and self.page.locator(marker).count() == 0:
            raise FlowError(
                f"login via {method} didn't land on a signed-in page — check the "
                "credentials, or correct walmart.selectors.logged_in_marker"
            )
        return method

    def open_phone_form(self) -> None:
        self.page.goto(self.cfg.phone_url)
        self.wait_out_captcha()
        add = (self.cfg.selectors or {}).get("add_phone_button", "")
        if add:
            self.page.click(add)
        self.shot("phone-form")

    def submit_phone(self, national_number: str) -> None:
        self._fill("phone_input", national_number)
        self.page.click(self._sel("phone_submit"))
        self.wait_out_captcha()
        self.shot("phone-submitted")

    def submit_code(self, code: str) -> None:
        self._fill("code_input", code)
        self.page.click(self._sel("code_submit"))
        self.wait_out_captcha()
        self.shot("code-submitted")

    def confirm(self) -> bool:
        """True if the success marker is present, or none is configured."""
        marker = (self.cfg.selectors or {}).get("success_marker", "")
        if not marker:
            return True  # unset means "don't hard-fail" until we know the page
        try:
            return self.page.locator(marker).count() > 0
        except Exception:
            return False


def add_phone_to_account(
    flow: WalmartFlow,
    account: WalmartAccount,
    acquire_number: Callable[[], Any],
    fetch_email_code: Callable[[], str] | None = None,
) -> AccountResult:
    """One account, end to end.

    `acquire_number` is injected rather than called directly so the browser
    half and the money half stay independently testable — and so it can be
    invoked at exactly the right moment, once the form is on screen.
    """
    try:
        login_method = flow.login(account, fetch_email_code)
        flow.open_phone_form()
    except Exception as e:
        # Capture the page as it stands, not as it was a step ago. Without this
        # a selector timeout says only which selector missed, never why.
        flow.dump("error")
        return AccountResult(
            account.email, False, error=f"{type(e).__name__}: {e} | {flow.describe()}"
        )

    # Only now is it worth spending money: the form is up and waiting.
    try:
        purchase, attempts = acquire_number()
    except Exception as e:
        return AccountResult(account.email, False, error=f"{type(e).__name__}: {e}")

    result = AccountResult(
        account.email,
        False,
        phone=purchase.phone,
        provider=purchase.provider,
        code=purchase.code or "",
        attempts=attempts,
        login_method=login_method,
    )
    try:
        flow.submit_phone(purchase.national)
        flow.submit_code(purchase.code or "")
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        return result

    result.ok = flow.confirm()
    if not result.ok:
        result.error = "submitted, but the success marker wasn't found"
    return result


def load_walmart_config(raw: dict[str, Any] | None) -> WalmartConfig:
    raw = raw or {}
    return WalmartConfig(
        login_url=raw.get("login_url", "https://www.walmart.com/account/login"),
        phone_url=raw.get("phone_url", "https://www.walmart.com/account/profile"),
        selectors=raw.get("selectors") or {},
        captcha_marker=raw.get("captcha_marker", ""),
        captcha_wait_seconds=int(raw.get("captcha_wait_seconds", 300)),
    )
