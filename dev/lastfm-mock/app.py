#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mock Last.fm API for the dev environment, also reused by the
e2e suite (#281) so the Most Played chart has real data to render during
the accessibility scan instead of only ever exercising its empty-state hint.

Answers the four methods Trobar's lastfm.py actually calls — user.getInfo,
user.getTopAlbums, artist.getSimilar, user.getRecentTracks — with canned JSON
derived from testlib.json, so Suggestions, the similar-artists strip (#48),
auto-fit ranking (#45), and the Most Played chart (#267) all work with no
real Last.fm account or key. The api_key is ignored. Point Trobar at it with
LASTFM_API_BASE=http://<host>:<port>/2.0/.

TESTLIB_PATH/PORT are configurable (default /testlib.json, 8080 — dev/'s own
docker-compose values, unchanged) so this same script also runs directly
(no Docker) against dev/testlib.json's real repo-relative path, which is
what the e2e harness does — see e2e/README.md."""
import json
import os
import pathlib

from flask import Flask, jsonify, request

app = Flask(__name__)
LIB = json.loads(pathlib.Path(os.environ.get("TESTLIB_PATH", "/testlib.json")).read_text())

# Flatten albums across artists, ranked by playcount (desc) — the shape
# user.getTopAlbums returns.
_ALBUMS = sorted(
    ({"artist": a["name"], **al} for a in LIB["artists"] for al in a["albums"]),
    key=lambda x: x.get("playcount", 0), reverse=True,
)
_SIMILAR = {a["name"].lower(): a.get("similar", []) for a in LIB["artists"]}


@app.route("/2.0/")
def api():
    method = request.args.get("method", "")

    if method == "user.getInfo":
        return jsonify({"user": {"name": request.args.get("user", LIB["user"]["name"]),
                                 "playcount": str(LIB["user"]["playcount"])}})

    if method == "user.getTopAlbums":
        try:
            limit = int(request.args.get("limit", 50))
        except ValueError:
            limit = 50
        albums = [{
            "name": al["title"],
            "artist": {"name": al["artist"]},
            "playcount": str(al.get("playcount", 0)),
            "image": [],
        } for al in _ALBUMS[:limit]]
        return jsonify({"topalbums": {"album": albums}})

    if method == "artist.getSimilar":
        artist = request.args.get("artist", "")
        names = _SIMILAR.get(artist.lower(), [])
        return jsonify({"similarartists": {"artist": [{"name": n} for n in names]}})

    if method == "user.getRecentTracks":
        # A handful of tracks pulled from the top albums.
        tracks = []
        for al in _ALBUMS:
            for t in al["tracks"]:
                tracks.append({"name": t,
                               "artist": {"#text": al["artist"]},
                               "album": {"#text": al["title"]}})
        try:
            limit = int(request.args.get("limit", 50))
        except ValueError:
            limit = 50
        return jsonify({"recenttracks": {"track": tracks[:limit]}})

    return jsonify({"error": 6, "message": f"mock: unhandled method {method!r}"}), 200


@app.route("/healthz")
def healthz():
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
