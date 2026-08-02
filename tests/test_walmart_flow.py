"""Offline tests for the Walmart add-phone flow.

The page is a fake and the purchase is injected, so the ordering and failure
handling are testable with no browser and no money.

Run with:  python tests/test_walmart_flow.py   (or)   python -m pytest
"""

from __future__ import annotations

import contextlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sams_automation.config import WalmartAccount, load_walmart_accounts  # noqa: E402
from sams_automation.sms_purchase import Purchase  # noqa: E402
from sams_automation.walmart_flow import (  # noqa: E402
    FlowError,
    WalmartFlow,
    add_phone_to_account,
    load_walmart_config,
)

SELECTORS = {
    "login_email": "#email",
    "login_password": "#password",
    "login_submit": "#signin",
    "phone_input": "#phone",
    "phone_submit": "#addphone",
    "code_input": "#code",
    "code_submit": "#verify",
}


class FakeLocator:
    def __init__(self, n: int):
        self._n = n

    def count(self) -> int:
        return self._n


class FakePage:
    """Records what the flow did, and can be told to fail at a given step."""

    def __init__(
        self,
        *,
        present: set[str] | None = None,
        missing: set[str] | None = None,
        fail_on: str | None = None,
    ):
        self.filled: dict[str, str] = {}
        self.clicked: list[str] = []
        self.visited: list[str] = []
        self.present = present or set()
        # Fields the page doesn't have. A real page that accepts a fill has
        # the field, so the configured selectors count as present by default
        # and this is how a test says one is absent.
        self.missing = missing or set()
        self.fail_on = fail_on

    def goto(self, url: str) -> None:
        self.visited.append(url)

    def fill(self, selector: str, value: str) -> None:
        if self.fail_on == selector:
            raise RuntimeError(f"element not found: {selector}")
        self.filled[selector] = value

    def click(self, selector: str) -> None:
        if self.fail_on == selector:
            raise RuntimeError(f"element not found: {selector}")
        self.clicked.append(selector)

    def locator(self, selector: str) -> FakeLocator:
        if selector in self.missing:
            return FakeLocator(0)
        if selector in self.present:
            return FakeLocator(1)
        # Markers must be declared with present=; a configured field exists.
        return FakeLocator(1 if selector in set(SELECTORS.values()) else 0)

    def all(self):
        return []

    def screenshot(self, path: str) -> None:
        pass

    def set_default_timeout(self, ms: int) -> None:
        pass

    # -- what the diagnostics read ----------------------------------------
    url = "https://wm/login"

    def title(self) -> str:
        return "Sign in"

    def content(self) -> str:
        return "<html><body><input type='email'></body></html>"


def _flow(page: FakePage, **overrides):
    cfg = load_walmart_config(
        {"login_url": "https://wm/login", "phone_url": "https://wm/profile",
         "selectors": {**SELECTORS, **overrides}}
    )
    return WalmartFlow(page, cfg, shot_dir=tempfile.mkdtemp())


def _purchase(code="445566"):
    return Purchase(
        provider="daisysms", order_id="1", phone="+13055550123",
        price=0.35, currency="USD", service_code="wm", code=code,
    )


ACCOUNT = WalmartAccount(email="a@example.com", password="pw")


# --- the happy path -------------------------------------------------------


def test_full_flow_fills_everything_in_order():
    page = FakePage()
    flow = _flow(page)
    result = add_phone_to_account(flow, ACCOUNT, lambda: (_purchase(), []))

    assert result.ok, result.error
    assert page.filled["#email"] == "a@example.com"
    assert page.filled["#password"] == "pw"
    # The form wants the 10-digit national form, not +1...
    assert page.filled["#phone"] == "3055550123"
    assert page.filled["#code"] == "445566"
    assert page.clicked == ["#signin", "#addphone", "#verify"]
    assert page.visited == ["https://wm/login", "https://wm/profile"]


def test_result_carries_the_number_and_provider():
    flow = _flow(FakePage())
    result = add_phone_to_account(flow, ACCOUNT, lambda: (_purchase(), []))
    assert result.phone == "+13055550123"
    assert result.provider == "daisysms"
    assert result.code == "445566"


# --- ordering: buy last ---------------------------------------------------


def test_number_is_bought_only_after_the_form_is_up():
    # A bought number starts expiring immediately, so a slow login or a
    # CAPTCHA must not be burning its clock.
    order: list[str] = []

    class TracingPage(FakePage):
        def goto(self, url):
            order.append(f"goto {url}")
            super().goto(url)

    page = TracingPage()

    def acquire():
        order.append("BUY")
        return _purchase(), []

    add_phone_to_account(_flow(page), ACCOUNT, acquire)
    assert order.index("BUY") > order.index("goto https://wm/profile")


def test_no_number_is_bought_when_login_fails():
    bought = []

    def acquire():
        bought.append(1)
        return _purchase(), []

    page = FakePage(fail_on="#signin")
    result = add_phone_to_account(_flow(page), ACCOUNT, acquire)
    assert not result.ok
    assert not bought, "spent money despite never reaching the form"


def test_login_marker_failure_stops_before_buying():
    bought = []

    def acquire():
        bought.append(1)
        return _purchase(), []

    # Marker configured but absent => not actually signed in.
    flow = _flow(FakePage(), logged_in_marker="#account-menu")
    result = add_phone_to_account(flow, ACCOUNT, acquire)
    assert not result.ok and not bought
    assert "signed-in" in result.error


def test_a_screenshot_is_taken_at_the_login_decision_point():
    """The email screen is where the selectors get identified.

    Walmart may show a password field, a "use a code instead" link, or a
    Continue button to a second screen — and which one decides the whole
    config. A failure here previously produced no image of that page.
    """
    shots = []

    class ShotPage(FakePage):
        def screenshot(self, path):
            shots.append(Path(path).name)

    page = ShotPage(fail_on="#password")   # the two-step-login failure
    result = add_phone_to_account(_flow(page), ACCOUNT, lambda: (_purchase(), []))
    assert not result.ok
    assert "walmart-login-email-entered.png" in shots, shots


# --- sign-in via emailed code ---------------------------------------------


def test_emailed_code_is_preferred_over_the_password():
    page = FakePage()
    flow = _flow(page, login_use_code="#sendcode",
                 login_code_input="#logincode", login_code_submit="#loginverify")
    result = add_phone_to_account(
        flow, ACCOUNT, lambda: (_purchase(), []), fetch_email_code=lambda: "998877"
    )
    assert result.ok, result.error
    assert result.login_method == "code"
    assert page.filled["#logincode"] == "998877"
    assert "#password" not in page.filled, "typed the password despite the code option"
    assert page.clicked[0] == "#sendcode"


def test_password_is_used_when_no_code_option_is_configured():
    page = FakePage()
    result = add_phone_to_account(_flow(page), ACCOUNT, lambda: (_purchase(), []))
    assert result.login_method == "password"
    assert page.filled["#password"] == "pw"


def test_code_option_is_skipped_when_no_mailbox_reader_is_supplied():
    # Configured to use a code but nothing can read the mailbox: fall back
    # rather than hanging on a code that will never be fetched.
    page = FakePage()
    flow = _flow(page, login_use_code="#sendcode")
    result = add_phone_to_account(flow, ACCOUNT, lambda: (_purchase(), []))
    assert result.login_method == "password"


def test_no_signin_code_arriving_is_explained():
    flow = _flow(FakePage(), login_use_code="#sendcode")
    result = add_phone_to_account(
        flow, ACCOUNT, lambda: (_purchase(), []), fetch_email_code=lambda: ""
    )
    assert not result.ok
    assert "walmart.imap" in result.error


def test_a_mailbox_failure_does_not_spend_money():
    bought = []

    def acquire():
        bought.append(1)
        return _purchase(), []

    def fetch():
        raise RuntimeError("IMAP login failed")

    flow = _flow(FakePage(), login_use_code="#sendcode")
    result = add_phone_to_account(flow, ACCOUNT, acquire, fetch_email_code=fetch)
    assert not result.ok and not bought
    assert "IMAP login failed" in result.error


def test_account_without_a_password_still_works_via_code():
    # Code-only accounts are legitimate; only the password path needs one.
    page = FakePage()
    flow = _flow(page, login_use_code="#sendcode",
                 login_code_input="#logincode", login_code_submit="#loginverify")
    nopass = WalmartAccount(email="a@example.com", password="")
    result = add_phone_to_account(
        flow, nopass, lambda: (_purchase(), []), fetch_email_code=lambda: "112233"
    )
    assert result.ok, result.error


def test_account_without_a_password_and_no_code_path_is_explained():
    nopass = WalmartAccount(email="a@example.com", password="")
    result = add_phone_to_account(_flow(FakePage()), nopass, lambda: (_purchase(), []))
    assert not result.ok
    assert "login_use_code" in result.error


# --- failures -------------------------------------------------------------


def test_purchase_failure_is_reported_not_raised():
    def acquire():
        raise RuntimeError("no provider delivered a code")

    result = add_phone_to_account(_flow(FakePage()), ACCOUNT, acquire)
    assert not result.ok
    assert "no provider delivered a code" in result.error


def test_a_bought_number_is_still_reported_when_the_form_fails():
    # The money is spent; the result must say which number, or it's lost.
    page = FakePage(fail_on="#code")
    result = add_phone_to_account(_flow(page), ACCOUNT, lambda: (_purchase(), []))
    assert not result.ok
    assert result.phone == "+13055550123" and result.code == "445566"


def test_missing_selector_names_the_key_to_fix():
    flow = _flow(FakePage(), phone_submit="")
    result = add_phone_to_account(flow, ACCOUNT, lambda: (_purchase(), []))
    assert not result.ok
    assert "walmart.selectors.phone_submit" in result.error


def test_success_marker_absent_is_a_failure_when_configured():
    flow = _flow(FakePage(), success_marker="#confirmed")
    result = add_phone_to_account(flow, ACCOUNT, lambda: (_purchase(), []))
    assert not result.ok
    assert "success marker" in result.error


def test_success_marker_present_confirms():
    page = FakePage(present={"#confirmed"})
    flow = _flow(page, success_marker="#confirmed")
    result = add_phone_to_account(flow, ACCOUNT, lambda: (_purchase(), []))
    assert result.ok


def test_unset_success_marker_does_not_hard_fail():
    # Until the real page is mapped, an unset marker must not fail every run.
    result = add_phone_to_account(_flow(FakePage()), ACCOUNT, lambda: (_purchase(), []))
    assert result.ok


# --- optional two-step login ----------------------------------------------


def test_login_continue_is_clicked_when_configured():
    page = FakePage()
    flow = _flow(page, login_continue="#next")
    add_phone_to_account(flow, ACCOUNT, lambda: (_purchase(), []))
    assert page.clicked[:2] == ["#next", "#signin"]


def test_login_continue_is_skipped_when_blank():
    page = FakePage()
    add_phone_to_account(_flow(page), ACCOUNT, lambda: (_purchase(), []))
    assert page.clicked[0] == "#signin"


# --- accounts file --------------------------------------------------------


def _accounts(text: str):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "wm.csv"
        path.write_text(text)
        return load_walmart_accounts(path)


def test_colon_is_the_separator():
    accounts = _accounts("a@example.com:pw1\n")
    assert [(a.email, a.password) for a in accounts] == [("a@example.com", "pw1")]


def test_only_the_first_colon_splits():
    # A password may contain colons; they belong to the password, not the split.
    accounts = _accounts("a@example.com:pa:ss:word\n")
    assert accounts[0].password == "pa:ss:word"


def test_a_comma_in_a_password_is_not_a_separator():
    # This is the whole point of preferring colon over comma.
    accounts = _accounts("a@example.com:pass,word,123\n")
    assert accounts[0].password == "pass,word,123"
    assert accounts[0].proxy == ""


def test_comma_form_still_reads_when_there_is_no_colon():
    # Older files written before the switch must keep working.
    accounts = _accounts("email,password\na@example.com,pw1\n")
    assert [(a.email, a.password) for a in accounts] == [("a@example.com", "pw1")]


def test_accounts_skip_blanks_comments_and_headers():
    accounts = _accounts("email:password\n\n# a comment\na@example.com:pw1\n\n")
    assert len(accounts) == 1


def test_bad_lines_are_skipped_not_fatal():
    # One typo in a long pasted list must not lose the other 4,999.
    accounts = _accounts("a@example.com:pw1\nnotanemail:pw\nc@example.com:pw3\n")
    assert [a.email for a in accounts] == ["a@example.com", "c@example.com"]


def test_email_only_lines_are_accepted():
    # Code-based sign-in needs no password, so an email alone is a valid row.
    accounts = _accounts("a@example.com\nb@example.com:pw2\n")
    assert [(a.email, a.password) for a in accounts] == [
        ("a@example.com", ""), ("b@example.com", "pw2")
    ]


def test_a_file_with_nothing_usable_explains_the_format():
    try:
        _accounts("garbage\nmore garbage\n")
        assert False, "should have raised"
    except ValueError as e:
        assert "email:password" in str(e)


def test_missing_accounts_file_points_at_the_example():
    try:
        load_walmart_accounts("/nonexistent/wm.csv")
        assert False, "should have raised"
    except FileNotFoundError as e:
        assert "walmart_accounts.example.csv" in str(e)


def test_login_page_is_dumped_before_the_first_fill():
    """The dump has to survive the email field never appearing.

    That was the real failure: every screenshot in login() came after the fill,
    so a timeout on the email field left no record of the page at all.
    """
    page = FakePage(fail_on="#email")
    flow = _flow(page)
    try:
        flow.login(ACCOUNT)
    except Exception:
        pass
    saved = flow.shot_dir / "walmart-login-page.html"
    assert saved.exists(), "no HTML dump of the login page"
    assert "input" in saved.read_text()


class FakeElement:
    def __init__(self, page, text, attrs=None):
        self.page, self.text, self.attrs = page, text, attrs or {}

    def is_visible(self):
        return True

    def inner_text(self):
        return self.text

    def get_attribute(self, name):
        return self.attrs.get(name)

    def click(self):
        self.page.clicked.append(f"element:{self.text}")


class ChoicePage(FakePage):
    """A code-choice screen offering email, SMS, and a password field."""

    OPTIONS = [
        "Text a one-time passcode to (***) ***-1234",
        "Email a one-time passcode to b***9@gmail.com",
        # The container wrapping the whole screen. Both of these run past the
        # old 70-character truncation, so ranking on the truncated length tied
        # them and picked whichever came first — this one.
        "Choose a sign in method Email me a verification code b***9@gmail.com "
        "or Text me a verification code (***) ***-1234 Continue",
    ]

    def locator(self, selector):
        if selector == WalmartFlow.CLICKABLE_QUERY:
            page = self
            options = [FakeElement(self, t) for t in self.OPTIONS]

            class Multi:
                def all(self):
                    return options

                def count(self):
                    return len(options)

            return Multi()
        return super().locator(selector)


def test_auto_finds_the_emailed_code_option_and_never_the_sms_one():
    """The code is read from a mailbox, so the SMS option is the wrong one.

    On these accounts the phone it would text is the number being added, which
    nobody can receive on yet.
    """
    page = ChoicePage()
    flow = _flow(page, login_use_code="auto",
                 login_code_input="#logincode", login_code_submit="#loginverify")
    result = add_phone_to_account(
        flow, ACCOUNT, lambda: (_purchase(), []), fetch_email_code=lambda: "424242"
    )
    assert result.ok, result.error
    assert result.login_method == "code"
    assert "#password" not in page.filled, "typed the password despite a code option"
    assert page.filled["#logincode"] == "424242"
    # The tightest match, not the wrapper that swallows the heading.
    assert "element:Email a one-time passcode to b***9@gmail.com" in page.clicked


def test_walmarts_request_code_button_is_recognised():
    """The real wording, taken from a live run.

    The screen also shows the password form's "Sign in" submit; pressing that
    with an empty password fails the attempt, so wording has to beat shape.
    """
    class Control:
        def __init__(self, text, page):
            self.text, self.page = text, page

        def is_visible(self):
            return True

        def is_enabled(self):
            return True

        def inner_text(self):
            return self.text

        def get_attribute(self, name):
            return None

        def click(self):
            self.page.clicked.append(self.text)

    class RealChooser(FakePage):
        LABELS = [
            "Change",
            "Choose a sign in method",
            "Email me a verification code b***9@gmail.com",
            "Password",
            "Sign in",            # the password form's submit, same screen
            "Request code",
            "Give feedback",
        ]

        def locator(self, selector):
            if selector == WalmartFlow.CLICKABLE_QUERY:
                page = self
                controls = [Control(t, page) for t in page.LABELS]

                class Multi:
                    def all(self):
                        return controls
                return Multi()
            return super().locator(selector)

    page = RealChooser()
    flow = _flow(page, login_use_code="auto")
    proceed = flow._find_proceed()
    assert proceed is not None, "didn't recognise 'Request code'"
    proceed.click()
    assert page.clicked == ["Request code"], page.clicked


def test_the_send_button_is_pressed_when_choosing_only_ticks_the_option():
    """A radio chooser doesn't send anything on its own.

    Without the follow-up press the run waited out the full mailbox timeout
    for an email nobody had asked for.
    """
    class RadioChooser(ChoicePage):
        """Clicking an option changes nothing; Continue does the sending."""

        url = "https://identity.walmart.com/account/signin/withotpchoice"

        def locator(self, selector):
            if selector in WalmartFlow.PROCEED_SELECTORS:
                page = self

                class Button:
                    def is_visible(self):
                        return True

                    def click(self):
                        page.clicked.append("proceed")
                        type(page).url = "https://identity.walmart.com/account/verifyitsyou"

                    def nth(self, i):
                        return self

                    def count(self):
                        return 1

                return Button()
            return super().locator(selector)

    page = RadioChooser()
    flow = _flow(page, login_use_code="auto",
                 login_code_input="auto", login_code_submit="auto")
    try:
        # What happens after the code arrives is another test's business; this
        # one is only about the press that causes it to be sent.
        with contextlib.suppress(FlowError):
            flow.login(ACCOUNT, fetch_email_code=lambda: "123456")
    finally:
        RadioChooser.url = "https://identity.walmart.com/account/signin/withotpchoice"
    assert "proceed" in page.clicked, "never pressed the button that sends the code"


def test_a_radio_with_no_text_of_its_own_is_found_via_its_label():
    """Radio inputs have no inner text, so matching on it discarded them.

    A radio-list chooser is made entirely of these — the wording lives in the
    label — so none of its options were ever candidates.
    """
    ticked = []

    class Radio:
        def is_visible(self):
            return True

        def inner_text(self):
            return ""

        def get_attribute(self, name):
            return {"aria-label": "Email me a verification code b***9@gmail.com"}.get(name)

        def check(self):
            ticked.append(1)

    class RadioOnlyPage(FakePage):
        def locator(self, selector):
            if selector == WalmartFlow.CLICKABLE_QUERY:
                class Multi:
                    def all(self):
                        return [Radio()]
                return Multi()
            return super().locator(selector)

    flow = _flow(RadioOnlyPage(), login_use_code="auto")
    assert flow.find_code_option() is not None, "the radio was never a candidate"
    flow._activate(flow.find_code_option())
    assert ticked, "used click() on a radio instead of check()"


def test_auto_falls_back_to_the_password_when_no_code_option_exists():
    page = FakePage()      # no clickables at all
    flow = _flow(page, login_use_code="auto")
    result = add_phone_to_account(
        flow, ACCOUNT, lambda: (_purchase(), []), fetch_email_code=lambda: "424242"
    )
    assert result.ok, result.error
    assert result.login_method == "password"


def test_an_explicit_selector_still_wins_over_the_search():
    page = ChoicePage()
    flow = _flow(page, login_use_code="#sendcode",
                 login_code_input="#logincode", login_code_submit="#loginverify")
    result = add_phone_to_account(
        flow, ACCOUNT, lambda: (_purchase(), []), fetch_email_code=lambda: "424242"
    )
    assert result.ok, result.error
    assert result.login_method == "code"
    assert page.clicked.count("#sendcode") == 1


class CodeEntryPage(ChoicePage):
    """The verify-it's-you screen, with a code field of a given shape."""

    def __init__(self, *, field_selector="input[autocomplete='one-time-code']",
                 boxes=0, **kw):
        super().__init__(**kw)
        self.field_selector = field_selector
        self.boxes = boxes
        self.typed: list[str] = []

    def locator(self, selector):
        page = self

        class Field:
            def __init__(self, index=0):
                self.index = index

            def is_visible(self):
                return True

            def fill(self, value):
                page.typed.append(value)

            def click(self):
                page.clicked.append(f"element:{selector}")

            def nth(self, i):
                return Field(i)

            def count(self):
                return page.boxes if selector == "input[maxlength='1']" else 1

        if selector == "input[maxlength='1']":
            return Field()
        if selector in (page.field_selector, *WalmartFlow.CODE_SUBMIT_SELECTORS):
            return Field()
        if selector in WalmartFlow.CODE_INPUT_SELECTORS:
            class Empty:
                def count(self):
                    return 0
            return Empty()
        return super().locator(selector)


def _code_flow(page):
    return _flow(page, login_use_code="auto",
                 login_code_input="auto", login_code_submit="auto")


def test_the_code_field_is_found_when_it_is_not_the_expected_shape():
    """Walmart's verify screen doesn't use autocomplete=one-time-code.

    The shipped selector timed out on it for 30s, and the ids on that page are
    React-generated so there is nothing stable to pin instead.
    """
    page = CodeEntryPage(field_selector="input[inputmode='numeric']")
    result = add_phone_to_account(
        _code_flow(page), ACCOUNT, lambda: (_purchase(), []),
        fetch_email_code=lambda: "123456",
    )
    assert result.ok, result.error
    assert page.typed == ["123456"]


def test_a_segmented_code_field_gets_one_digit_per_box():
    # A single fill would put all six characters in the first box.
    page = CodeEntryPage(boxes=6)
    result = add_phone_to_account(
        _code_flow(page), ACCOUNT, lambda: (_purchase(), []),
        fetch_email_code=lambda: "123456",
    )
    assert result.ok, result.error
    assert page.typed == ["1", "2", "3", "4", "5", "6"]


class StillOnLoginPage(FakePage):
    """Sign-in didn't take: every navigation lands back on the identity host."""

    url = "https://identity.walmart.com/account/login?tp=X"


def test_a_failed_login_is_caught_without_a_configured_marker():
    """Nothing checked before, so a dead session reached the purchase step.

    The giveaway is the phone-form dump coming back as the login page — by
    which point money could already have been spent.
    """
    bought = []
    page = StillOnLoginPage()
    flow = _flow(page)
    result = add_phone_to_account(
        flow, ACCOUNT, lambda: (bought.append(1), (_purchase(), []))[1]
    )
    assert not result.ok
    assert "didn't work" in result.error, result.error
    assert not bought, "spent money on a signed-out session"


def test_the_profile_page_redirecting_to_signin_stops_the_run():
    class RedirectsAfterLogin(FakePage):
        """Login looks fine, but the profile page bounces to sign-in."""

        url = "https://www.walmart.com/account"

        def goto(self, url):
            super().goto(url)
            if "profile" in url:
                type(self).url = "https://identity.walmart.com/account/login"

    bought = []
    page = RedirectsAfterLogin()
    try:
        result = add_phone_to_account(
            _flow(page), ACCOUNT, lambda: (bought.append(1), (_purchase(), []))[1]
        )
    finally:
        RedirectsAfterLogin.url = "https://www.walmart.com/account"
    assert not result.ok
    assert "redirected to sign-in" in result.error, result.error
    assert not bought, "spent money before confirming the form was reachable"


def test_missing_password_field_is_named_rather_than_timing_out():
    """Filling a password field that isn't there burns 30s per account.

    The check asks the page, not the URL — Walmart's code-choice screen
    sometimes carries a password field too, and refusing on the URL alone
    would block a sign-in that works.
    """
    page = FakePage(missing={"#password"})
    flow = _flow(page, login_continue="#next")
    result = add_phone_to_account(flow, ACCOUNT, _never_buys)
    assert not result.ok
    assert "no password field" in result.error, result.error
    assert "#password" not in page.filled


def test_password_is_used_when_the_field_is_actually_there():
    page = FakePage()
    flow = _flow(page, login_continue="#next")
    result = add_phone_to_account(flow, ACCOUNT, lambda: (_purchase(), []))
    assert result.ok, result.error
    assert page.filled["#password"] == "pw"


def test_describe_reports_the_page_state():
    page = FakePage(present={"input[type='password']"})
    flow = _flow(page)
    described = flow.describe()
    assert "url=https://wm/login" in described
    assert "'Sign in'" in described
    assert "password=1" in described


def test_failure_dumps_the_page_and_says_what_was_on_it():
    page = FakePage(fail_on="#email")
    flow = _flow(page)
    result = add_phone_to_account(flow, ACCOUNT, _never_buys)
    assert not result.ok
    assert "url=" in result.error, result.error
    assert (flow.shot_dir / "walmart-error.html").exists()


def _never_buys():
    raise AssertionError("no money may be spent when login failed")


def test_shipped_example_file_parses():
    root = Path(__file__).resolve().parents[1]
    accounts = load_walmart_accounts(root / "walmart_accounts.example.csv")
    assert accounts and all(a.email and a.password for a in accounts)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  ok    {name}")
            except AssertionError as e:
                failures += 1
                print(f"  FAIL  {name}: {e}")
    print(f"\n{'FAILED' if failures else 'All tests passed'} ({failures} failure(s))")
    raise SystemExit(1 if failures else 0)
