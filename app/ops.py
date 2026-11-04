#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operations endpoints: /healthz, for the container's HEALTHCHECK and any
orchestrator. Unauthenticated (main's session gate exempts it by name), so it
says nothing beyond whether the server can read its database.
"""

import logging
import sqlite3

from flask import Blueprint, jsonify

import db

_log = logging.getLogger(__name__)

bp = Blueprint("ops", __name__)


@bp.route("/healthz")
def healthz():
    """200 when the app is serving and its database answers, 503 when the
    database doesn't. Cheaper than rendering /login, which the HEALTHCHECK
    used to fetch."""
    try:
        with db.connection() as conn:
            conn.execute("SELECT 1").fetchone()
    except sqlite3.Error as e:
        _log.warning("healthz: the database did not answer: %s", e)
        return jsonify({"status": "unavailable"}), 503
    return jsonify({"status": "ok"})
