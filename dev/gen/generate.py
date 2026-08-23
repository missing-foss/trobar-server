#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate a fake but real-file music library for the dev environment.

Reads /testlib.json and writes, under /music, one tagged FLAC per track — a 2s
silent stream with ARTIST/ALBUM/TITLE/TRACKNUMBER/DATE Vorbis comments and an
embedded cover (a solid colour unique per album). Silent + invented names =
zero copyright. Real FLAC files with real tags + art so the scanner, cover
extraction/caching (#62) and the Subsonic/Jellyfin providers all have something
genuine to index. Idempotent: skips files that already exist.
"""
import hashlib
import json
import pathlib
import subprocess
import sys

MUSIC = pathlib.Path("/music")
lib = json.loads(pathlib.Path("/testlib.json").read_text())


def album_colour(artist: str, title: str) -> str:
    h = hashlib.sha256(f"{artist}/{title}".encode()).hexdigest()
    return "0x" + h[:6]  # deterministic per-album hex colour


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


made = 0
for artist in lib["artists"]:
    aname = artist["name"]
    for album in artist["albums"]:
        folder = MUSIC / aname / f"{album['title']} ({album['year']})"
        folder.mkdir(parents=True, exist_ok=True)
        cover = folder / "cover.png"
        if not cover.exists():
            run(["ffmpeg", "-nostdin", "-f", "lavfi", "-i",
                 f"color=c={album_colour(aname, album['title'])}:s=500x500",
                 "-frames:v", "1", "-y", str(cover)])
        for i, title in enumerate(album["tracks"], start=1):
            out = folder / f"{i:02d} - {title}.flac"
            if out.exists():
                continue
            # 1) silent audio + Vorbis-comment tags. (Muxing the cover in the
            # same ffmpeg pass via attached_pic hangs on FLAC — do the picture
            # with metaflac instead, which is fast and reliable.)
            run([
                "ffmpeg", "-nostdin",
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "2",
                "-metadata", f"ARTIST={aname}",
                "-metadata", f"ALBUM={album['title']}",
                "-metadata", f"TITLE={title}",
                "-metadata", f"track={i}",
                "-metadata", f"date={album['year']}",
                "-y", str(out),
            ])
            # 2) embed the album cover (FLAC PICTURE block; a bare filename
            # defaults to type 3 = front cover in metaflac).
            run(["metaflac", f"--import-picture-from={cover}", str(out)])
            made += 1

print(f"[gen] wrote {made} new track(s) under /music", file=sys.stderr)
