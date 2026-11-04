#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 missing-foss
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The modules split out of sync_state stay split: none of them imports
sync_state back, and every name sync_state still re-exports for its callers
is the same object as in its new home, not a copy that a test or a caller
could change on one side only.

    python3 -m unittest test_store_split -v      # from app/
"""
import ast
import unittest
from pathlib import Path

import autofit
import basket_store
import device_store
import selection_store
import sync_state
import token_store
import unresolved_store

_APP = Path(__file__).parent
# Each split-out module, and the names sync_state re-exports from it.
_SPLIT = {selection_store: [
    "create_selection", "removal_count", "selected_by_others", "toggle_selection_device",
    "delete_selection", "parse_target_id", "_resolve_selection_track_ids",
    "recompute_playlist_devices", "required_track_ids_for_device", "recompute_device_state",
    "_DEVICE_PLAYLIST_IDS_SQL",
], basket_store: [
    "list_basket", "add_basket_item", "unstage_basket_item_device", "prune_linkless_basket_items",
    "remove_basket_item", "clear_basket", "basket_last_destinations_dict", "_rewrite_last_destinations",
    "prune_device_from_last_destinations", "remap_device_in_last_destinations",
], token_store: [
    "hash_token", "regenerate_token", "authenticate_device", "_ENROLL_ALPHABET",
    "ENROLLMENT_TTL_SECONDS", "_generate_enrollment_code", "create_enrollment_grant",
    "create_integration_token", "authenticate_integration_token", "list_integration_tokens",
    "revoke_integration_token",
], unresolved_store: [
    "record_unresolved_playlist_tracks", "accepted_suggestion", "list_unresolved_playlist_tracks",
    "set_unresolved_playlist_tracks_accepted", "set_unresolved_playlist_tracks_excluded",
], autofit: [
    "AutofitSummary", "DEFAULT_AUTOFIT_PERIOD", "autofit_selection_id_for_device",
    "create_autofit_selection", "autofit_status", "_TRANSCODE_BYTES_PER_SEC", "_EST_SLACK_BYTES",
    "_device_size_estimate", "_sum_device_bytes", "refresh_autofit", "autofit_fill_basis",
], device_store: [
    "M3U_MARKER", "TRANSCODE_FORMATS", "_FS_ILLEGAL", "_LOSSLESS_EXTS", "_TRANSCODE_EXT",
    "_device_path", "_device_playlists", "_device_playlists_by_id", "_is_lossless",
    "_lowest_transcode_tier", "_parse_device_path", "_record_device_unknown_tracks",
    "_refresh_unknown_track_count", "_source_ext", "ack", "add_device_unknown_tracks",
    "backfill_delivered_paths", "create_device", "device_path", "device_path_index",
    "device_playlist_filenames", "device_playlist_ids", "fs_segment", "get_changes",
    "legacy_device_path", "list_device_unknown_tracks", "on_device_path", "record_device_manifest",
    "redeem_enrollment_grant", "resolve_card_entry", "resolve_missing_tracks",
    "set_device_unknown_adopted", "sync_status", "transfer_device", "wants_transcode",
]}


class StoreSplitTests(unittest.TestCase):
    def test_no_split_module_imports_sync_state(self):
        for module in _SPLIT:
            tree = ast.parse((_APP / f"{module.__name__}.py").read_text())
            imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
            with self.subTest(module=module.__name__):
                self.assertNotIn("sync_state", imported)

    def test_sync_state_re_exports_the_same_objects(self):
        for module, names in _SPLIT.items():
            for name in names:
                with self.subTest(name=name):
                    self.assertIs(getattr(sync_state, name), getattr(module, name))

    def test_moved_names_are_defined_only_in_their_new_home(self):
        tree = ast.parse((_APP / "sync_state.py").read_text())
        defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        defined |= {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
        for names in _SPLIT.values():
            self.assertEqual(defined & set(names), set())


if __name__ == "__main__":
    unittest.main()
