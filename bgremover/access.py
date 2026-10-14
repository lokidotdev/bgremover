"""Which websites may call the API.

Rules are hostnames: `example.com` matches exactly that host (any scheme/port),
`*.example.com` matches any subdomain (but not the apex). The caller's host is
taken from the `Origin` header, falling back to `Referer`.

Note: browsers set these headers and page scripts cannot forge them, so this
reliably stops other *websites* from using the API. Non-browser clients (curl,
servers) can send any header they like, so it is not a substitute for secret keys.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from urllib.parse import urlsplit

from .db import Database

_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
_HOST = re.compile(rf"^(\*\.)?{_LABEL}(\.{_LABEL})*$")


class InvalidRule(ValueError):
    """The pattern is not a hostname or `*.`-wildcard hostname."""


class DuplicateRule(ValueError):
    """A rule with this pattern already exists."""


class RuleNotFound(LookupError):
    pass


@dataclass(frozen=True)
class Rule:
    id: int
    pattern: str
    note: str
    enabled: bool
    created_at: str


@dataclass(frozen=True)
class Decision:
    allowed: bool
    host: str | None  # the caller's host, if one was sent
    origin: str | None  # value to echo in Access-Control-Allow-Origin when allowed


class DomainPolicy:
    """Owns the allow-list. `check()` is hot-path and served from an in-memory cache."""

    def __init__(self, db: Database):
        self._db = db
        self._lock = threading.Lock()
        self._load()

    # ---- hot path -------------------------------------------------------

    def check(self, origin: str | None, referer: str | None = None) -> Decision:
        source = origin if origin and origin != "null" else referer
        host = _host_of(source) if source else None
        if host is None:
            return Decision(self._allow_no_origin, None, None)
        allowed = host in self._exact or any(host.endswith(s) for s in self._suffixes)
        echo = origin if origin and origin != "null" else _origin_of(source)
        return Decision(allowed, host, echo)

    # ---- admin ----------------------------------------------------------

    def rules(self) -> list[Rule]:
        return [_row(r) for r in self._db.query("SELECT * FROM domains ORDER BY pattern")]

    def add_rule(self, pattern: str, note: str = "") -> Rule:
        pattern = normalize_pattern(pattern)
        try:
            cur = self._db.execute(
                "INSERT INTO domains (pattern, note) VALUES (?, ?)", (pattern, note.strip()[:200])
            )
        except sqlite3.IntegrityError:
            raise DuplicateRule(f"{pattern} is already in the list") from None
        self._load()
        return self._get(cur.lastrowid)

    def update_rule(self, rule_id: int, *, enabled: bool | None = None, note: str | None = None) -> Rule:
        self._get(rule_id)
        if enabled is not None:
            self._db.execute("UPDATE domains SET enabled = ? WHERE id = ?", (int(enabled), rule_id))
        if note is not None:
            self._db.execute("UPDATE domains SET note = ? WHERE id = ?", (note.strip()[:200], rule_id))
        self._load()
        return self._get(rule_id)

    def delete_rule(self, rule_id: int) -> None:
        if self._db.execute("DELETE FROM domains WHERE id = ?", (rule_id,)).rowcount == 0:
            raise RuleNotFound(rule_id)
        self._load()

    @property
    def allow_no_origin(self) -> bool:
        """Whether requests without Origin/Referer (curl, server-to-server) are accepted."""
        return self._allow_no_origin

    def set_allow_no_origin(self, value: bool) -> None:
        self._db.execute(
            "INSERT INTO settings (key, value) VALUES ('allow_no_origin', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            ("1" if value else "0",),
        )
        self._load()

    # ---- internals ------------------------------------------------------

    def _get(self, rule_id: int) -> Rule:
        rows = self._db.query("SELECT * FROM domains WHERE id = ?", (rule_id,))
        if not rows:
            raise RuleNotFound(rule_id)
        return _row(rows[0])

    def _load(self) -> None:
        with self._lock:
            patterns = [r["pattern"] for r in self._db.query("SELECT pattern FROM domains WHERE enabled = 1")]
            self._exact = frozenset(p for p in patterns if not p.startswith("*."))
            self._suffixes = tuple(p[1:] for p in patterns if p.startswith("*."))  # ".example.com"
            setting = self._db.query("SELECT value FROM settings WHERE key = 'allow_no_origin'")
            self._allow_no_origin = bool(setting) and setting[0]["value"] == "1"


def normalize_pattern(raw: str) -> str:
    """Accepts `Example.com`, `https://example.com/path`, `*.example.com`; returns a bare lowercase host."""
    p = raw.strip().lower()
    wildcard = p.startswith("*.")
    if wildcard:
        p = p[2:]
    if "://" in p:
        p = urlsplit(p).hostname or ""
    p = p.split("/")[0].split(":")[0].rstrip(".")
    try:
        p = p.encode("idna").decode("ascii") if p else p
    except UnicodeError:
        raise InvalidRule(f"'{raw}' is not a valid domain") from None
    p = ("*." if wildcard else "") + p
    if not _HOST.match(p):
        raise InvalidRule(f"'{raw}' is not a valid domain; use e.g. example.com or *.example.com")
    return p


def _host_of(value: str) -> str | None:
    try:
        host = urlsplit(value).hostname
    except ValueError:
        return None
    return host.rstrip(".") if host else None


def _origin_of(url: str) -> str | None:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else None


def _row(r: dict) -> Rule:
    return Rule(r["id"], r["pattern"], r["note"], bool(r["enabled"]), r["created_at"])
