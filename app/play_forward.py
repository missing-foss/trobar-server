#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sending a person's stored plays on to their Maloja, ListenBrainz or Last.fm.

Optional and per person, and only while they keep plays: each target has its own
switch. One row per person and target holds the last play sent, so a play goes
once, a failed send is tried again from there when more plays arrive, and turning
a target on starts at the newest play, so old history is never replayed. Plays
go oldest first. A play a service rejects is passed over and counted.
"""

import logging

import requests

import credential_store
import db
import device_plays
import jobs
import lastfm
import listenbrainz
import url_guard

_log = logging.getLogger(__name__)

JOB_TYPE = "play_forward"
APP_SECRET_CONFIG = "lastfm_app_secret_enc"
TARGETS = ("maloja", "listenbrainz", "lastfm")
_BATCH = 100
_LASTFM_BATCH = 50  # its limit per request
_LASTFM_CREDENTIAL_ERRORS = (4, 9, 10, 13, 14, 26)
_LASTFM_LATER_ERRORS = (8, 11, 16, 29)
_TIMEOUT_S = 15

ERROR_REFUSED = "refused"
ERROR_UNREACHABLE = "unreachable"
_RETRY_LATER = (408, 425, 429)


class _Rejected(Exception):
    """The service refused what was sent, not the sender: sending it again
    gets the same answer."""


class _SendFailed(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def lastfm_app(conn) -> dict | None:
    """The install's Last.fm API account, set by an admin: {"key", "secret"},
    or None until both are there and readable."""
    key = db.get_config(conn, "lastfm_app_api_key")
    secret = credential_store.decrypt(db.get_config(conn, APP_SECRET_CONFIG), 0, APP_SECRET_CONFIG)
    return {"key": key, "secret": secret} if key and secret else None


def _credentials(conn, user_id: int) -> dict:
    """target -> what sending to it takes, or None when something is missing."""
    row = conn.execute(
        "SELECT maloja_url, listenbrainz_username, maloja_api_key_enc, listenbrainz_token_enc, "
        "lastfm_session_enc FROM users WHERE id = ?", (user_id,)).fetchone()
    app = lastfm_app(conn)
    session = credential_store.decrypt(row["lastfm_session_enc"], user_id, "lastfm_session")
    key = credential_store.decrypt(row["maloja_api_key_enc"], user_id, "maloja_api_key")
    token = credential_store.decrypt(row["listenbrainz_token_enc"], user_id, "listenbrainz_token")
    return {"maloja": {"base": row["maloja_url"], "key": key} if row["maloja_url"] and key else None,
            "listenbrainz": {"token": token} if row["listenbrainz_username"] and token else None,
            "lastfm": {**app, "session": session} if app and session else None}


def _rows(conn, user_id: int) -> dict:
    return {r["target"]: r for r in conn.execute(
        "SELECT target, enabled, last_play_id, last_error, rejected FROM play_sends WHERE user_id = ?",
        (user_id,))}


def status(conn, user_id: int) -> dict:
    """target -> {"on", "ready", "error", "rejected"}, for the profile. No
    credentials."""
    creds, rows = _credentials(conn, user_id), _rows(conn, user_id)
    out = {}
    for target in TARGETS:
        row = rows.get(target)
        out[target] = {"on": bool(row and row["enabled"]), "ready": creds[target] is not None,
                       "error": row["last_error"] if row else None,
                       "rejected": row["rejected"] if row else 0}
    return out


def set_enabled(conn, user_id: int, target: str, enabled: bool, commit: bool = True) -> None:
    """Turns a target on from the newest stored play, or off."""
    newest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM device_plays").fetchone()[0]
    conn.execute(
        "INSERT INTO play_sends (user_id, target, enabled, last_play_id) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (user_id, target) DO UPDATE SET enabled = excluded.enabled, last_error = NULL, rejected = 0, "
        "last_play_id = CASE WHEN excluded.enabled = 1 AND play_sends.enabled = 0 THEN excluded.last_play_id "
        "ELSE play_sends.last_play_id END",
        (user_id, target, 1 if enabled else 0, newest))
    if commit:
        conn.commit()


def enqueue_for(conn, user_id: int) -> None:
    """Queues sending for this person if any target is on. Commits, if so."""
    if any(r["enabled"] for r in _rows(conn, user_id).values()):
        jobs.enqueue(conn, JOB_TYPE, {"user_id": user_id}, dedupe_key=f"{JOB_TYPE}:{user_id}")


def _still_on(conn, user_id: int, target: str) -> bool:
    """Whether this person still keeps plays and has this target on, read
    afresh: a send stops soon after they say stop."""
    row = conn.execute("SELECT enabled FROM play_sends WHERE user_id = ? AND target = ?",
                       (user_id, target)).fetchone()
    return bool(row and row["enabled"]) and device_plays.is_enabled(conn, user_id)


def _maloja_outcome(status_code: int) -> str:
    """What a Maloja answer means for the play sent. 409: it already has it."""
    if status_code in (401, 403):
        raise _SendFailed(ERROR_REFUSED)
    if status_code == 409 or 200 <= status_code < 300:
        return "taken"
    if status_code in _RETRY_LATER or status_code >= 500:
        raise _SendFailed(ERROR_UNREACHABLE)
    return "rejected"


def _send_maloja(conn, user_id: int, cred: dict, plays: list) -> tuple[int, int, str | None]:
    """One request per play, the switches checked before each. Returns how many
    were dealt with, how many of those it rejected, and any error that stopped
    it. A rejected play is passed over: it would only be rejected again."""
    done = rejected = 0
    url = cred["base"].rstrip("/") + "/apis/mlj_1/newscrobble"
    try:
        with url_guard.session() as session:
            for play in plays:
                if not _still_on(conn, user_id, "maloja"):
                    break
                resp = session.post(url, timeout=_TIMEOUT_S, json={
                    "key": cred["key"], "title": play["title"], "artist": play["artist"],
                    "album": play["album"], "time": play["played_unix"]})
                if _maloja_outcome(resp.status_code) == "rejected":
                    rejected += 1
                done += 1
    except _SendFailed as e:
        return done, rejected, e.code
    except (requests.RequestException, url_guard.BlockedAddress) as e:
        _log.warning("Maloja didn't take a play: %s", type(e).__name__)
        return done, rejected, ERROR_UNREACHABLE
    return done, rejected, None


def _post_listens(base: str, cred: dict, plays: list) -> None:
    """Raises _Rejected for a 4xx naming the listens, _SendFailed otherwise."""
    listens = [{"listened_at": p["played_unix"], "track_metadata": {
        "artist_name": p["artist"], "track_name": p["title"],
        **({"release_name": p["album"]} if p["album"] else {})}} for p in plays]
    try:
        resp = requests.post(base.rstrip("/") + "/1/submit-listens", timeout=_TIMEOUT_S,
                             headers={"Authorization": f"Token {cred['token']}"},
                             json={"listen_type": "import", "payload": listens})
    except requests.RequestException as e:
        _log.warning("ListenBrainz didn't take plays: %s", type(e).__name__)
        raise _SendFailed(ERROR_UNREACHABLE) from e
    if resp.status_code in (401, 403):
        raise _SendFailed(ERROR_REFUSED)
    if resp.status_code in _RETRY_LATER or resp.status_code >= 500:
        raise _SendFailed(ERROR_UNREACHABLE)
    if not resp.ok:
        raise _Rejected()


def _send_listenbrainz(conn, user_id: int, cred: dict, plays: list) -> tuple[int, int, str | None]:
    """The batch in one request. If it is rejected, one play at a time, so one
    bad play costs only itself. Returns as _send_maloja does."""
    base = db.get_config(conn, "listenbrainz_api_base") or listenbrainz.API_BASE
    try:
        try:
            _post_listens(base, cred, plays)
            return len(plays), 0, None
        except _Rejected:
            pass
    except _SendFailed as e:
        return 0, 0, e.code
    done = rejected = 0
    for play in plays:
        if not _still_on(conn, user_id, "listenbrainz"):
            break
        try:
            _post_listens(base, cred, [play])
        except _Rejected:
            rejected += 1
        except _SendFailed as e:
            return done, rejected, e.code
        done += 1
    return done, rejected, None


def _post_scrobbles(conn, cred: dict, plays: list) -> int:
    """One signed request for up to _LASTFM_BATCH plays. Returns how many it
    ignored; raises _Rejected or _SendFailed as _post_listens does."""
    params: dict = {"method": "track.scrobble", "api_key": cred["key"], "sk": cred["session"]}
    for i, play in enumerate(plays):
        params[f"artist[{i}]"] = play["artist"]
        params[f"track[{i}]"] = play["title"]
        params[f"timestamp[{i}]"] = play["played_unix"]
        if play["album"]:
            params[f"album[{i}]"] = play["album"]
    body = {**params, "api_sig": lastfm.api_signature(params, cred["secret"]), "format": "json"}
    try:
        resp = requests.post(db.get_config(conn, "lastfm_api_base") or lastfm.API_BASE, data=body, timeout=_TIMEOUT_S)
        answer = resp.json()
    except requests.RequestException as e:
        _log.warning("Last.fm didn't take plays: %s", type(e).__name__)
        raise _SendFailed(ERROR_UNREACHABLE) from e
    except ValueError as e:
        raise _SendFailed(ERROR_UNREACHABLE) from e
    error = answer.get("error") if isinstance(answer, dict) else None
    if error in _LASTFM_CREDENTIAL_ERRORS:
        raise _SendFailed(ERROR_REFUSED)
    if error in _LASTFM_LATER_ERRORS or resp.status_code >= 500 or resp.status_code in _RETRY_LATER:
        raise _SendFailed(ERROR_UNREACHABLE)
    if error is not None or not resp.ok or not isinstance(answer, dict):
        raise _Rejected()
    attr = (answer.get("scrobbles") or {}).get("@attr") or {}
    return int(attr.get("ignored", 0))


def _send_lastfm(conn, user_id: int, cred: dict, plays: list) -> tuple[int, int, str | None]:
    """In chunks of its limit, the switches checked before each. A rejected
    chunk goes one play at a time. Returns as _send_maloja does."""
    done = rejected = 0
    for start in range(0, len(plays), _LASTFM_BATCH):
        if not _still_on(conn, user_id, "lastfm"):
            break
        chunk = plays[start:start + _LASTFM_BATCH]
        try:
            try:
                rejected += _post_scrobbles(conn, cred, chunk)
                done += len(chunk)
                continue
            except _Rejected:
                pass
            for play in chunk:
                if not _still_on(conn, user_id, "lastfm"):
                    return done, rejected, None
                try:
                    rejected += _post_scrobbles(conn, cred, [play])
                except _Rejected:
                    rejected += 1
                done += 1
        except _SendFailed as e:
            return done, rejected, e.code
    return done, rejected, None


_SENDERS = {"maloja": _send_maloja, "listenbrainz": _send_listenbrainz, "lastfm": _send_lastfm}


def _forward(conn, user_id: int, target: str, cred: dict, after: int) -> None:
    """Sends everything after play `after`, a batch at a time, recording how
    far it got, so what went is never sent again. Stops if the person turns
    this target or keeping plays off. Commits."""
    while _still_on(conn, user_id, target):
        plays = conn.execute(
            "SELECT id, artist, title, album, CAST(strftime('%s', played_at) AS INTEGER) AS played_unix "
            "FROM device_plays WHERE user_id = ? AND id > ? AND COALESCE(artist, '') != '' "
            "AND COALESCE(title, '') != '' ORDER BY id LIMIT ?", (user_id, after, _BATCH)).fetchall()
        if not plays:
            with db.write(conn):
                conn.execute("UPDATE play_sends SET last_error = NULL WHERE user_id = ? AND target = ?",
                             (user_id, target))
            return
        done, rejected, error = _SENDERS[target](conn, user_id, cred, plays)
        if done:
            after = plays[done - 1]["id"]
        with db.write(conn):
            conn.execute("UPDATE play_sends SET last_play_id = ?, rejected = rejected + ?, last_error = ? "
                         "WHERE user_id = ? AND target = ?", (after, rejected, error, user_id, target))
        if error:
            return


def run_job(payload: dict, report) -> dict:
    """Job handler: one person's targets that are on, ready, and while they
    keep plays."""
    user_id = int(payload["user_id"])
    with db.connection() as conn:
        if not device_plays.is_enabled(conn, user_id):
            return {"sent_to": []}
        creds = _credentials(conn, user_id)
        sent_to = []
        for target, row in _rows(conn, user_id).items():
            if row["enabled"] and creds.get(target) is not None:
                _forward(conn, user_id, target, creds[target], row["last_play_id"])
                sent_to.append(target)
        return {"sent_to": sent_to}
