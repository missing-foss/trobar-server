# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Signed cover addresses for devices that can't send a token with an image
request: a Garmin watch's makeImageRequest takes no headers
(trobar-garmin#54). The /api/device/changes answer, which the device fetches
with its token, carries one address per album; the cover route checks the
signature instead of a token.

An address is good for one cover only: one device, one track, one size, until
it expires. The signature is an HMAC-SHA256 under the app's secret key, with
a prefix of its own so it can never stand for anything else the key signs.
No device token is ever put in an address."""

import hashlib
import hmac
import logging
import time

# Never given an address, a signature or a query string: they open a cover.
_log = logging.getLogger(__name__)

# How long an address works. A sync fetches its covers right after the
# changes answer; a failed or expired one gets a fresh address next sync.
TTL_SECONDS = 6 * 3600
_DOMAIN = b"trobar-cover-v1"


def _signature(secret: bytes, device_id: int, track_id: int, size: int, expires: int) -> str:
    message = b"|".join([_DOMAIN, str(device_id).encode(), str(track_id).encode(),
                         str(size).encode(), str(expires).encode()])
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def address(secret: bytes, device_id: int, track_id: int, size: int, now: float | None = None) -> str:
    """The path and query a device fetches this cover at."""
    expires = int(now if now is not None else time.time()) + TTL_SECONDS
    sig = _signature(secret, device_id, track_id, size, expires)
    return f"/api/device/cover/{track_id}?size={size}&d={device_id}&exp={expires}&sig={sig}"


def valid(secret: bytes, device_id: int, track_id: int, size: int, expires: int, sig: str,
          now: float | None = None) -> bool:
    """Whether `sig` was made for exactly this device, track, size and expiry,
    and the expiry hasn't passed. One that expires further ahead than any
    address is issued for is refused too."""
    now = now if now is not None else time.time()
    if not now < expires <= now + TTL_SECONDS:
        return False
    return hmac.compare_digest(_signature(secret, device_id, track_id, size, expires), sig)
