#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where device sync used to live, kept for one release as re-exports.

Everything that was here has moved, one module per concern: selections and
the device state they add up to (selection_store), the basket
(basket_store), tokens (token_store), devices (device_store), playlist
entries that matched no library track (unresolved_store) and auto-fill
(autofit). Each `sync_state.X` still names the same object, so callers keep
working; new code imports the module it needs directly.
"""

import logging

# _new_id lives in db, so every module split out of this one can use it
# without importing this one back; re-exported for sync_state._new_id callers.
from db import _new_id  # noqa: F401
# Moved to selection_store.py. Re-exported for one release, so every
# `sync_state.X` keeps working; new code imports selection_store directly.
from selection_store import (  # noqa: F401
    _DEVICE_PLAYLIST_IDS_SQL,
    _resolve_selection_track_ids,
    create_selection,
    delete_selection,
    parse_target_id,
    recompute_device_state,
    recompute_playlist_devices,
    removal_count,
    required_track_ids_for_device,
    selected_by_others,
    toggle_selection_device,
)
# Moved to basket_store.py. Re-exported for one release, so every
# `sync_state.X` keeps working; new code imports basket_store directly.
from basket_store import (  # noqa: F401
    _rewrite_last_destinations,
    add_basket_item,
    basket_last_destinations_dict,
    clear_basket,
    list_basket,
    prune_device_from_last_destinations,
    prune_linkless_basket_items,
    remap_device_in_last_destinations,
    remove_basket_item,
    unstage_basket_item_device,
)
# Moved to token_store.py. Re-exported for one release, so every
# `sync_state.X` keeps working; new code imports token_store directly.
from token_store import (  # noqa: F401
    _ENROLL_ALPHABET,
    _generate_enrollment_code,
    authenticate_device,
    authenticate_integration_token,
    create_enrollment_grant,
    create_integration_token,
    ENROLLMENT_TTL_SECONDS,
    hash_token,
    list_integration_tokens,
    regenerate_token,
    revoke_integration_token,
)
# Moved to device_store.py. Re-exported for one release, so every
# `sync_state.X` keeps working; new code imports device_store directly.
from device_store import (  # noqa: F401
    _device_path,
    _device_playlists,
    _device_playlists_by_id,
    _FS_ILLEGAL,
    _is_lossless,
    _LOSSLESS_EXTS,
    _lowest_transcode_tier,
    _parse_device_path,
    _record_device_unknown_tracks,
    _refresh_unknown_track_count,
    _source_ext,
    _TRANSCODE_EXT,
    ack,
    add_device_unknown_tracks,
    backfill_delivered_paths,
    create_device,
    device_path,
    device_path_index,
    device_playlist_filenames,
    device_playlist_ids,
    fs_segment,
    get_changes,
    legacy_device_path,
    list_device_unknown_tracks,
    M3U_MARKER,
    on_device_path,
    record_device_manifest,
    redeem_enrollment_grant,
    resolve_card_entry,
    resolve_missing_tracks,
    set_device_unknown_adopted,
    sync_status,
    TRANSCODE_FORMATS,
    transfer_device,
    wants_transcode,
)
# Moved to unresolved_store.py. Re-exported for one release, so every
# `sync_state.X` keeps working; new code imports unresolved_store directly.
from unresolved_store import (  # noqa: F401
    accepted_suggestion,
    list_unresolved_playlist_tracks,
    record_unresolved_playlist_tracks,
    set_unresolved_playlist_tracks_accepted,
    set_unresolved_playlist_tracks_excluded,
)
# Moved to autofit.py. Re-exported for one release, so every
# `sync_state.X` keeps working; new code imports autofit directly.
from autofit import (  # noqa: F401
    _device_size_estimate,
    _EST_SLACK_BYTES,
    _sum_device_bytes,
    _TRANSCODE_BYTES_PER_SEC,
    autofit_fill_basis,
    autofit_selection_id_for_device,
    autofit_status,
    AutofitSummary,
    create_autofit_selection,
    DEFAULT_AUTOFIT_PERIOD,
    refresh_autofit,
)

_log = logging.getLogger(__name__)
