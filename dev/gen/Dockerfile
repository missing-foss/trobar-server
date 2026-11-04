# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

# One-shot seeder: generates the fake test library into the /music volume, then
# exits. python:3.12-slim + ffmpeg, and DejaVu for the titles on the showcase
# library's covers. The manifest is mounted at /testlib.json.
FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg flac fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
COPY generate.py /generate.py
CMD ["python3", "/generate.py"]
