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
    Playwright,
    TimeoutError as PWTimeout,
)

from .config import Account, Config, VerificationConfig
from .imap_client import ImapClient
from .proxies import ProxyPool
from .stealth import STEALTH_INIT_JS


class FlowError(Exception):
    """A step in the browser flow failed in a way we can't recover from."""


class SamsFlow:
    def __init__(
        self,
        cfg: Config,
        playwright: Playwright,
        imap: ImapClient,
        proxy_pool: ProxyPool | None = None,
        shared_context: BrowserContext | None = None,
    ):
        self.cfg = cfg
        self.pw = playwright
        self.imap = imap
        self.proxy_pool = proxy_pool
        # When set (real-Chrome persistent-profile mode), one warmed profile is
        # reused across all accounts and we log out/in between them.
        self.shared_context = shared_context
        # The browser currently driving an ephemeral run, so phase 2 can open a
        # brand-new context for the secondary member. None in shared-profile mode.
        self._current_browser: Browser | None = None
        self.sel = cfg.sams.selectors
        Path(cfg.browser.screenshot_dir).mkdir(parents=True, exist_ok=True)
        Path(cfg.browser.state_dir).mkdir(parents=True, exist_ok=True)

    # -- public entrypoint ----------------------------------------------------

    def process(self, account: Account) -> None:
        """Run the full flow for one account. Raises FlowError on failure."""
        if self.shared_context is not None:
            self._process_shared(account)
        else:
            self._process_ephemeral(account)

    def _process_shared(self, account: Account) -> None:
        """Use the one persistent (warmed) Chrome profile, switching accounts."""
        page = self.shared_context.new_page()
        page.set_default_timeout(self.cfg.browser.timeout_ms)
        try:
            self._login(page, account, shared=True)
            self._run_member_flow(page, account)
        finally:
            page.close()

    def _process_ephemeral(self, account: Account) -> None:
        """Launch a throwaway browser per account (optionally via a proxy)."""
        launch_kwargs: dict = {
            "headless": self.cfg.browser.headless,
            "slow_mo": self.cfg.browser.slow_mo_ms,
        }
        if self.cfg.browser.channel:
            launch_kwargs["channel"] = self.cfg.browser.channel
        proxy = (
            self.proxy_pool.for_account(account.primary_email)
            if self.proxy_pool
            else None
        )
        if proxy:
            launch_kwargs["proxy"] = proxy.to_playwright()
            print(f"    via proxy {proxy.label}")

        browser = self.pw.chromium.launch(**launch_kwargs)
        self._current_browser = browser
        try:
            context = self._new_context(browser, account)
            if self.cfg.browser.stealth:
                context.add_init_script(STEALTH_INIT_JS)
            page = context.new_page()
            page.set_default_timeout(self.cfg.browser.timeout_ms)
            try:
                self._login(page, account, shared=False)
                self._run_member_flow(page, account)
                if self.cfg.browser.persist_sessions:
                    context.storage_state(path=self._state_path(account))
            finally:
                context.close()
        finally:
            self._current_browser = None
            browser.close()

    def _run_member_flow(self, page: Page, account: Account) -> None:
        """Both sides of one membership, end to end.

        Phase 1 (MAIN account): the primary — already logged in on ``page`` —
        adds the secondary member and submits the form. This triggers the
        activation email to the member's address.

        Phase 2 (SECONDARY member): the new member activates their own
        membership. By default this runs in a fresh browser session (see
        ``activation.new_session``) so it behaves like the member setting things
        up themselves, not the primary doing it from their signed-in session.
        """
        # New membership number — read out of the activation email in phase 2.
        self._member_number: str | None = None

        # ---- Phase 1: main account adds the member ----------------------------
        print("    [phase 1/2] main account: adding the secondary member")
        self._open_add_member(page, account)
        trigger_time = datetime.now(timezone.utc)
        self._fill_member_form(page, account)

        # ---- Phase 2: the new member activates their membership --------------
        print("    [phase 2/2] secondary member: activating the new membership")
        self._activate_secondary(page, account, trigger_time)

    def _activate_secondary(
        self, primary_page: Page, account: Account, trigger_time: datetime
    ) -> None:
        """Wait for the activation email, then finish setup AS the new member.

        Pulls the code/link out of the catch-all inbox, opens it (in a fresh
        session by default), sets the member's own password, and confirms.
        """
        result = self.imap.wait_for_verification(
            to_address=account.secondary_email,
            since=trigger_time,
            verification=self.cfg.verification,
        )
        # The membership number to paste into registration comes from the email.
        if result.member_number:
            self._member_number = result.member_number
            print(f"    new membership number: {self._member_number}")

        page, cleanup = self._secondary_page(primary_page)
        try:
            if self.cfg.verification.mode == "link":
                if not result.link:
                    raise FlowError(
                        f"No activation link extracted for {account.secondary_email}."
                    )
                page.goto(result.link, wait_until="domcontentloaded")
                self._maybe_captcha(page, account)
            else:  # "code"
                if self.cfg.activation.url:
                    page.goto(self.cfg.activation.url, wait_until="domcontentloaded")
                    self._maybe_captcha(page, account)
                if not result.code:
                    raise FlowError(
                        f"No activation code extracted for {account.secondary_email}."
                    )
                # Prefer a dedicated activation field; fall back to the generic one.
                code_key = "activate_code_input" if self.sel.get(
                    "activate_code_input"
                ) else "code_input"
                submit_key = "activate_submit" if self.sel.get(
                    "activate_submit"
                ) else "code_submit"
                self._fill(page, code_key, result.code)
                self._shot(page, account, "activation-code-entered")
                self._click(page, submit_key)
                self._maybe_captcha(page, account)

            self._dump(page, "PAGE-activation")
            self._shot(page, account, "activation-opened")
            self._register_membership(page, account)
            self._set_secondary_password(page, account)
            self._confirm_success(page, account)
        finally:
            cleanup()

    def _register_membership(self, page: Page, account: Account) -> None:
        """The "Register your membership" page: confirm info, then Continue.

        Real page (Confirm your information): Membership number (17 digits) +
        First name + Last name -> Continue. The number comes from the email; the
        names from the CSV. Fields are matched by their visible labels (with
        optional selector overrides), so this works inside Sam's iframes.
        """
        if not self._member_number:
            raise FlowError(
                f"{account.secondary_email}: registration needs the new membership "
                "number, but none was found in the activation email. Check "
                "verification.member_number_regex."
            )
        num_sel = self.sel.get("activate_member_number")
        if num_sel:
            self._fill(page, "activate_member_number", self._member_number)
        else:
            self._fill_by_label(page, "Membership number", self._member_number)
        # First/Last must match what Sam's has on file (from the CSV).
        self._fill_by_label(page, "First name", account.secondary_first,
                            required=False)
        self._fill_by_label(page, "Last name", account.secondary_last,
                            required=False)
        self._shot(page, account, "registration-filled")
        if not self._click_role_button(page, "Continue"):
            self._click_first_any(
                page, ['button:has-text("Continue")', "button[type='submit']"]
            )
        self._maybe_captcha(page, account)
        # Whatever comes after Continue (email / create password) — capture it so
        # we can finish wiring, and set the password if the field is present.
        page.wait_for_timeout(2500)
        self._dump(page, "PAGE-registration-next")
        self._shot(page, account, "registration-continued")

    def _secondary_page(self, primary_page: Page):
        """Return ``(page, cleanup)`` for the secondary member's activation.

        With ``activation.new_session`` (default) this is a brand-new context —
        a clean session, as if the member opened the email on their own device.
        Otherwise it reuses the primary's page (legacy single-session behavior).
        """
        if not self.cfg.activation.new_session:
            return primary_page, (lambda: None)

        if self._current_browser is not None:
            # Ephemeral mode: a fresh incognito context is a truly separate login.
            ctx = self._current_browser.new_context()
            if self.cfg.browser.stealth:
                ctx.add_init_script(STEALTH_INIT_JS)
            page = ctx.new_page()
            page.set_default_timeout(self.cfg.browser.timeout_ms)
            return page, ctx.close

        # Shared persistent-profile mode can't spawn an isolated context, so use a
        # fresh page/tab in the warmed profile. (It shares cookies with the
        # primary; an activation link still opens fine there.)
        page = self.shared_context.new_page()
        page.set_default_timeout(self.cfg.browser.timeout_ms)
        return page, page.close

    def _set_secondary_password(self, page: Page, account: Account) -> None:
        """Set the new member's OWN password to complete activation.

        Skipped (a no-op) until the ``create_password`` selector is configured,
        which we confirm from the real activation page on the first run.
        """
        if not self.sel.get("create_password"):
            self._shot(page, account, "activation-no-password-step")
            return
        if not account.secondary_password:
            raise FlowError(
                f"{account.secondary_email}: activation needs a "
                "secondary_password but the account row has none."
            )
        self._maybe_captcha(page, account)
        self._fill(page, "create_password", account.secondary_password)
        self._fill(
            page, "create_password_confirm", account.secondary_password,
            required=False,
        )
        self._shot(page, account, "secondary-password-filled")
        self._click(page, "create_submit")
        self._maybe_captcha(page, account)

    # -- context / session ----------------------------------------------------

    def _state_path(self, account: Account) -> str:
        safe = account.primary_email.replace("@", "_at_").replace("/", "_")
        return str(Path(self.cfg.browser.state_dir) / f"{safe}.json")

    def _new_context(self, browser: Browser, account: Account) -> BrowserContext:
        state = self._state_path(account)
        if self.cfg.browser.persist_sessions and Path(state).exists():
            return browser.new_context(storage_state=state)
        return browser.new_context()

    # -- steps ----------------------------------------------------------------

    # Candidate selectors tried in order (Sam's markup varies / is unknown).
    EMAIL_CANDIDATES = [
        "input[type='email']",
        "input[name='email']",
        "#email",
        "input[autocomplete='username']",
        "input[name='loginId']",
        "input[name='userId']",
    ]
    PASSWORD_CANDIDATES = [
        "input[type='password']",
        "input[name='password']",
        "#password",
        "input[autocomplete='current-password']",
    ]

    def _login(self, page: Page, account: Account, shared: bool) -> None:
        marker = self.sel.get("logged_in_marker")

        if shared:
            # One shared profile serves every account, so make sure we're not
            # still signed in as the previous member before logging in — and
            # verify it, since a stale session would run this account as the
            # previous one.
            self._ensure_logged_out(page, account)
        else:
            page.goto(self.cfg.sams.login_url, wait_until="domcontentloaded")

        # In ephemeral mode a saved session may already have us logged in.
        if not shared and marker and self._is_visible(page, marker, timeout_ms=4000):
            self._shot(page, account, "already-logged-in")
            return

        # Sam's Club fronts the login form with a PerimeterX "press & hold"
        # challenge. Wait for the real email field to appear, prompting the user
        # to solve the challenge by hand in the Chrome window; only then fill.
        email_sel = self._resolve("login_email")
        candidates = ([email_sel] if email_sel else []) + self.EMAIL_CANDIDATES
        # Sam's renders the sign-in form inside a /login/embed iframe, so we look
        # in the page AND every child frame and remember which one holds it.
        scope, found = self._wait_for_login_form(page, account, candidates)
        self._dump(page, "PAGE-login-form")  # capture the real form once visible
        if not found:
            raise FlowError(
                f"Login form never appeared for {account.primary_email} — the "
                "bot challenge may not have been solved. See the captured page."
            )

        scope.fill(found, account.primary_email)
        submit_sels = [self._resolve("login_submit"), "button[type='submit']"]
        # Sam's is iframe-heavy, so a bare button[type=submit] can click the wrong
        # thing. Target the real buttons by their visible label first.
        continue_sels = ['button:has-text("Continue")', *submit_sels]
        signin_sels = ['button:has-text("Sign In")', *submit_sels]

        # Log in with an emailed code instead of a password (works for accounts
        # with no password; the code lands in the inbox we already read).
        if self.cfg.sams.login_method == "email_code":
            self._login_with_email_code(page, account, scope, continue_sels)
            self._shot(page, account, "logged-in")
            return

        # Getting to the password field can take up to three shapes on Sam's:
        #   (a) it's already on this page/frame,
        #   (b) it appears after a 'Continue' click past the email step,
        #   (c) Sam's shows a "choose a sign-in method" page and you must pick
        #       "Enter your password" before the field is revealed.
        # Match the "Enter your password" option; overridable in config.
        pw_option = self._resolve("login_password_option") or "text=Enter your password"

        pwd_scope, pwd = self._find_visible(page, self.PASSWORD_CANDIDATES, timeout_ms=3000)
        if not pwd:  # (b) advance past the email step
            if not self._click_in_scope(scope, continue_sels):
                self._click_first_any(page, continue_sels)
            self._maybe_captcha(page, account)
            pwd_scope, pwd = self._find_visible(page, self.PASSWORD_CANDIDATES,
                                                timeout_ms=6000)
        if not pwd:  # (c) pick the password sign-in method, then look again
            self._shot(page, account, "choose-signin-method")
            if self._click_first_any(page, [pw_option]):
                pwd_scope, pwd = self._find_visible(
                    page, self.PASSWORD_CANDIDATES,
                    timeout_ms=self.cfg.browser.timeout_ms,
                )
        if not pwd:
            self._dump(page, "NOTFOUND-password")
            raise FlowError(
                "Reached the sign-in page but couldn't find the password field. "
                "If Sam's showed a 'choose a sign-in method' step, set "
                "sams.selectors.login_password_option to match that option."
            )
        pwd_scope.fill(pwd, account.primary_password)
        # In a shared profile, don't let the session stick — we log out between
        # accounts, and "Stay signed in" fights that.
        if self.shared_context is not None:
            self._uncheck_stay_signed_in(pwd_scope)
        self._shot(page, account, "login-filled")
        # Click "Sign In" inside the same frame as the password; fall back widely.
        if not self._click_in_scope(pwd_scope, signin_sels):
            self._click_first_any(page, signin_sels)
        self._maybe_captcha(page, account)
        self._confirm_signed_in(page, account, pwd_scope, pwd, marker)
        self._shot(page, account, "logged-in")

    def _confirm_signed_in(self, page, account, pwd_scope, pwd_sel, marker) -> None:
        """Make sure the sign-in actually went through before moving on.

        If the password field is still on screen a few seconds after Sign In,
        the submit didn't take — try Enter as a fallback, then fail with a clear
        message rather than silently walking into a bounced-to-login page.
        """
        if marker:
            try:
                page.wait_for_selector(marker, timeout=self.cfg.browser.timeout_ms)
                return
            except PWTimeout:
                self._shot(page, account, "login-failed")
                raise FlowError(
                    f"Login did not complete for {account.primary_email} "
                    "(logged_in_marker never appeared)."
                )
        # No marker configured: use "did the sign-in form go away?" as the signal.
        page.wait_for_timeout(3500)
        still_scope, still = self._find_visible(page, self.PASSWORD_CANDIDATES,
                                                timeout_ms=1500)
        if still:  # try submitting via Enter in the password field
            try:
                still_scope.press(still, "Enter")
            except Exception:
                pass
            self._maybe_captcha(page, account)
            page.wait_for_timeout(3500)
            still_scope, still = self._find_visible(page, self.PASSWORD_CANDIDATES,
                                                    timeout_ms=1500)
        if still:
            self._dump(page, "login-not-advancing")
            raise FlowError(
                f"Entered the password for {account.primary_email} but sign-in "
                "didn't complete — still on the sign-in form. The Sign In button "
                "may need a different selector (see the captured page)."
            )

    # Candidate selectors for a one-time-code entry field.
    CODE_CANDIDATES = [
        "input[autocomplete='one-time-code']",
        "input[inputmode='numeric']",
        "input[name='code']",
        "input[name='otp']",
        "input[type='tel']",
    ]

    def _login_with_email_code(self, page: Page, account: Account, scope,
                               continue_sels: list) -> None:
        """Sign in via "Email me a verification code" — no password needed.

        Picks the email-code option on Sam's "choose a sign-in method" page,
        sends the code, reads it from the catch-all inbox (addressed to this
        primary login email), and enters it.
        """
        option = self._resolve("login_email_code_option") or \
            "text=Email me a verification code"
        # Advance from the email step to the choose-method page if needed.
        if not self._first_visible(page, [option], timeout_ms=3000):
            if not self._click_in_scope(scope, continue_sels):
                self._click_first_any(page, continue_sels)
            self._maybe_captcha(page, account)
        if not self._first_visible(page, [option], timeout_ms=self.cfg.browser.timeout_ms):
            self._dump(page, "NOTFOUND-email-code-option")
            raise FlowError(
                "Couldn't find the 'Email me a verification code' option on the "
                "sign-in page. Set sams.selectors.login_email_code_option."
            )
        self._click_first_any(page, [option])
        trigger = datetime.now(timezone.utc)
        # Send the code.
        self._click_first_any(page, [
            'button:has-text("Send Code")', 'button:has-text("Send code")',
            'button:has-text("Continue")', *continue_sels,
        ])
        self._maybe_captcha(page, account)
        self._shot(page, account, "login-code-requested")

        # Read the login code from the inbox (sent to this primary login email).
        ver = VerificationConfig(
            mode="code",
            code_regex=self.cfg.sams.login_code_regex or r"\b(\d{6})\b",
            link_regex="",
        )
        result = self.imap.wait_for_verification(
            to_address=account.primary_email, since=trigger, verification=ver
        )
        if not result.code:
            raise FlowError(f"No login code email arrived for {account.primary_email}.")
        print(f"    login code: {result.code}")

        code_cands = [self._resolve("login_code_input"), *self.CODE_CANDIDATES]
        cscope, csel = self._find_visible(page, code_cands,
                                          timeout_ms=self.cfg.browser.timeout_ms)
        if not csel:
            self._dump(page, "NOTFOUND-login-code-input")
            raise FlowError("Couldn't find the login code entry field.")
        cscope.fill(csel, result.code)
        self._shot(page, account, "login-code-entered")
        self._click_first_any(page, [
            'button:has-text("Sign In")', 'button:has-text("Verify")',
            'button:has-text("Continue")', *continue_sels,
        ])
        self._maybe_captcha(page, account)

    def _ensure_logged_out(self, page: Page, account: Account) -> None:
        """Guarantee a clean, signed-out login page before signing in.

        Shared-profile runs reuse one browser across accounts, so a leftover
        session would run this account as the previous one. We hit the logout
        URL and confirm the email-entry field shows; if the profile still
        remembers the last account we click "Change"; and if it's still signed
        in we wipe cookies AND browser storage (Sam's keeps you signed in via
        storage, not just cookies). If it STILL won't clear, we stop rather than
        risk running this account as the previous one.
        """
        print("    ensuring the previous account is signed out...")
        if self.cfg.sams.logout_url:
            try:
                page.goto(self.cfg.sams.logout_url, wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
            except Exception:
                pass
        if self._on_login_email_page(page):
            return
        # "Welcome back" remembers the previous account — reset to email entry.
        if self._click_first_any(page, ['a:has-text("Change")',
                                        'button:has-text("Change")']):
            page.wait_for_timeout(1000)
            if self._on_login_email_page(page):
                return
        # Still signed in: wipe cookies + storage, then reload.
        print("    still signed in — clearing the profile's cookies and storage...")
        self._clear_session()
        if self._on_login_email_page(page):
            return
        self._dump(page, "logout-failed")
        raise FlowError(
            f"Couldn't sign out the previous account before starting "
            f"{account.primary_email}. Stopping so this account isn't run as the "
            "previous one. Fully quit Chrome (Cmd+Q) and re-run; if it persists, "
            "delete the 'chrome-profile' folder to start fresh."
        )

    def _on_login_email_page(self, page: Page) -> bool:
        """True once the clean email-entry login page is showing."""
        page.goto(self.cfg.sams.login_url, wait_until="domcontentloaded")
        return bool(self._first_visible(page, self.EMAIL_CANDIDATES, timeout_ms=5000))

    def _clear_session(self) -> None:
        """Wipe cookies and site storage for the shared profile."""
        ctx = self.shared_context
        if ctx is None:
            return
        try:
            ctx.clear_cookies()
        except Exception:
            pass
        try:
            p = ctx.new_page()
            try:
                p.goto("https://www.samsclub.com/", wait_until="domcontentloaded",
                       timeout=20000)
                p.evaluate(
                    "() => { try { localStorage.clear(); sessionStorage.clear(); } "
                    "catch (e) {} }"
                )
            finally:
                p.close()
        except Exception:
            pass

    def _uncheck_stay_signed_in(self, scope) -> None:
        """Best-effort: untick the 'Stay signed in' box so sessions don't persist."""
        try:
            cb = scope.get_by_label("Stay signed in")
            if cb.count() and cb.is_checked():
                cb.uncheck()
        except Exception:
            pass

    def _wait_for_login_form(self, page: Page, account: Account, candidates: list):
        """Poll for the login field, prompting the user to solve any challenge.

        Returns ``(scope, selector)`` where ``scope`` is the page or the frame
        that holds the field, or ``(None, None)`` on timeout.
        """
        deadline = time.monotonic() + self.cfg.sams.captcha_wait_seconds
        prompted = False
        while time.monotonic() < deadline:
            scope, sel = self._find_visible(page, candidates, timeout_ms=1500)
            if sel:
                return scope, sel
            if not prompted:
                self._dump(page, "PAGE-login-challenge")
                print(
                    "\n[!] Sam's Club is showing a 'press & hold' challenge on the "
                    "login page.\n    Solve it in the Chrome window that opened; "
                    "the tool is waiting and will continue automatically.\n"
                )
                prompted = True
            time.sleep(2)
        return None, None

    def _scopes(self, page: Page) -> list:
        """The page plus every child frame — Sam's login lives in an iframe."""
        scopes: list = [page]
        try:
            for fr in page.frames:
                if fr is not page.main_frame:
                    scopes.append(fr)
        except Exception:
            pass
        return scopes

    def _find_visible(self, page: Page, selectors: list, timeout_ms: int):
        """First (scope, selector) visible in the page or any frame, else (None, None)."""
        sels = [s for s in selectors if s]
        if not sels:
            return None, None
        per = max(400, timeout_ms // len(sels))
        for sel in sels:
            for scope in self._scopes(page):
                try:
                    scope.wait_for_selector(sel, timeout=per, state="visible")
                    return scope, sel
                except PWTimeout:
                    continue
                except Exception:
                    continue
        return None, None

    def _first_visible(self, page: Page, selectors: list, timeout_ms: int) -> str | None:
        """Return the first selector that becomes visible (any frame), else None."""
        _, sel = self._find_visible(page, selectors, timeout_ms)
        return sel

    def _fill_by_label(self, page: Page, label: str, value: str,
                       required: bool = True) -> bool:
        """Fill the input with this visible label, across the page and its frames."""
        for scope in self._scopes(page):
            try:
                loc = scope.get_by_label(label, exact=False)
                if loc.count() and loc.first.is_visible():
                    loc.first.fill(value)
                    return True
            except Exception:
                continue
        if required:
            self._dump(page, f"NOTFOUND-label-{label.replace(' ', '_')}")
            raise FlowError(f"Could not find a field labeled '{label}'.")
        return False

    def _click_role_button(self, page: Page, name: str) -> bool:
        """Click a button by its visible name, across the page and its frames."""
        for scope in self._scopes(page):
            try:
                loc = scope.get_by_role("button", name=name, exact=False)
                if loc.count() and loc.first.is_visible():
                    loc.first.click()
                    return True
            except Exception:
                continue
        return False

    def _click_in_scope(self, scope, selectors: list) -> bool:
        """Click the first visible selector within one specific scope (page/frame)."""
        for sel in selectors:
            if not sel:
                continue
            try:
                scope.wait_for_selector(sel, timeout=2500, state="visible")
                scope.click(sel)
                return True
            except PWTimeout:
                continue
            except Exception:
                continue
        return False

    def _click_first_any(self, page: Page, selectors: list) -> bool:
        """Click the first visible selector across the page and its frames."""
        for sel in selectors:
            if not sel:
                continue
            for scope in self._scopes(page):
                try:
                    scope.wait_for_selector(sel, timeout=1200, state="visible")
                    scope.click(sel)
                    return True
                except PWTimeout:
                    continue
                except Exception:
                    continue
        return False

    def _click_first(self, page: Page, selectors: list) -> None:
        self._click_first_any(page, selectors)

    def _open_add_member(self, page: Page, account: Account) -> None:
        page.goto(self.cfg.sams.add_member_url, wait_until="domcontentloaded")
        self._maybe_captcha(page, account)
        self._dump(page, "PAGE-add-member")
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
        page.wait_for_timeout(2500)  # let the confirmation render
        self._dump(page, "PAGE-after-save")

    def _confirm_success(self, page: Page, account: Account) -> None:
        # An activation-specific marker wins if configured, else the generic one.
        marker = self.sel.get("activation_success_marker") or self.sel.get(
            "success_marker"
        )
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
        # Frame-aware: Sam's renders forms inside iframes, so search every frame.
        scope, sel = self._find_visible(page, [selector],
                                        timeout_ms=self.cfg.browser.timeout_ms)
        if not sel:
            if required:
                self._dump(page, f"NOTFOUND-{key}")
                raise FlowError(f"Could not find field '{key}' ({selector}).")
            return
        scope.fill(sel, value)

    def _select(self, page: Page, key: str, value: str, required: bool = True) -> None:
        selector = self._resolve(key)
        if not selector:
            if required:
                raise FlowError(f"Selector '{key}' is not configured.")
            return
        scope, sel = self._find_visible(page, [selector],
                                        timeout_ms=self.cfg.browser.timeout_ms)
        if not sel:
            if required:
                self._dump(page, f"NOTFOUND-{key}")
                raise FlowError(f"Could not find select '{key}' ({selector}).")
            return
        scope.select_option(sel, value)

    def _click(self, page: Page, key: str) -> None:
        selector = self._resolve(key)
        if not selector:
            raise FlowError(f"Selector '{key}' is not configured.")
        scope, sel = self._find_visible(page, [selector],
                                        timeout_ms=self.cfg.browser.timeout_ms)
        if not sel:
            self._dump(page, f"NOTFOUND-{key}")
            raise FlowError(f"Could not click '{key}' ({selector}).")
        scope.click(sel)

    def _dump(self, page: Page, name: str) -> None:
        """Save a screenshot AND the page's HTML — used to find real selectors.

        Sam's puts the login (and other) forms inside iframes, whose contents
        aren't in ``page.content()``. So we also write each child frame's HTML to
        ``<name>-frameN.html`` — that's where the real fields live.
        """
        ts = datetime.now(timezone.utc).strftime("%H%M%S")
        base = Path(self.cfg.browser.screenshot_dir) / f"{name}-{ts}"
        try:
            page.screenshot(path=str(base) + ".png", full_page=True)
        except Exception:
            pass
        try:
            Path(str(base) + ".html").write_text(page.content(), encoding="utf-8")
        except Exception:
            pass
        # Each iframe's own HTML (login form lives in one of these).
        try:
            i = 0
            for fr in page.frames:
                if fr is page.main_frame:
                    continue
                try:
                    html = fr.content()
                except Exception:
                    continue
                if html and html.strip():
                    Path(f"{base}-frame{i}.html").write_text(html, encoding="utf-8")
                    i += 1
        except Exception:
            pass

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
