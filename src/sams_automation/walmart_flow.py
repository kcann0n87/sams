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
from typing import Any, Callable, Sequence

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

    def settle(self, timeout_ms: int = 15000) -> None:
        """Wait for a click's navigation to finish before reading the page.

        Best effort: a single-page app may never go fully idle, and a step that
        didn't navigate at all shouldn't fail here. Either way the next action
        has its own wait.
        """
        for state in ("domcontentloaded", "networkidle"):
            try:
                self.page.wait_for_load_state(state, timeout=timeout_ms)
            except Exception:
                return

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

    def _present(self, key: str) -> bool:
        """Whether a configured selector matches anything right now."""
        selector = (self.cfg.selectors or {}).get(key, "")
        if not selector:
            return False
        try:
            return self.page.locator(selector).count() > 0
        except Exception:
            return False

    # Words that mark the "send me a one-time code" option apart from the other
    # things on that screen.
    CODE_WORDS = ("code", "passcode", "otp", "one-time", "one time")
    # It has to be the emailed one. The code is read out of a mailbox, so the
    # SMS option would send it to a phone nobody is watching — and on these
    # accounts that phone is the number we're about to add.
    EMAIL_WORDS = ("email", "e-mail", "@")

    # Walmart's method chooser is a radio list, so the option itself is a
    # label or a radio rather than anything that reads as a button.
    CLICKABLE_QUERY = (
        "button, [role=button], [role=radio], input[type=radio], label, "
        "[data-automation-id], [data-testid], a"
    )
    # Choosing a method often only ticks the radio; the email is sent by a
    # separate press.
    PROCEED_SELECTORS = (
        "button:has-text('Continue')",
        "button:has-text('Send')",
        "button:has-text('Next')",
        "button[type='submit']",
    )

    def _clickables(self) -> list[Any]:
        try:
            return self.page.locator(self.CLICKABLE_QUERY).all()[:200]
        except Exception:
            return []

    @staticmethod
    def _text_of(element: Any) -> str:
        """The element's visible text, untruncated.

        Truncating here broke the ranking in find_code_option: every candidate
        longer than the cap tied at the cap, so "smallest text wins" picked
        whichever came first — which was the container wrapping the whole
        screen, heading included. Clicking that did nothing.
        """
        try:
            return " ".join((element.inner_text() or "").split())
        except Exception:
            return ""

    def _url(self) -> str:
        try:
            return str(self.page.url)
        except Exception:
            return ""

    def _option_text(self, element: Any) -> str:
        """Text identifying a control, including ones with no text of their own.

        A radio input has no inner text at all, so matching on inner_text alone
        silently discarded exactly the controls a radio-list chooser is made
        of. Its label carries the wording instead.
        """
        text = self._text_of(element)
        if text:
            return text
        for attribute in ("aria-label", "title", "value", "placeholder"):
            try:
                value = element.get_attribute(attribute)
            except Exception:
                continue
            if value:
                return " ".join(str(value).split())
        try:
            element_id = element.get_attribute("id")
        except Exception:
            element_id = None
        if element_id:
            try:
                label = self.page.locator(f"label[for='{element_id}']")
                if label.count():
                    return self._text_of(label.first)
            except Exception:
                pass
        try:                                   # a radio wrapped in its label
            ancestor = element.locator("xpath=ancestor::label[1]")
            if ancestor.count():
                return self._text_of(ancestor.first)
        except Exception:
            pass
        return ""

    @staticmethod
    def _enabled(element: Any) -> bool:
        try:
            return bool(element.is_enabled())
        except Exception:
            return True   # can't tell: let the click decide

    @staticmethod
    def _activate(element: Any) -> None:
        """Click, or tick if it's a radio — check() handles both correctly."""
        try:
            element.check()
            return
        except Exception:
            pass
        element.click()

    def find_code_option(self) -> Any | None:
        """The "email me a one-time code" control on a code-choice screen.

        Walmart builds these options from divs carrying a role rather than
        buttons, and the ids around them are React-generated and change every
        load, so there is no stable selector to write down. Matching on the
        visible text is what survives a reload.

        Returns the tightest element whose text names both a code and email —
        the innermost span rather than the wrapper, which still triggers the
        clickable parent but can't accidentally be the whole page.
        """
        candidates = []
        everything = []
        for element in self._clickables():
            text = self._option_text(element).lower()
            if not text:
                continue
            everything.append(text)
            if not any(w in text for w in self.CODE_WORDS):
                continue
            if not any(w in text for w in self.EMAIL_WORDS):
                continue
            candidates.append((len(text), element, text))
        if not candidates:
            # Say what was on the screen. Guessing at markup nobody can see is
            # how this took several runs to get right the first time.
            print("    no code option matched. Clickable things on this screen:")
            for seen in sorted(set(everything), key=len)[:25]:
                print(f"      {seen[:90]!r}")
            return None
        candidates.sort(key=lambda c: c[0])
        _, element, text = candidates[0]
        if len(candidates) > 1:
            print(f"    code option: {text[:70]!r} "
                  f"(best of {len(candidates)})")
        else:
            print(f"    code option: {text[:70]!r}")
        return element

    # Ordered best-first. A CSS list can't express preference — it returns
    # whatever comes first in the DOM — so these are tried one at a time.
    CODE_INPUT_SELECTORS = (
        "input[autocomplete='one-time-code']",
        "input[aria-label*='code' i]",
        "input[name*='code' i]",
        "input[id*='otp' i], input[name*='otp' i]",
        "input[inputmode='numeric']",
        "input[type='tel']",
        "input[maxlength='6']",
        # Last: Walmart masks the code on some screens, so the only field left
        # looks like a password one.
        "form input[type='password']",
    )
    CODE_SUBMIT_SELECTORS = (
        "button[type='submit']",
        "button:has-text('Verify')",
        "button:has-text('Continue')",
        "button:has-text('Submit')",
    )

    def _first_visible(self, selectors: Sequence[str]) -> Any | None:
        """First visible element matching any of these, in preference order."""
        for selector in selectors:
            try:
                located = self.page.locator(selector)
                total = located.count()
            except Exception:
                continue
            for index in range(min(total, 10)):
                item = located.nth(index)
                try:
                    if item.is_visible():
                        return item
                except Exception:
                    continue
        return None

    def enter_code(self, code: str) -> bool:
        """Type the sign-in code and submit. False if no field was found.

        Handles the segmented layout too — six boxes of one character each,
        which a single fill would put the whole code into the first of.
        """
        explicit = str((self.cfg.selectors or {}).get("login_code_input", "")).strip()
        if explicit and explicit.lower() != "auto":
            self.page.fill(explicit, code)
        else:
            try:
                boxes = self.page.locator("input[maxlength='1']")
                segmented = boxes.count()
            except Exception:
                segmented = 0
            if segmented >= len(code):
                for position, digit in enumerate(code):
                    boxes.nth(position).fill(digit)
                print(f"    code entered across {len(code)} boxes")
            else:
                field = self._first_visible(self.CODE_INPUT_SELECTORS)
                if field is None:
                    return False
                field.fill(code)
                print("    code entered")

        submit = str((self.cfg.selectors or {}).get("login_code_submit", "")).strip()
        if submit and submit.lower() != "auto":
            self.page.click(submit)
            return True
        button = self._first_visible(self.CODE_SUBMIT_SELECTORS)
        if button is not None:
            button.click()
        # Some of these forms submit themselves once the last box is filled,
        # so a missing button is not a failure.
        return True

    def _code_target(self) -> Any | None:
        """The control to click for an emailed code, or None to use a password.

        `login_use_code: auto` searches the page by its visible text, which is
        the only stable handle Walmart gives here. Any other value is used
        verbatim, so a selector can still be pinned when the search picks
        wrong. Empty keeps the old behaviour of going straight to a password.
        """
        setting = str((self.cfg.selectors or {}).get("login_use_code", "")).strip()
        if not setting:
            return None
        if setting.lower() != "auto":
            # Returned as a string so the click goes through page.click, which
            # waits for the element. Checking it exists first would fall back
            # to a password the moment the option rendered a beat late — and a
            # pinned selector is a deliberate choice worth waiting on.
            return setting
        found = self.find_code_option()
        if found is None:
            # Say so rather than silently typing a password: this is the whole
            # point of the code path, and falling back without a word makes it
            # look like the setting was ignored.
            print("    no emailed-code option found on this screen — using the password")
        return found

    def suggest_code_options(self) -> list[str]:
        """Print the clickable things that look like a code option.

        The alternative is dumping the page and reading it by hand, and this
        screen's options are divs with a role rather than buttons, so they
        don't show up in an obvious place.
        """
        found: list[str] = []
        try:
            elements = self.page.locator(
                "button, [role=button], [data-automation-id], [data-testid], a"
            ).all()
        except Exception:
            return found
        for element in elements[:200]:
            try:
                text = " ".join((element.inner_text() or "").split())[:70]
                if not text or not any(w in text.lower() for w in self.CODE_WORDS):
                    continue
                selector = ""
                for attribute in ("data-automation-id", "data-testid", "id"):
                    value = element.get_attribute(attribute)
                    if value:
                        selector = (
                            f"#{value}" if attribute == "id" else f"[{attribute}='{value}']"
                        )
                        break
                if not selector:
                    label = element.get_attribute("aria-label")
                    selector = f"[aria-label='{label}']" if label else f"text={text[:40]!r}"
            except Exception:
                continue
            found.append(f"      {selector:<44} {text}")
        if found:
            print("\n    Options on this screen that mention a code:")
            for line in found:
                print(line)
            print(
                "\n    Set the one you want with:\n"
                "      .venv/bin/python tools/set_selector.py login_use_code \"<selector>\"\n"
            )
        return found

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
        self.dump("login-email-entered")

        # Walmart splits email and password across two screens, so a "continue"
        # button between them is optional rather than assumed.
        cont = (self.cfg.selectors or {}).get("login_continue", "")
        if cont:
            self.page.click(cont)
            # Continue navigates. Dumping straight away captured the page we
            # just left — email still filled, the button already disabled —
            # which is the opposite of what the dump is for.
            self.settle()
            self.wait_out_captcha()
            self.dump("login-after-continue")
            print(f"    after continue: {self.describe()}")

        target = self._code_target() if fetch_email_code is not None else None
        if target is not None:
            method = "code"
            # A string is a configured selector; anything else is an element
            # the text search already has a handle on.
            before = self._url()
            if isinstance(target, str):
                self.page.click(target)
            else:
                self._activate(target)
            self.settle()
            # Picking the method is not the same as asking for the code. On a
            # radio-list chooser the click only ticks the option, and the email
            # is sent by a separate press — without it the run waits out the
            # whole mailbox timeout for a message nobody sent.
            if self._url() == before:
                proceed = self._first_visible(self.PROCEED_SELECTORS)
                if proceed is None:
                    print("    nothing to press to send the code")
                elif not self._enabled(proceed):
                    # Clicking a disabled button waits the full timeout and
                    # then blames the click. The real cause is upstream: the
                    # option never got selected.
                    print(
                        "    the send button is disabled — the option didn't "
                        "take. Nothing was requested"
                    )
                else:
                    proceed.click()
                    self.settle()
                    print("    pressed continue to send the code")
            self.wait_out_captcha()
            if self._url() == before:
                print(
                    "    WARNING: still on the same screen — the code may not "
                    "have been sent"
                )
            self.dump("login-code-requested")
            print(f"    code requested: {self.describe()}")
            code = fetch_email_code()
            if not code:
                raise FlowError(
                    "no sign-in code arrived in the mailbox — check walmart.imap "
                    "and that the code actually went to this account's address"
                )
            if not self.enter_code(code):
                # No field. Either the page moved on already — someone typed
                # the code by hand while this was reading the mailbox — or the
                # screen is one we can't recognise. Being off the identity host
                # tells those apart.
                self.settle()
                if self.signed_out():
                    self.dump("code-entry-not-found")
                    raise FlowError(
                        "couldn't find the code field on this screen — set "
                        "walmart.selectors.login_code_input from "
                        "screenshots/walmart-code-entry-not-found.html"
                    )
                print("    code field already gone — the page had moved on")
        else:
            if not account.password:
                raise FlowError(
                    f"{account.email} has no password and the emailed-code path "
                    "isn't configured (set walmart.selectors.login_use_code)"
                )
            # Walmart can route to a screen with no password field. Filling one
            # then times out for 30s and blames the selector, when the real
            # answer is that this account is on the code path. Ask the page
            # rather than the URL: the code-choice screen sometimes carries a
            # password field too, and refusing on the URL alone would block a
            # sign-in that works.
            if not self._present("login_password"):
                self.suggest_code_options()
                raise FlowError(
                    "no password field on this screen — it's Walmart's code-choice "
                    "page. Set walmart.selectors.login_use_code to one of the "
                    "options listed above"
                )
            method = "password"
            self._fill("login_password", account.password)
            self.page.click(self._sel("login_submit"))

        self.settle()
        self.wait_out_captcha()
        self.dump("after-login")

        marker = (self.cfg.selectors or {}).get("logged_in_marker", "")
        if marker:
            if self.page.locator(marker).count() == 0:
                raise FlowError(
                    f"login via {method} didn't land on a signed-in page — check the "
                    "credentials, or correct walmart.selectors.logged_in_marker"
                )
        elif self.signed_out():
            # Without a marker there was no check at all, so a failed sign-in
            # sailed on into the purchase step and the "phone form" dump came
            # back as the login page. Still being on the identity host is
            # proof enough, and costs no configuration.
            raise FlowError(
                f"login via {method} didn't work — still on {self.page.url.split('?')[0]}. "
                "Check the credentials, or set walmart.selectors.logged_in_marker "
                "if this is a false alarm"
            )
        return method

    # Walmart runs sign-in on its own host, so being there afterwards means it
    # didn't take. Any account page redirects here when the session is dead.
    SIGNED_OUT_MARKERS = ("identity.walmart.com", "/account/login", "/account/signin")

    def signed_out(self) -> bool:
        try:
            url = str(self.page.url)
        except Exception:
            return False
        return any(m in url for m in self.SIGNED_OUT_MARKERS)

    def open_phone_form(self) -> None:
        self.page.goto(self.cfg.phone_url)
        self.settle()
        self.wait_out_captcha()
        if self.signed_out():
            self.dump("phone-form")
            raise FlowError(
                f"{self.cfg.phone_url} redirected to sign-in — the session isn't "
                "logged in. Nothing was bought"
            )
        add = (self.cfg.selectors or {}).get("add_phone_button", "")
        if add:
            self.page.click(add)
            self.settle()
        self.dump("phone-form")
        print(f"    phone form: {self.describe()}")

    def submit_phone(self, national_number: str) -> None:
        self._fill("phone_input", national_number)
        self.page.click(self._sel("phone_submit"))
        self.wait_out_captcha()
        self.dump("phone-submitted")

    def submit_code(self, code: str) -> None:
        self._fill("code_input", code)
        self.page.click(self._sel("code_submit"))
        self.wait_out_captcha()
        self.dump("code-submitted")

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
