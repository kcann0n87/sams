"""Fetch Sam's Club verification codes/links from a catch-all IMAP mailbox.

The catch-all mailbox receives every verification email regardless of which
secondary address it was sent to, so we identify the right message by matching
the recipient ("To:" / "Delivered-To:") against the secondary email we just
registered, and by only accepting messages that arrived *after* we triggered
the send (so a stale code from a previous run can't be picked up).
"""

from __future__ import annotations

import email
import imaplib
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime

from .config import ImapConfig, VerificationConfig


class VerificationTimeout(Exception):
    """Raised when no matching verification email arrives in time."""


@dataclass
class VerificationResult:
    code: str | None
    link: str | None
    subject: str
    received: datetime


@dataclass
class WatchHit:
    """One verification email seen by the live watcher."""

    uid: str
    to_address: str  # which secondary email it was sent to
    code: str | None
    link: str | None
    subject: str
    received: datetime


class ImapClient:
    def __init__(self, cfg: ImapConfig):
        self.cfg = cfg
        self._conn: imaplib.IMAP4 | None = None

    # -- connection lifecycle -------------------------------------------------

    def connect(self) -> None:
        if self.cfg.ssl:
            self._conn = imaplib.IMAP4_SSL(self.cfg.host, self.cfg.port)
        else:
            self._conn = imaplib.IMAP4(self.cfg.host, self.cfg.port)
        self._conn.login(self.cfg.username, self.cfg.password)
        self._conn.select(self.cfg.mailbox)

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    def __enter__(self) -> "ImapClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- fetching -------------------------------------------------------------

    def wait_for_verification(
        self,
        *,
        to_address: str,
        since: datetime,
        verification: VerificationConfig,
    ) -> VerificationResult:
        """Poll the mailbox until a matching verification email is found.

        Args:
            to_address: the secondary email the code was sent to.
            since: only consider emails received at/after this UTC time.
            verification: mode + regexes describing what to extract.
        """
        assert self._conn is not None, "connect() before waiting"
        deadline = time.monotonic() + self.cfg.timeout_seconds
        code_re = re.compile(verification.code_regex)
        link_re = re.compile(verification.link_regex)
        seen_uids: set[bytes] = set()

        while time.monotonic() < deadline:
            # Re-select to force the server to refresh its view of the mailbox.
            self._conn.select(self.cfg.mailbox)
            for uid, msg in self._recent_candidates(to_address, since, seen_uids):
                seen_uids.add(uid)
                body = _message_text(msg)
                received = _received_time(msg)
                if received < since:
                    continue

                code = None
                link = None
                if verification.mode == "code":
                    m = code_re.search(body) or code_re.search(msg.get("Subject", ""))
                    code = m.group(1) if m else None
                else:  # "link"
                    m = link_re.search(body)
                    link = m.group(1) if m else None

                if (verification.mode == "code" and code) or (
                    verification.mode == "link" and link
                ):
                    return VerificationResult(
                        code=code,
                        link=link,
                        subject=msg.get("Subject", ""),
                        received=received,
                    )
            time.sleep(self.cfg.poll_interval_seconds)

        raise VerificationTimeout(
            f"No verification email for {to_address} arrived within "
            f"{self.cfg.timeout_seconds}s."
        )

    def _recent_candidates(
        self, to_address: str, since: datetime, seen_uids: set[bytes]
    ):
        """Yield (uid, Message) for recent messages plausibly matching."""
        assert self._conn is not None
        # IMAP SINCE is date-granular only, so search from the calendar day of
        # `since` and do exact time filtering in Python.
        since_str = since.strftime("%d-%b-%Y")
        criteria = ["SINCE", since_str]

        # Narrow by sender if configured (OR across the from_contains list).
        from_terms = self.cfg.from_contains
        typ, data = self._conn.search(None, *criteria)
        if typ != "OK" or not data or not data[0]:
            return

        uids = data[0].split()
        # Newest first, and don't refetch what we've already inspected.
        for uid in reversed(uids):
            if uid in seen_uids:
                continue
            typ, msg_data = self._conn.fetch(uid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            if not isinstance(raw, (bytes, bytearray)):
                continue
            msg = email.message_from_bytes(raw)

            if not _to_matches(msg, to_address):
                continue
            if from_terms and not _from_matches(msg, from_terms):
                continue
            yield uid, msg

    def scan_recent(
        self,
        *,
        since: datetime,
        verification: VerificationConfig,
        to_filter: str | None = None,
        skip_uids: set[str] | None = None,
    ) -> list[WatchHit]:
        """Return Sam's Club verification emails received at/after `since`.

        Used by the live watcher. `skip_uids` is mutated: every message this
        examines is added to it so subsequent calls don't re-fetch it.
        """
        assert self._conn is not None, "connect() before scanning"
        skip = skip_uids if skip_uids is not None else set()
        self._conn.select(self.cfg.mailbox)
        code_re = re.compile(verification.code_regex)
        link_re = re.compile(verification.link_regex)

        since_str = since.strftime("%d-%b-%Y")
        typ, data = self._conn.search(None, "SINCE", since_str)
        hits: list[WatchHit] = []
        if typ != "OK" or not data or not data[0]:
            return hits

        for raw_uid in data[0].split():
            uid = raw_uid.decode() if isinstance(raw_uid, bytes) else str(raw_uid)
            if uid in skip:
                continue
            typ, msg_data = self._conn.fetch(raw_uid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            if not isinstance(raw, (bytes, bytearray)):
                continue
            skip.add(uid)  # mark examined so we don't re-fetch next cycle

            msg = email.message_from_bytes(raw)
            if self.cfg.from_contains and not _from_matches(msg, self.cfg.from_contains):
                continue
            received = _received_time(msg)
            if received < since:
                continue
            if to_filter and not _to_matches(msg, to_filter):
                continue

            body = _message_text(msg)
            m = code_re.search(body) or code_re.search(msg.get("Subject", ""))
            code = m.group(1) if m else None
            m2 = link_re.search(body)
            link = m2.group(1) if m2 else None

            hits.append(
                WatchHit(
                    uid=uid,
                    to_address=_primary_recipient(msg),
                    code=code,
                    link=link,
                    subject=msg.get("Subject", ""),
                    received=received,
                )
            )
        hits.sort(key=lambda h: h.received)
        return hits

    def test_connection(self) -> list[str]:
        """Connect, select the mailbox, return available folder names."""
        self.connect()
        try:
            assert self._conn is not None
            typ, boxes = self._conn.list()
            names = []
            if typ == "OK" and boxes:
                for b in boxes:
                    if isinstance(b, bytes):
                        names.append(b.decode(errors="replace"))
            return names
        finally:
            self.close()


# -- helpers ------------------------------------------------------------------


def _addresses(msg: Message, header: str) -> str:
    return " ".join(str(v) for v in msg.get_all(header, [])).lower()


def _hme_alias(msg: Message) -> str:
    """The iCloud Hide My Email alias this message was addressed to, if any.

    Hide My Email adds a header like:
        X-ICLOUD-HME: p=59clawed-trade@icloud.com; d=; f=you@icloud.com; ...
    where `p=` is the alias the sender actually used (the "secondary email"),
    which is the most reliable recipient for our purposes.
    """
    hme = msg.get("X-ICLOUD-HME", "") or ""
    m = re.search(r"p=([^;\s]+@[^;\s]+)", hme)
    return m.group(1) if m else ""


def _primary_recipient(msg: Message) -> str:
    """Best guess at the real recipient (the secondary email) for a catch-all.

    Prefer the Hide My Email alias, then the envelope recipient headers, then To.
    """
    alias = _hme_alias(msg)
    if alias:
        return alias
    headers: list[str] = []
    for h in ("X-Original-To", "Envelope-To", "Delivered-To", "To"):
        headers += [str(v) for v in msg.get_all(h, [])]
    addrs = [addr for _, addr in getaddresses(headers) if addr]
    return addrs[0] if addrs else ""


def _to_matches(msg: Message, to_address: str) -> bool:
    target = to_address.lower()
    if target and target == _hme_alias(msg).lower():
        return True
    for header in (
        "To",
        "Delivered-To",
        "X-Original-To",
        "Cc",
        "Envelope-To",
        "X-ICLOUD-HME",
    ):
        if target in _addresses(msg, header):
            return True
    return False


def _from_matches(msg: Message, from_terms: list[str]) -> bool:
    frm = _addresses(msg, "From")
    return any(term in frm for term in from_terms)


def _received_time(msg: Message) -> datetime:
    raw = msg.get("Date")
    if raw:
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (TypeError, ValueError):
            pass
    return datetime.now(timezone.utc)


def _message_text(msg: Message) -> str:
    """Return a searchable text blob from the message (plain + html)."""
    parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in ("text/plain", "text/html"):
                parts.append(_decode_part(part))
    else:
        parts.append(_decode_part(msg))
    return "\n".join(parts)


def _decode_part(part: Message) -> str:
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        payload = None
    if payload is None:
        content = part.get_payload()
        return content if isinstance(content, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, ValueError):
        return payload.decode("utf-8", errors="replace")
