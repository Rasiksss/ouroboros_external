"""Email file intake — pick up PDFs emailed to the intake mailbox.

Users on locked-down work machines often can't upload files through the
browser. Instead they email a PDF from their work address to the intake
mailbox (the same beget mailbox that sends access-request notifications,
just read from over IMAP instead of written to over SMTP). This module polls
that mailbox and hands matching PDFs to an ingest callback, which registers
them as the user's documents.

Binding model (MVP, per product decision):
  - The user "arms" intake from chat with their sending address. This opens a
    30-minute window bound to (user_id, from_email).
  - While the window is open, every PDF that arrives from that exact address
    is ingested as that user's document; each arrival is recorded so the UI
    can show a toast. No auto-analysis — the user starts it.
  - The window auto-closes after 30 minutes; the user re-arms to resume.

Matching is by sender ``From`` only — no SPF/DKIM verification yet (spoofing
hardening is a deliberate follow-up). The shared "info" mailbox also receives
human mail, so the poller is careful: it reads with ``BODY.PEEK`` (never sets
``\\Seen`` while inspecting) and only marks a message read once we have
actually ingested its PDF for an armed user. Mail from non-armed senders is
left untouched and stays visible to humans.

State is in-memory: a 30-minute window is ephemeral, and the process runs a
single uvicorn worker, so a dict keyed by user_id is sufficient. Windows are
lost on restart — the user simply re-arms.
"""
from __future__ import annotations

import asyncio
import email
import imaplib
import logging
import re
import time
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.utils import parseaddr
from typing import Awaitable, Callable, Optional

log = logging.getLogger("finengine.email_intake")

# How often the poller checks the mailbox while ≥1 window is open. No mailbox
# connection happens at all when nobody is armed.
POLL_INTERVAL_SEC = 20
# How long a window stays open after the user arms it.
WINDOW_MINUTES = 30
# Defensive cap on unseen messages scanned per poll (flooded shared mailbox).
MAX_SCAN_PER_POLL = 50
# Skip attachments larger than this (avoid OOM from a huge email).
MAX_PDF_BYTES = 60 * 1024 * 1024

# Ingest callback: (user_id, filename, pdf_bytes) -> doc_id.
IngestFn = Callable[[int, str, bytes], Awaitable[str]]


# Basic shape check — same pattern as the access-request form. parseaddr()
# happily returns "garbage" verbatim as the address, so validate before trust.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _norm_email(addr: str) -> str:
    """Extract, normalize and validate the bare address from a raw From value.

    Returns "" when the value isn't a plausible email — callers treat that as
    "no match" (poller) or invalid input (arm).
    """
    em = parseaddr(addr or "")[1].strip().lower()
    return em if _EMAIL_RE.match(em) else ""


def _ascii_email(addr: str) -> str:
    """Punycode-encode the domain so locked-down clients can always send.

    "infoaifa@аифа.рф" -> "infoaifa@xn--80aax2c.xn--p1ai". Returns the input
    unchanged if it isn't an address or can't be encoded.
    """
    addr = (addr or "").strip()
    if "@" not in addr:
        return addr
    local, _, domain = addr.rpartition("@")
    try:
        return f"{local}@{domain.encode('idna').decode('ascii')}"
    except Exception:
        return addr


def _extract_pdfs(msg: email.message.Message, max_bytes: int) -> list[tuple[str, bytes]]:
    """Return [(filename, bytes), ...] for every PDF attachment in the message."""
    out: list[tuple[str, bytes]] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = part.get_filename()
        if filename:
            try:
                filename = str(make_header(decode_header(filename)))
            except Exception:
                pass
        name = filename or ""
        ctype = (part.get_content_type() or "").lower()
        if ctype != "application/pdf" and not name.lower().endswith(".pdf"):
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            payload = None
        if not payload:
            continue
        if len(payload) > max_bytes:
            log.warning("email intake: skipping oversized attachment '%s' (%d bytes)", name, len(payload))
            continue
        out.append((name or "document.pdf", payload))
    return out


@dataclass
class ReceivedFile:
    doc_id: str
    filename: str
    ts: float


@dataclass
class Window:
    user_id: int
    from_email: str          # normalized, lowercase
    expires_at: float
    received: list[ReceivedFile] = field(default_factory=list)

    def active(self, now: Optional[float] = None) -> bool:
        return (now if now is not None else time.time()) < self.expires_at


class EmailIntake:
    """Window store + IMAP poller. One instance, started at app startup.

    All state (``_windows``, ``Window.received``) is touched only from the
    event loop thread — arm/disarm/get run inside request handlers, and the
    poller appends to ``received`` in ``_poll_once`` (also on the loop). The
    blocking IMAP work runs in a threadpool but receives plain-data snapshots
    and never touches shared state, so no lock is needed.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        address: str = "",
        window_minutes: int = WINDOW_MINUTES,
        ingest: IngestFn,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._address = address or user
        self._window_sec = window_minutes * 60
        self._ingest = ingest
        self._windows: dict[int, Window] = {}
        self._task: Optional[asyncio.Task] = None

    # ── public state ──────────────────────────────────────────────────────
    @property
    def configured(self) -> bool:
        return bool(self._host and self._user and self._password)

    @property
    def address(self) -> str:
        return self._address

    @property
    def address_ascii(self) -> str:
        return _ascii_email(self._address)

    def arm(self, user_id: int, from_email: str) -> Window:
        em = _norm_email(from_email)
        if not em:
            raise ValueError("invalid email")
        w = Window(user_id=user_id, from_email=em, expires_at=time.time() + self._window_sec)
        self._windows[user_id] = w
        return w

    def disarm(self, user_id: int) -> None:
        self._windows.pop(user_id, None)

    def get(self, user_id: int) -> Optional[Window]:
        return self._windows.get(user_id)

    # ── poller ────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._task is not None:
            return
        if not self.configured:
            log.info("email intake: IMAP creds not configured — poller disabled")
            return
        self._task = asyncio.create_task(self._run())
        log.info(
            "email intake: poller started (address=%s, every %ds)",
            self._address, POLL_INTERVAL_SEC,
        )

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL_SEC)
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("email intake: poll cycle failed")

    async def _poll_once(self) -> None:
        now = time.time()
        targets = {w.from_email: w for w in self._windows.values() if w.active(now)}
        if not targets:
            return
        loop = asyncio.get_running_loop()
        matches = await loop.run_in_executor(None, self._fetch_matching, set(targets.keys()))
        ingested_uids: list[str] = []
        for uid, from_em, pdfs in matches:
            w = targets.get(from_em)
            if w is None:
                continue
            ingested_any = False
            for fname, data in pdfs:
                try:
                    doc_id = await self._ingest(w.user_id, fname, data)
                    w.received.append(ReceivedFile(doc_id=doc_id, filename=fname, ts=time.time()))
                    ingested_any = True
                    log.info(
                        "email intake: ingested '%s' for user %s as doc %s",
                        fname, w.user_id, doc_id,
                    )
                except Exception:
                    log.exception("email intake: ingest failed for '%s' (user %s)", fname, w.user_id)
            if ingested_any:
                ingested_uids.append(uid)
        if ingested_uids:
            await loop.run_in_executor(None, self._mark_seen, ingested_uids)

    # ── blocking IMAP (run in executor) ───────────────────────────────────
    def _fetch_matching(self, target_emails: set[str]) -> list[tuple[str, str, list[tuple[str, bytes]]]]:
        """Connect, find UNSEEN mail from armed senders, return their PDFs.

        Reads with BODY.PEEK throughout — does not set \\Seen. Returns
        [(uid, from_email, [(filename, bytes), ...]), ...].
        """
        out: list[tuple[str, str, list[tuple[str, bytes]]]] = []
        try:
            m = imaplib.IMAP4_SSL(self._host, self._port)
            m.login(self._user, self._password)
        except Exception:
            log.exception("email intake: IMAP connect/login failed")
            return out
        try:
            m.select("INBOX")
            typ, data = m.uid("search", None, "UNSEEN")
            if typ != "OK" or not data or not data[0]:
                return out
            uids = data[0].split()
            uids = uids[-MAX_SCAN_PER_POLL:]
            uids.reverse()  # newest first
            for raw_uid in uids:
                uid = raw_uid.decode() if isinstance(raw_uid, bytes) else str(raw_uid)
                from_em = self._peek_from(m, uid)
                if not from_em or from_em not in target_emails:
                    continue
                typ, full = m.uid("fetch", uid, "(BODY.PEEK[])")
                if typ != "OK" or not full or not isinstance(full[0], tuple):
                    continue
                try:
                    msg = email.message_from_bytes(full[0][1])
                except Exception:
                    continue
                pdfs = _extract_pdfs(msg, MAX_PDF_BYTES)
                if pdfs:
                    out.append((uid, from_em, pdfs))
            return out
        except Exception:
            log.exception("email intake: fetch failed")
            return out
        finally:
            try:
                m.logout()
            except Exception:
                pass

    @staticmethod
    def _peek_from(m: imaplib.IMAP4_SSL, uid: str) -> str:
        try:
            typ, hdr = m.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
            if typ != "OK" or not hdr or not isinstance(hdr[0], tuple):
                return ""
            raw = hdr[0][1]
            line = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
            line = line.strip()
            if line.lower().startswith("from:"):
                line = line[5:]
            return _norm_email(line)
        except Exception:
            return ""

    def _mark_seen(self, uids: list[str]) -> None:
        if not uids:
            return
        try:
            m = imaplib.IMAP4_SSL(self._host, self._port)
            m.login(self._user, self._password)
        except Exception:
            log.exception("email intake: IMAP connect/login failed (mark seen)")
            return
        try:
            m.select("INBOX")
            for uid in uids:
                try:
                    m.uid("store", uid, "+FLAGS", "(\\Seen)")
                except Exception:
                    log.exception("email intake: failed to mark uid %s seen", uid)
        finally:
            try:
                m.logout()
            except Exception:
                pass
