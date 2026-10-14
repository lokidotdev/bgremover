"""Request log and the aggregates the admin dashboard shows."""

from __future__ import annotations

from .db import Database

RETENTION_DAYS = 90


class UsageLog:
    def __init__(self, db: Database):
        self._db = db
        self._writes = 0

    def record(self, host: str | None, status: int, duration_ms: int) -> None:
        self._db.execute(
            "INSERT INTO requests (host, status, duration_ms) VALUES (?, ?, ?)", (host, status, duration_ms)
        )
        self._writes += 1
        if self._writes % 1000 == 0:
            self._db.execute(f"DELETE FROM requests WHERE ts < datetime('now', '-{RETENTION_DAYS} days')")

    def summary(self, days: int = 7) -> dict:
        days = max(1, min(days, RETENTION_DAYS))
        since = (f"-{days} days",)
        where = "WHERE ts >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)"
        totals = self._db.query(
            f"""SELECT COUNT(*) AS requests,
                       SUM(status = 200) AS succeeded,
                       SUM(status = 403) AS blocked,
                       SUM(status NOT IN (200, 403)) AS failed,
                       CAST(AVG(CASE WHEN status = 200 THEN duration_ms END) AS INTEGER) AS avg_ms
                FROM requests {where}""",
            since,
        )[0]
        by_host = self._db.query(
            f"""SELECT COALESCE(host, '(no origin)') AS host, COUNT(*) AS requests,
                       SUM(status = 200) AS succeeded, SUM(status = 403) AS blocked,
                       MAX(ts) AS last_seen
                FROM requests {where} GROUP BY host ORDER BY requests DESC LIMIT 50""",
            since,
        )
        by_day = self._db.query(
            f"""SELECT substr(ts, 1, 10) AS day, COUNT(*) AS requests,
                       SUM(status = 200) AS succeeded, SUM(status = 403) AS blocked
                FROM requests {where} GROUP BY day ORDER BY day""",
            since,
        )
        return {
            "days": days,
            "totals": {k: v or 0 for k, v in totals.items()},
            "by_host": by_host,
            "by_day": by_day,
        }
