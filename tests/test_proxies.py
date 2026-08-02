"""Offline tests for proxy parsing and assignment.

Run with:  python tests/test_proxies.py   (or)   python -m pytest
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sams_automation.proxies import (  # noqa: E402
    Proxy,
    ProxyPool,
    load_proxies,
    parse_proxy_line,
)


def test_host_port():
    p = parse_proxy_line("1.2.3.4:8080")
    assert p.server == "http://1.2.3.4:8080"
    assert p.username is None and p.password is None


def test_host_port_user_pass():
    p = parse_proxy_line("1.2.3.4:8080:bob:secret")
    assert p.server == "http://1.2.3.4:8080"
    assert p.username == "bob" and p.password == "secret"


def test_at_form():
    p = parse_proxy_line("bob:secret@1.2.3.4:8080")
    assert p.server == "http://1.2.3.4:8080"
    assert p.username == "bob" and p.password == "secret"


def test_scheme_at_form():
    p = parse_proxy_line("http://bob:secret@1.2.3.4:3128")
    assert p.server == "http://1.2.3.4:3128"
    assert p.username == "bob" and p.password == "secret"


def test_socks5_no_creds():
    p = parse_proxy_line("socks5://1.2.3.4:1080")
    assert p.server == "socks5://1.2.3.4:1080"
    assert p.username is None


def test_to_playwright():
    p = parse_proxy_line("1.2.3.4:8080:bob:secret")
    assert p.to_playwright() == {
        "server": "http://1.2.3.4:8080",
        "username": "bob",
        "password": "secret",
    }


def test_label_hides_scheme_and_creds():
    # label is for logging: no scheme, and credentials never appear
    p = parse_proxy_line("http://bob:secret@1.2.3.4:3128")
    assert p.label == "1.2.3.4:3128"
    assert "secret" not in p.label


def test_sticky_is_deterministic():
    proxies = [Proxy("http://a:1"), Proxy("http://b:2"), Proxy("http://c:3")]
    pool = ProxyPool(proxies, "sticky")
    first = pool.for_account("member1@example.com")
    again = pool.for_account("member1@example.com")
    assert first is again  # same account -> same proxy every time


def test_sticky_spreads_accounts():
    proxies = [Proxy("http://a:1"), Proxy("http://b:2"), Proxy("http://c:3")]
    pool = ProxyPool(proxies, "sticky")
    picked = {pool.for_account(f"m{i}@x.com").server for i in range(30)}
    assert len(picked) > 1  # not everyone lands on the same proxy


def test_round_robin_cycles():
    proxies = [Proxy("http://a:1"), Proxy("http://b:2")]
    pool = ProxyPool(proxies, "round_robin")
    got = [pool.for_account("x").server for _ in range(4)]
    assert got == ["http://a:1", "http://b:2", "http://a:1", "http://b:2"]


def test_empty_pool_returns_none():
    assert ProxyPool([], "sticky").for_account("x") is None


def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return 1 if failed else 0


def test_fresh_rotation_never_repeats_until_the_pool_wraps():
    pool = ProxyPool([Proxy(server=f"http://10.0.0.{i}:8080") for i in range(5)], "fresh")
    picked = [pool.for_account(f"a{i}@x.com").server for i in range(5)]
    assert len(set(picked)) == 5, "an IP was reused before the pool ran out"
    assert not pool.exhausted
    pool.for_account("a6@x.com")
    assert pool.exhausted, "wrap-around should be reported"


def test_fresh_rotation_ignores_the_account_key():
    # Unlike sticky, the same account must not keep getting the same IP.
    pool = ProxyPool([Proxy(server=f"http://10.0.0.{i}:8080") for i in range(50)], "fresh")
    picked = {pool.for_account("same@x.com").server for _ in range(10)}
    assert len(picked) == 10


def test_unknown_rotation_is_rejected_at_construction():
    try:
        ProxyPool([Proxy(server="http://1.2.3.4:8080")], "nonsense")
        assert False, "should have raised"
    except ValueError as e:
        assert "fresh" in str(e)


def test_load_skips_bad_lines_and_dedupes():
    import tempfile
    from pathlib import Path as _P

    with tempfile.TemporaryDirectory() as d:
        path = _P(d) / "p.txt"
        path.write_text(
            "# comment\n"
            "1.2.3.4:8080\n"
            "1.2.3.4:8080\n"          # exact duplicate
            "garbage-with-no-port\n"  # unparseable
            "\n"
            "5.6.7.8:3128:user:pass\n"
        )
        proxies = load_proxies(path)
    assert [p.label for p in proxies] == ["1.2.3.4:8080", "5.6.7.8:3128"]


def test_load_handles_a_large_list():
    import tempfile
    from pathlib import Path as _P

    with tempfile.TemporaryDirectory() as d:
        path = _P(d) / "p.txt"
        path.write_text("\n".join(f"10.{i // 256}.{i % 256}.1:8080" for i in range(5000)))
        proxies = load_proxies(path)
    assert len(proxies) == 5000
    pool = ProxyPool(proxies, "fresh")
    assert len({pool.for_account(f"a{i}@x.com").server for i in range(100)}) == 100



if __name__ == "__main__":
    raise SystemExit(_run_all())
