"""Offline tests for the Walmart add-phone flow.

The page is a fake and the purchase is injected, so the ordering and failure
handling are testable with no browser and no money.

Run with:  python tests/test_walmart_flow.py   (or)   python -m pytest
"""

from __future__ import annotations

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

    def __init__(self, *, present: set[str] | None = None, fail_on: str | None = None):
        self.filled: dict[str, str] = {}
        self.clicked: list[str] = []
        self.visited: list[str] = []
        self.present = present or set()
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
        return FakeLocator(1 if selector in self.present else 0)

    def screenshot(self, path: str) -> None:
        pass

    def set_default_timeout(self, ms: int) -> None:
        pass


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


def test_accounts_load_from_csv():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "wm.csv"
        path.write_text(
            "email,password,proxy,notes\n"
            "a@example.com,pw1,1.2.3.4:8080,first\n"
            "\n"                                   # blank padding row
            "b@example.com,pw2,,\n"
        )
        accounts = load_walmart_accounts(path)
    assert [a.email for a in accounts] == ["a@example.com", "b@example.com"]
    assert accounts[0].proxy == "1.2.3.4:8080"
    assert accounts[1].proxy == ""


def test_accounts_missing_column_is_explained():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "wm.csv"
        path.write_text("email,notes\na@example.com,x\n")
        try:
            load_walmart_accounts(path)
            assert False, "should have raised"
        except ValueError as e:
            assert "password" in str(e)


def test_account_without_a_password_is_rejected():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "wm.csv"
        path.write_text("email,password\na@example.com,\n")
        try:
            load_walmart_accounts(path)
            assert False, "should have raised"
        except ValueError as e:
            assert "a@example.com" in str(e)


def test_missing_accounts_file_points_at_the_example():
    try:
        load_walmart_accounts("/nonexistent/wm.csv")
        assert False, "should have raised"
    except FileNotFoundError as e:
        assert "walmart_accounts.example.csv" in str(e)


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
