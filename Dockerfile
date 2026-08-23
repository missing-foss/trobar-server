# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

# Pinned by digest (not just the tag) so the base image is immutable and
# supply-chain-auditable — OpenSSF Scorecard Pinned-Dependencies (#185). The
# tag is kept for readability and as the target when the digest is refreshed.
# A pinned digest that nothing ever bumps goes stale and silently misses
# base-image security fixes, so it has to be refreshed deliberately rather than
# left alone — a bare digest nobody updates is worse than a tag.
# Re-resolve with: docker inspect --format='{{index .RepoDigests 0}}' python:3.14-slim
FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6

WORKDIR /app

# ffmpeg: server-side MP3 transcoding for /api/device/file/<id> — see
# app/transcode.py. GPLv3-licensed; see THIRD_PARTY_NOTICES.md.
# libchromaprint1 + libchromaprint-tools: audio fingerprinting for #200/#239.
#
# BOTH are needed, and libchromaprint-tools (the fpcalc CLI) is not optional —
# this comment used to say the opposite. pyacoustid's in-process ctypes+audioread
# path is faster and was the reason to skip the CLI, but it decodes inside OUR
# process: when ffmpeg times out on a malformed file, the partial buffer reaches
# native libchromaprint, which fails an assertion
#
#   Assertion 'length % m_num_channels == 0' failed
#
# and assert() calls abort(). That is SIGABRT — it kills the whole server and no
# Python except can catch it. One bad FLAC took production down six times on
# v2.4.0. fingerprint.py/provenance.py therefore pass force_fpcalc=True, which
# moves the decode into a subprocess where an abort kills only the child and
# pyacoustid raises a normal, catchable exception. LGPL-2.1; see
# THIRD_PARTY_NOTICES.md.
# tzdata: so the TZ env var (plumbed in docker-compose.yaml) can actually resolve
# a zone name. It is ALREADY present today, but only as a transitive dependency —
# verified in the built image, where TZ=Europe/Paris correctly reports CEST +0200.
# Declared explicitly because relying on that is fragile: if whatever pulls it in
# ever stops doing so, glibc silently falls back to UTC with no error, and TZ
# becomes a no-op that looks like a bug in the app (#322).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libchromaprint1 \
    libchromaprint-tools tzdata \
    && rm -rf /var/lib/apt/lists/*

# requirements.txt is a compiled, hash-pinned lockfile (every direct +
# transitive package pinned with sha256 hashes, generated from
# requirements.in — see the comment at its top) — OpenSSF Scorecard
# Pinned-Dependencies, #195. --require-hashes enforces that every
# requirement here (and nothing outside it) is hash-verified at install
# time; a bare `pip install -r` would silently accept an unpinned/tampered
# transitive dependency.
COPY app/requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY app/ .
# The About page serves these offline; VERSION is bumped at release.
COPY VERSION LICENSE THIRD_PARTY_NOTICES.md ./
RUN pybabel compile -d translations

# #91: run unprivileged, not as root. Create the data dir owned by that uid so
# a named volume inherits the ownership; bind-mount users must chown their host
# dir to 10001 (documented in the README / SECURITY.md). MUSIC_ROOT is only
# ever read, so it just needs to be readable by 10001.
RUN useradd -u 10001 -r -s /usr/sbin/nologin trobar \
    && mkdir -p /data && chown trobar:trobar /data

ENV PYTHONUNBUFFERED=1
EXPOSE 5000
USER 10001

# #91: let an orchestrator tell a wedged process from a healthy one. /login is
# unauthenticated and always 200s once the app is serving.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/login').status==200 else 1)"

CMD ["python", "main.py"]
