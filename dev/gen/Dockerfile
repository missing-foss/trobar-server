# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

# One-shot seeder: generates the fake test library into the /music volume, then
# exits. python:3.12-slim + ffmpeg. testlib.json is mounted at /testlib.json.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg flac && rm -rf /var/lib/apt/lists/*
COPY generate.py /generate.py
CMD ["python3", "/generate.py"]
