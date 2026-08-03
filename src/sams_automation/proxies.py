"""Load a proxy list and assign a proxy to each account.

A stable ("sticky") IP per account is the default: a given Sam's Club login
always exits through the same proxy, which looks far more natural than the same
account hopping between IPs run to run. Round-robin and random are available if
you'd rather spread load across the pool differently.
"""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Proxy:
    server: str  # e.g. "http://host:port" or "socks5://host:port"
    username: str | None = None
    password: str | None = None

    def to_playwright(self) -> dict:
        d: dict[str, str] = {"server": self.server}
        if self.username:
            d["username"] = self.username
        if self.password is not None:
            d["password"] = self.password
        return d

    @property
    def label(self) -> str:
        """host:port without credentials, safe for logging."""
        return re.sub(r"^\w+://", "", self.server)


def parse_proxy_line(line: str) -> Proxy:
    """Parse one proxy line. Accepts, with or without a scheme:

        host:port
        host:port:username:password
        username:password@host:port
        scheme://username:password@host:port
        scheme://host:port

    If no scheme is present, http is assumed.
    """
    line = line.strip()
    scheme = "http"
    m = re.match(r"^(\w+)://(.*)$", line)
    if m:
        scheme, rest = m.group(1), m.group(2)
    else:
        rest = line

    username: str | None = None
    password: str | None = None

    if "@" in rest:
        creds, hostport = rest.rsplit("@", 1)
        if ":" in creds:
            username, password = creds.split(":", 1)
        else:
            username = creds
    else:
        parts = rest.split(":")
        if len(parts) == 4:
            host, port, username, password = parts
            hostport = f"{host}:{port}"
        else:
            hostport = rest

    return Proxy(server=f"{scheme}://{hostport}", username=username, password=password)


def load_proxies(
    path: str | Path, *, dedupe: bool = True, report: bool = False
) -> list[Proxy]:
    """Read a proxy list (one per line; blank lines and #-comments ignored).

    Built for big lists. A malformed line is skipped rather than raised: in a
    5,000-line file one typo shouldn't take down the run, and a summary of what
    was dropped is more useful than a stack trace on line 3,847.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Proxy file not found: {path}. Create it (see proxies.example.txt) "
            "or set proxies.enabled: false in config.yaml."
        )
    proxies: list[Proxy] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    skipped = 0
    duplicates = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            proxy = parse_proxy_line(line)
        except Exception:
            skipped += 1
            continue
        if not _looks_usable(proxy):
            skipped += 1
            continue
        if dedupe:
            key = (proxy.server, proxy.username, proxy.password)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
        proxies.append(proxy)

    if report:
        parts = [f"{len(proxies)} proxies loaded from {path}"]
        if duplicates:
            parts.append(f"{duplicates} duplicate(s) dropped")
        if skipped:
            parts.append(f"{skipped} unparseable line(s) skipped")
        print("  " + ", ".join(parts))
    return proxies


def _looks_usable(proxy: Proxy) -> bool:
    """Cheap sanity check — a host and a numeric port."""
    hostport = proxy.label
    if ":" not in hostport:
        return False
    host, _, port = hostport.rpartition(":")
    return bool(host) and port.isdigit()


ROTATIONS = ("sticky", "round_robin", "random", "fresh")


class ProxyPool:
    def __init__(self, proxies: list[Proxy], rotation: str = "sticky"):
        if rotation not in ROTATIONS:
            raise ValueError(
                f"Unknown proxy rotation: {rotation}. Known: {', '.join(ROTATIONS)}"
            )
        self.proxies = proxies
        self.rotation = rotation
        self._rr = 0
        # "fresh" hands out a shuffled sequence with no repeats, so a large
        # pool never puts two accounts behind the same IP in one run.
        self._unused: list[Proxy] = []
        self.exhausted = False

    def for_account(self, key: str) -> Proxy | None:
        """Pick the proxy for a given account key (e.g. its email)."""
        if not self.proxies:
            return None
        if self.rotation == "sticky":
            # Deterministic: same key -> same proxy on every run.
            h = int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16)
            return self.proxies[h % len(self.proxies)]
        if self.rotation == "round_robin":
            proxy = self.proxies[self._rr % len(self.proxies)]
            self._rr += 1
            return proxy
        if self.rotation == "random":
            return random.choice(self.proxies)
        if self.rotation == "fresh":
            if not self._unused:
                self._unused = list(self.proxies)
                random.shuffle(self._unused)
                # Only a wrap-around counts as exhausted, not the first fill.
                self.exhausted = self._rr > 0
            self._rr += 1
            return self._unused.pop()
        raise ValueError(f"Unknown proxy rotation: {self.rotation}")
