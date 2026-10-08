#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate a fake but real-file music library for the dev environment.

Reads the manifest at $TESTLIB_PATH (default /testlib.json) and writes, under
/music, one tagged FLAC per track: a silent stream with ARTIST/ALBUM/TITLE/
TRACKNUMBER/DATE (and GENRE, when the album has one) Vorbis comments and an
embedded cover. Silent + invented names = zero copyright. Real FLAC files with
real tags + art so the scanner, cover extraction/caching (#62) and the
Subsonic/Jellyfin providers all have something genuine to index. Idempotent:
skips files that already exist.

Optional manifest keys, used by dev/testlib-showcase.json and absent from
dev/testlib.json (whose output is unchanged):
- "cover_style": "designed" draws a cover with a gradient, a shape and the
  album's title and artist, from a per-album seed. The default is a solid
  colour per album.
- "track_seconds": [min, max] gives each track a length in that range, from a
  per-track seed. The default is 2 seconds.
- an album's "format": "mp3" writes VBR MP3 with ID3v2 tags and the cover
  attached, instead of FLAC.
- "artist_images": true writes artist.png in each artist's folder (a gradient
  and the artist's initials), which the Filesystem provider serves as the
  artist's picture.
- "playlists": [{"name", "tracks": [{"artist", "album", "track"}]}] writes one
  .m3u per playlist at the top of /music, with paths relative to it. The
  Filesystem provider titles a playlist by its path, so there it reads as the
  bare name.
"""
import colorsys
import hashlib
import json
import os
import pathlib
import subprocess
import sys

MUSIC = pathlib.Path("/music")
lib = json.loads(pathlib.Path(os.environ.get("TESTLIB_PATH", "/testlib.json")).read_text())

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_SERIF = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"
COVER = 600  # px, square


def album_seed(artist: str, title: str) -> bytes:
    return hashlib.sha256(f"{artist}/{title}".encode()).digest()


def album_colour(artist: str, title: str) -> str:
    return "0x" + album_seed(artist, title).hex()[:6]  # deterministic per-album hex colour


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _hsv(h: float, s: float, v: float) -> str:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return "0x%02X%02X%02X" % (int(r * 255), int(g * 255), int(b * 255))


def _drawtext(text: str) -> str:
    """A value for drawtext's text option, escaped for the option and the graph."""
    for ch in ("\\", ":", "%"):
        text = text.replace(ch, "\\\\" + ch)
    return text.replace("'", "’").replace(",", "\\,")


def _title_lines(title: str) -> list[str]:
    """One line, or two split at the space nearest the middle when it's long."""
    if len(title) <= 20 or " " not in title:
        return [title]
    mid = len(title) // 2
    cut = min((i for i, c in enumerate(title) if c == " "), key=lambda i: abs(i - mid))
    return [title[:cut], title[cut + 1:]]


def designed_cover(artist: str, title: str, out: pathlib.Path) -> None:
    """A gradient, one of a few shapes, and the title and artist set in type."""
    d = album_seed(artist, title)
    s = COVER
    hue = d[0] / 255
    dark = _hsv(hue, 0.45 + d[2] / 255 * 0.3, 0.28 + d[3] / 255 * 0.22)
    light = _hsv(hue + 0.06 + d[4] / 255 * 0.22, 0.35 + d[5] / 255 * 0.35, 0.62 + d[6] / 255 * 0.28)
    style = d[1] % 5
    shapes = ""
    if style == 0:  # a radial glow
        src = f"gradients=s={s}x{s}:c0={light}:c1={dark}:type=radial:x0={s // 2}:y0={s * 2 // 5}:x1={s // 2 + s * 3 // 5}:y1={s * 2 // 5}"
    elif style == 1:  # a diagonal wash with a ring
        src = f"gradients=s={s}x{s}:c0={dark}:c1={light}:x0=0:y0=0:x1={s}:y1={s}"
        cx, cy, r = 200 + d[7] % 200, 170 + d[8] % 120, 120 + d[9] % 70
        ring = f"lt(abs(hypot(X-{cx},Y-{cy})-{r}),7)"
        shapes = f",format=yuv444p,geq=lum='if({ring},235,lum(X,Y))':cb='if({ring},128,cb(X,Y))':cr='if({ring},128,cr(X,Y))'"
    elif style == 2:  # stripes
        src = f"gradients=s={s}x{s}:c0={dark}:c1={dark}"
        band = s // (6 + d[7] % 10)
        slope = d[8] % 3 - 1
        shapes = f",format=yuv444p,geq=lum='if(lt(mod(X+Y*{slope}+{s},{band}),{band // 2}),min(235,lum(X,Y)*1.35),lum(X,Y))':cb='cb(X,Y)':cr='cr(X,Y)'"
    elif style == 3:  # a spiral sweep
        src = f"gradients=s={s}x{s}:c0={dark}:c1={light}:type=spiral:x0={s // 2}:y0={s // 2}:x1={s}:y1={s // 2}"
    else:  # a horizon with a sun
        src = f"gradients=s={s}x{s}:c0={light}:c1={dark}:x0=0:y0=0:x1=0:y1={s}"
        cy = 240 + d[7] % 80
        sun = f"lt(hypot(X-{s // 2},Y-{cy}),{70 + d[8] % 50})"
        shapes = (f",drawbox=x=0:y={cy + 60}:w={s}:h={s}:color={dark}@0.85:t=fill"
                  f",format=yuv444p,geq=lum='if({sun},225,lum(X,Y))':cb='if({sun},120,cb(X,Y))':cr='if({sun},140,cr(X,Y))'")
    font = FONT_SERIF if d[10] % 2 else FONT_BOLD
    lines = _title_lines(title)
    size = max(26, min(52, int(880 / max(len(x) for x in lines))))
    text = ""
    for i, line in enumerate(lines):
        y = s - 70 - (len(lines) - i) * int(size * 1.15)
        text += f",drawtext=fontfile={font}:text='{_drawtext(line)}':fontcolor=white:fontsize={size}:x=40:y={y}"
    text += f",drawtext=fontfile={FONT}:text='{_drawtext(artist.upper())}':fontcolor=white@0.85:fontsize=22:x=42:y={s - 54}"
    # A faint band under the type, so white text reads on the lightest covers.
    band = f",drawbox=x=0:y={s - 90 - (len(lines) + 1) * int(size * 1.15)}:w={s}:h={s}:color=black@0.3:t=fill"
    graph = src + shapes + ",format=yuv444p" + band + text
    run(["ffmpeg", "-nostdin", "-f", "lavfi", "-i", graph, "-frames:v", "1", "-y", str(out)])


def artist_image(artist: str, out: pathlib.Path) -> None:
    """A soft radial gradient with the artist's initials, centred."""
    d = hashlib.sha256(f"artist/{artist}".encode()).digest()
    s = COVER
    hue = d[0] / 255
    inner = _hsv(hue, 0.35 + d[1] / 255 * 0.3, 0.7 + d[2] / 255 * 0.25)
    outer = _hsv(hue + 0.1, 0.5 + d[3] / 255 * 0.3, 0.25 + d[4] / 255 * 0.2)
    words = [w for w in artist.replace("&", " ").split() if w[:1].isalpha()]
    initials = "".join(w[0].upper() for w in words[:2]) or artist[:1].upper()
    font = FONT_SERIF if d[5] % 2 else FONT_BOLD
    graph = (f"gradients=s={s}x{s}:c0={inner}:c1={outer}:type=radial:x0={s // 2}:y0={s // 2}:x1={s}:y1={s // 2}"
             f",format=yuv444p"
             f",drawtext=fontfile={font}:text='{_drawtext(initials)}':fontcolor=white@0.92:fontsize=220"
             f":x=(w-text_w)/2:y=(h-text_h)/2")
    run(["ffmpeg", "-nostdin", "-f", "lavfi", "-i", graph, "-frames:v", "1", "-y", str(out)])


def track_seconds(artist: str, album: str, title: str) -> int:
    lo, hi = lib.get("track_seconds", [2, 2])
    h = int.from_bytes(hashlib.sha256(f"{artist}/{album}/{title}".encode()).digest()[:4], "big")
    return lo + h % (hi - lo + 1)


def track_path(artist: str, album: dict, i: int) -> pathlib.Path:
    ext = album.get("format", "flac")
    return pathlib.Path(artist) / f"{album['title']} ({album['year']})" / f"{i:02d} - {album['tracks'][i - 1]}.{ext}"


made = 0
for artist in lib["artists"]:
    aname = artist["name"]
    if lib.get("artist_images"):
        (MUSIC / aname).mkdir(parents=True, exist_ok=True)
        picture = MUSIC / aname / "artist.png"
        if not picture.exists():
            artist_image(aname, picture)
    for album in artist["albums"]:
        folder = MUSIC / aname / f"{album['title']} ({album['year']})"
        folder.mkdir(parents=True, exist_ok=True)
        cover = folder / "cover.png"
        if not cover.exists():
            if lib.get("cover_style") == "designed":
                designed_cover(aname, album["title"], cover)
            else:
                run(["ffmpeg", "-nostdin", "-f", "lavfi", "-i",
                     f"color=c={album_colour(aname, album['title'])}:s=500x500",
                     "-frames:v", "1", "-y", str(cover)])
        for i, title in enumerate(album["tracks"], start=1):
            out = MUSIC / track_path(aname, album, i)
            if out.exists():
                continue
            # 1) silent audio + Vorbis-comment tags. (Muxing the cover in the
            # same ffmpeg pass via attached_pic hangs on FLAC — do the picture
            # with metaflac instead, which is fast and reliable.)
            genre = ["-metadata", f"genre={album['genre']}"] if album.get("genre") else []
            tags = [
                "-metadata", f"ARTIST={aname}",
                "-metadata", f"ALBUM={album['title']}",
                "-metadata", f"TITLE={title}",
                "-metadata", f"track={i}",
                "-metadata", f"date={album['year']}",
                *genre,
            ]
            seconds = str(track_seconds(aname, album["title"], title))
            if album.get("format") == "mp3":
                # MP3 takes the cover in the same pass, as an attached picture.
                run(["ffmpeg", "-nostdin",
                     "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-i", str(cover),
                     "-t", seconds, "-map", "0:a", "-map", "1:v",
                     "-c:a", "libmp3lame", "-q:a", "2", "-c:v", "png", "-disposition:v", "attached_pic",
                     "-id3v2_version", "3", *tags, "-y", str(out)])
                made += 1
                continue
            run([
                "ffmpeg", "-nostdin",
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-t", seconds,
                *tags,
                "-y", str(out),
            ])
            # 2) embed the album cover (FLAC PICTURE block; a bare filename
            # defaults to type 3 = front cover in metaflac).
            run(["metaflac", f"--import-picture-from={cover}", str(out)])
            made += 1

albums = {(a["name"], al["title"]): al for a in lib["artists"] for al in a["albums"]}
for playlist in lib.get("playlists", []):
    lines = ["#EXTM3U"]
    for t in playlist["tracks"]:
        album = albums[(t["artist"], t["album"])]
        lines.append(track_path(t["artist"], album, t["track"]).as_posix())
    (MUSIC / f"{playlist['name']}.m3u").write_text("\n".join(lines) + "\n")

print(f"[gen] wrote {made} new track(s) under /music", file=sys.stderr)
