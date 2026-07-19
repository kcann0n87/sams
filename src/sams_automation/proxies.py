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


def load_proxies(path: str | Path) -> list[Proxy]:
    """Read proxies.txt (one per line; blank lines and #-comments ignored)."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Proxy file not found: {path}. Create it (see proxies.example.txt) "
            "or set proxies.enabled: false in config.yaml."
        )
    proxies: list[Proxy] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        proxies.append(parse_proxy_line(line))
    return proxies


class ProxyPool:
    def __init__(self, proxies: list[Proxy], rotation: str = "sticky"):
        self.proxies = proxies
        self.rotation = rotation
        self._rr = 0

    def for_account(self, key: str) -> Proxy | None:
        """Pick the proxy for a given account key (e.g. its primary email)."""
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
        raise ValueError(f"Unknown proxy rotation: {self.rotation}")
