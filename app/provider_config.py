# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A provider connection that hasn't been saved, for reading through.

Each server provider client reads its connection from the stored config
in one place, its `_current_config()`. The Change library source preview
has to read the TARGET provider's playlists with the credentials the
admin just typed and tested, before anything is stored. `using()` sets
those values for the current context only (a ContextVar, so another
request in another thread keeps reading the stored config), and each
client's `_current_config()` returns them while it is set.

The values are in the client's own `_current_config()` shape, which for
Jellyfin and Emby is the resolved user id, not the username."""

import contextlib
import contextvars

_OVERRIDE: contextvars.ContextVar[tuple[str, tuple] | None] = contextvars.ContextVar(
    "provider_config_override", default=None)


def override(provider_id: str) -> tuple | None:
    """The unsaved connection set for `provider_id` in this context, else None."""
    current = _OVERRIDE.get()
    if current is not None and current[0] == provider_id:
        return current[1]
    return None


@contextlib.contextmanager
def using(provider_id: str, values: tuple):
    token = _OVERRIDE.set((provider_id, tuple(values)))
    try:
        yield
    finally:
        _OVERRIDE.reset(token)
