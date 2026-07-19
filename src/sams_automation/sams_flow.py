"""Playwright driver for the Sam's Club login -> add-member -> verify flow.

Every URL and selector this touches comes from config (`cfg.sams`), so tuning
the automation against the real page never requires editing this file — you
only edit config.yaml.

Because the exact page structure isn't known until we watch a real run, each
step is defensive: it screenshots what it sees, waits for elements, and pauses
for manual CAPTCHA solving when running headful.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
)

from .config import Account, Config
from .imap_client import ImapClient, VerificationResult


class FlowError(Exception):
    """A step in the browser flow failed in a way we can't recover from."""


class SamsFlow:
    def __init__(self, cfg: Config, browser: Browser, imap: ImapClient):
        self.cfg = cfg
        self.browser = browser
        self.imap = imap
        self.sel = cfg.sams.selectors
        Path(cfg.browser.screenshot_dir).mkdir(parents=True, exist_ok=True)
        Path(cfg.browser.state_dir).mkdir(parents=True, exist_ok=True)

    # -- public entrypoint ----------------------------------------------------

    def process(self, account: Account) -> None:
        """Run the full flow for one account. Raises FlowError on failure."""
        context = self._new_context(account)
        page = context.new_page()
        page.set_default_timeout(self.cfg.browser.timeout_ms)
        try:
            self._login(page, account)
            self._open_add_member(page, account)
            trigger_time = datetime.now(timezone.utc)
            self._fill_member_form(page, account)
            self._handle_verification(page, account, trigger_time)
            self._confirm_success(page, account)
            if self.cfg.browser.persist_sessions:
                context.storage_state(path=self._state_path(account))
        finally:
            context.close()

    # -- context / session ----------------------------------------------------

    def _state_path(self, account: Account) -> str:
        safe = account.primary_email.replace("@", "_at_").replace("/", "_")
        return str(Path(self.cfg.browser.state_dir) / f"{safe}.json")

    def _new_context(self, account: Account) -> BrowserContext:
        state = self._state_path(account)
        if self.cfg.browser.persist_sessions and Path(state).exists():
            return self.browser.new_context(storage_state=state)
        return self.browser.new_context()

    # -- steps ----------------------------------------------------------------

    def _login(self, page: Page, account: Account) -> None:
        page.goto(self.cfg.sams.login_url, wait_until="domcontentloaded")
        self._maybe_captcha(page, account)

        # If a saved session already logged us in, skip typing credentials.
        marker = self.sel.get("logged_in_marker")
        if marker and self._is_visible(page, marker, timeout_ms=4000):
            self._shot(page, account, "already-logged-in")
            return

        self._fill(page, "login_email", account.primary_email)
        self._fill(page, "login_password", account.primary_password)
        self._shot(page, account, "login-filled")
        self._click(page, "login_submit")
        self._maybe_captcha(page, account)

        if marker:
            try:
                page.wait_for_selector(marker, timeout=self.cfg.browser.timeout_ms)
            except PWTimeout:
                self._shot(page, account, "login-failed")
                raise FlowError(
                    f"Login did not complete for {account.primary_email} "
                    "(logged_in_marker never appeared)."
                )
        self._shot(page, account, "logged-in")

    def _open_add_member(self, page: Page, account: Account) -> None:
        page.goto(self.cfg.sams.add_member_url, wait_until="domcontentloaded")
        self._maybe_captcha(page, account)
        self._shot(page, account, "add-member-page")

    def _fill_member_form(self, page: Page, account: Account) -> None:
        self._fill(page, "add_first_name", account.secondary_first)
        self._fill(page, "add_last_name", account.secondary_last)
        self._fill(page, "add_email", account.secondary_email)
        self._fill(page, "add_address1", account.address1, required=False)
        if account.address2:
            self._fill(page, "add_address2", account.address2, required=False)
        self._fill(page, "add_city", account.city, required=False)
        self._select(page, "add_state", account.state, required=False)
        self._fill(page, "add_zip", account.zip, required=False)
        if account.phone:
            self._fill(page, "add_phone", account.phone, required=False)
        self._shot(page, account, "member-form-filled")
        self._click(page, "add_submit")
        self._maybe_captcha(page, account)

    def _handle_verification(
        self, page: Page, account: Account, trigger_time: datetime
    ) -> None:
        result = self.imap.wait_for_verification(
            to_address=account.secondary_email,
            since=trigger_time,
            verification=self.cfg.verification,
        )
        if self.cfg.verification.mode == "code":
            self._enter_code(page, account, result)
        else:
            self._open_link(page, account, result)

    def _enter_code(
        self, page: Page, account: Account, result: VerificationResult
    ) -> None:
        if not result.code:
            raise FlowError(f"No code extracted for {account.secondary_email}.")
        self._fill(page, "code_input", result.code)
        self._shot(page, account, "code-entered")
        self._click(page, "code_submit")

    def _open_link(
        self, page: Page, account: Account, result: VerificationResult
    ) -> None:
        if not result.link:
            raise FlowError(f"No link extracted for {account.secondary_email}.")
        page.goto(result.link, wait_until="domcontentloaded")
        self._maybe_captcha(page, account)
        self._shot(page, account, "activation-link-opened")

    def _confirm_success(self, page: Page, account: Account) -> None:
        marker = self.sel.get("success_marker")
        if not marker:
            self._shot(page, account, "done-no-marker")
            return
        try:
            page.wait_for_selector(marker, timeout=self.cfg.browser.timeout_ms)
        except PWTimeout:
            self._shot(page, account, "success-marker-missing")
            raise FlowError(
                f"Secondary member for {account.secondary_email} may not have "
                "been confirmed (success_marker never appeared)."
            )
        self._shot(page, account, "success")

    # -- CAPTCHA --------------------------------------------------------------

    def _maybe_captcha(self, page: Page, account: Account) -> None:
        marker = self.cfg.sams.captcha_marker
        if not marker:
            return
        if not self._is_visible(page, marker, timeout_ms=1500):
            return
        self._shot(page, account, "captcha")
        if self.cfg.browser.headless:
            raise FlowError(
                "CAPTCHA detected while running headless. Re-run with "
                "headless: false in config.yaml so you can solve it."
            )
        print(
            f"\n[!] CAPTCHA detected for {account.label}. Solve it in the "
            f"browser window; waiting up to {self.cfg.sams.captcha_wait_seconds}s..."
        )
        deadline = time.monotonic() + self.cfg.sams.captcha_wait_seconds
        while time.monotonic() < deadline:
            if not self._is_visible(page, marker, timeout_ms=1000):
                print("[+] CAPTCHA cleared, continuing.")
                return
            time.sleep(2)
        raise FlowError("CAPTCHA was not solved in time.")

    # -- low-level helpers ----------------------------------------------------

    def _resolve(self, key: str) -> str | None:
        return self.sel.get(key)

    def _fill(self, page: Page, key: str, value: str, required: bool = True) -> None:
        selector = self._resolve(key)
        if not selector:
            if required:
                raise FlowError(f"Selector '{key}' is not configured.")
            return
        try:
            page.fill(selector, value)
        except PWTimeout:
            if required:
                raise FlowError(f"Could not find field '{key}' ({selector}).")

    def _select(self, page: Page, key: str, value: str, required: bool = True) -> None:
        selector = self._resolve(key)
        if not selector:
            if required:
                raise FlowError(f"Selector '{key}' is not configured.")
            return
        try:
            page.select_option(selector, value)
        except PWTimeout:
            if required:
                raise FlowError(f"Could not find select '{key}' ({selector}).")

    def _click(self, page: Page, key: str) -> None:
        selector = self._resolve(key)
        if not selector:
            raise FlowError(f"Selector '{key}' is not configured.")
        page.click(selector)

    def _is_visible(self, page: Page, selector: str, timeout_ms: int) -> bool:
        try:
            page.wait_for_selector(selector, timeout=timeout_ms, state="visible")
            return True
        except PWTimeout:
            return False

    def _shot(self, page: Page, account: Account, name: str) -> None:
        safe = account.secondary_email.replace("@", "_at_").replace("/", "_")
        ts = datetime.now(timezone.utc).strftime("%H%M%S")
        path = Path(self.cfg.browser.screenshot_dir) / f"{safe}-{name}-{ts}.png"
        try:
            page.screenshot(path=str(path), full_page=True)
        except Exception:
            pass
