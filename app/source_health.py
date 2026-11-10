# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""How each place a playlist sync reads from has fared, kept across runs.

A source is the library source itself, a member's own listing on it (a Roon
profile, a Jellyfin, Emby or Navidrome user), a member's linked streaming
account (Tidal, Spotify) or a public link. Every finished sync records each
source it tried, ok or failed; a cancelled one records nothing.

A source **alerts** once it has failed ALERT_RUNS runs in a row and has been
failing for at least ALERT_HOURS: one bad night says nothing, three runs over
half a day do. Its first success clears it. Admins see the alerting ones in
the header and the whole list in Administration; integrations read them from
/api/integrations/playlist-sources.
"""

import logging
import sqlite3

_log = logging.getLogger(__name__)

ALERT_RUNS = 3
ALERT_HOURS = 12

LIBRARY_SOURCE = "library_source"
MEMBER_LISTING = "member_listing"
LINKED_ACCOUNT = "linked_account"
PUBLIC_LINK = "public_link"

# A reason is a provider's short code or an exception's text: kept short, and
# to one line, since it is shown as it is.
_REASON_MAX = 200


def outcome(kind: str, provider: str, ok: bool, reason=None, *, user_id: int | None = None,
            subscription_id: int | None = None) -> dict:
    """One source's result in this run, for record()."""
    key = f"{kind}:{provider}:{user_id if user_id is not None else ''}:{subscription_id or ''}"
    text = None
    if not ok:
        text = " ".join(str(reason or "no reason given").split())[:_REASON_MAX]
    return {"key": key, "kind": kind, "provider": provider, "user_id": user_id,
            "subscription_id": subscription_id, "ok": ok, "reason": text}


def record(conn: sqlite3.Connection, outcomes: list[dict]) -> None:
    """Writes one finished run's outcomes. A source this run didn't try (an
    account unlinked, a provider no longer active) is forgotten: it can no
    longer fail, so it must not go on alerting."""
    seen = set()
    for o in outcomes:
        if o["key"] in seen:
            continue  # one outcome per source per run: the first says it
        seen.add(o["key"])
        if o["ok"]:
            conn.execute(
                "INSERT INTO source_health (key, kind, provider, user_id, subscription_id, "
                "consecutive_failures, failing_since, last_reason, last_success_at, last_seen_at, alerting) "
                "VALUES (?, ?, ?, ?, ?, 0, NULL, NULL, datetime('now'), datetime('now'), 0) "
                "ON CONFLICT(key) DO UPDATE SET consecutive_failures = 0, failing_since = NULL, "
                "last_reason = NULL, last_success_at = datetime('now'), last_seen_at = datetime('now'), "
                "alerting = 0",
                (o["key"], o["kind"], o["provider"], o["user_id"], o["subscription_id"]))
        else:
            conn.execute(
                "INSERT INTO source_health (key, kind, provider, user_id, subscription_id, "
                "consecutive_failures, failing_since, last_reason, last_success_at, last_seen_at, alerting) "
                "VALUES (?, ?, ?, ?, ?, 1, datetime('now'), ?, NULL, datetime('now'), 0) "
                "ON CONFLICT(key) DO UPDATE SET consecutive_failures = consecutive_failures + 1, "
                "failing_since = COALESCE(failing_since, datetime('now')), last_reason = excluded.last_reason, "
                "last_seen_at = datetime('now')",
                (o["key"], o["kind"], o["provider"], o["user_id"], o["subscription_id"], o["reason"]))
    before = {r["key"] for r in conn.execute("SELECT key FROM source_health WHERE alerting = 1")}
    conn.execute(
        "UPDATE source_health SET alerting = (consecutive_failures >= ? "
        "AND failing_since <= datetime('now', ?)) WHERE consecutive_failures > 0",
        (ALERT_RUNS, f"-{ALERT_HOURS} hours"))
    # One log line when a source starts alerting, so the log says it too.
    for row in conn.execute("SELECT key, kind, provider, consecutive_failures, last_reason "
                            "FROM source_health WHERE alerting = 1").fetchall():
        if row["key"] not in before:
            _log.warning("playlist source %s %s keeps failing (%d runs): %s", row["kind"],
                         row["provider"], row["consecutive_failures"], row["last_reason"])
    if seen:
        conn.execute(f"DELETE FROM source_health WHERE key NOT IN ({','.join('?' * len(seen))})",
                     tuple(seen))
    else:
        conn.execute("DELETE FROM source_health")
    conn.commit()


def rows(conn: sqlite3.Connection, alerting_only: bool = False) -> list[dict]:
    """Every recorded source, alerting first, then failing, then the rest,
    with the member's username or the public link's title for display."""
    where = "WHERE h.alerting = 1 " if alerting_only else ""
    result = conn.execute(
        "SELECT h.kind, h.provider, h.consecutive_failures, h.failing_since, h.last_reason, "
        "h.last_success_at, h.alerting, u.username, s.title AS link_title "
        "FROM source_health h LEFT JOIN users u ON u.id = h.user_id "
        "LEFT JOIN playlist_subscriptions s ON s.id = h.subscription_id "
        f"{where}ORDER BY h.alerting DESC, h.consecutive_failures > 0 DESC, h.provider, u.username, h.key"
    ).fetchall()
    return [{
        "kind": r["kind"],
        "provider": r["provider"],
        "account": r["username"] if r["kind"] != PUBLIC_LINK else r["link_title"],
        "runs": r["consecutive_failures"],
        "failing_since": r["failing_since"],
        "reason": r["last_reason"],
        "last_success_at": r["last_success_at"],
        "alerting": bool(r["alerting"]),
    } for r in result]
