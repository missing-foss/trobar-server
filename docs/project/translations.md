<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Translating Trobar

Trobar ships five apps — server, Android, desktop, Garmin, and the Home
Assistant integration — each with its own i18n system, currently in
**English (EN)** and **French (FR)**. There's no shared toolchain, so adding
a language means doing it once per app. This page walks through each. Pick
the app(s) you want to translate and open a PR against that app's repo.

Every app already gates translation completeness in CI, so the checklists
below double as what a reviewer will actually check.

## Server (`trobar-server`) — Flask-Babel / gettext

Source strings live in Python (`_("...")`) and Jinja templates
(`{% trans %}`), extracted into `app/translations/messages.pot`.

1. Initialize a new catalog (first time for that language) or update an
 existing one — **run from inside `app/`**, not the repo root: extracting
 from the root prefixes every `#:` path comment in the `.pot`/`.po` files
 with `app/` and drops their SPDX header, which then fails `reuse lint`.
   ```sh
   cd app
   pybabel extract -F babel.cfg -o translations/messages.pot .
   pybabel init -i translations/messages.pot -d translations -l <lang>
   # or, updating an existing catalog after new strings were added:
   pybabel update -i translations/messages.pot -d translations -l <lang>
   ```
2. Translate every entry in
 `app/translations/<lang>/LC_MESSAGES/messages.po`.
3. Compile it: `pybabel compile -d app/translations`.
4. Register the language in `app/main.py`: `_get_locale()` checks the
   language code in two places (the cookie-value check and the
 `best_match([...])` fallback), and `set_language()`'s
 `if lang not in (...)` guard is a third — all three need the new code
   added.
5. Add the language to the Preferences language-picker links in
 `app/templates/index.html` (search for `set_language`).

**CI gate**: `dev/check_translations.py` (run via `dev/verify.sh`) fails the
build on any untranslated or fuzzy `.po` entry, or on a placeholder
(`{name}`, `%(x)s`) that doesn't match the English source — a PR must be
translation-complete to pass.

## Android (`trobar-android`) — Android string resources

1. Add `app/src/main/res/values-<lang>/strings.xml`, translating every
 string that exists in `app/src/main/res/values/strings.xml`.
2. Add the language to the in-app picker: the `ChoiceRow` options list in
 `MainActivity.kt` (search for `"fr" to "Français"`).

**CI gate**: lint's `MissingTranslation` / `ExtraTranslation` checks (see
`app/build.gradle.kts`) fail the build if `values-<lang>/` doesn't have an
exact 1:1 key match with `values/` — nothing missing, nothing left over.

## Desktop (`trobar-desktop`) — Flutter ARB

1. Add `lib/l10n/app_<lang>.arb`, translating every entry in
 `lib/l10n/app_en.arb` (the template file, per `l10n.yaml`).
2. Run `flutter gen-l10n` to regenerate `lib/l10n/gen/app_localizations.dart`
   — this is also where an untranslated entry surfaces, via
 `lib/l10n/untranslated.txt`.
3. Add the language code to `languageValues` in `lib/app_prefs.dart` and to
 the picker in `lib/settings_screen.dart` (search for `Français`).
 `supportedLocales` in `main.dart` is generated from the ARB files
   automatically — no separate registration there.

## Garmin (`trobar-garmin`) — Connect IQ resources

1. Add `resources-<lang>/strings.xml`, translating every string from the
 base `resources/strings.xml`.
2. Declare the language in `manifest.xml`'s `<iq:languages>` block — note
 Connect IQ uses **3-letter codes** here (`eng`, `fre`), not the 2-letter
   codes the other three apps use.
3. **Wire the resource path explicitly in `monkey.jungle`**:
   ```
   base.lang.<code> = $(base.lang.<code>);resources-<lang>
   ```

 ⚠️ **This step is the trap.** A language declared in `manifest.xml` with
 no resource path wired in `monkey.jungle` **compiles green and lints
   clean** — and silently ships English strings on-device
. The SDK's own default resource discovery may already
   cover common language codes, but there's no build-time signal that tells
 you whether it actually found your `resources-<lang>/` folder or silently
   fell back to English. Wiring the path explicitly costs nothing if the
   default already had it covered, and is the actual fix if it didn't.
4. **Verify it renders**, not just that it builds: run the simulator in the
   new locale and confirm the strings actually show translated. A green
   build is not proof of a working translation for this app.

## Home Assistant integration (`trobar-ha`) — plain JSON, not gettext

This one genuinely works differently from the other four, worth stating
plainly rather than leaving a reader to assume it follows the gettext
pattern above. Home Assistant's own translation model: no `.po`/`.pot`, no
`pybabel`, no compile step. Strings live in plain JSON, keyed by structure
rather than extracted from source — `config.step.user.data.url`,
`entity.sensor.pending_tracks.name`, and so on.

- `custom_components/trobar/strings.json` is the source of truth.
- `custom_components/trobar/translations/en.json` is its English copy.
- Every other language is its own file, e.g. `translations/fr.json` —
 mirroring `strings.json` **key-for-key**, nothing more, nothing less.
  Core Home Assistant integrations get non-English translations generated
  by Lokalise; a custom integration like this one just commits the files by
  hand.

1. Copy `translations/en.json` to `translations/<lang>.json`.
2. Translate every value, keeping every key exactly as it is.
3. Check wording that names a specific navigation path (e.g. where the
   integration token is created) against the **server's own** translation
 for that path in `app/translations/<lang>/LC_MESSAGES/messages.po`,
   rather than translating the English fresh — the two need to describe the
   same click-path in the same words.

**CI gate**: `tests/test_translations.py` flattens `strings.json` and every
`translations/*.json` into key sets and asserts they're identical, plus that
every value is a non-empty string — the JSON equivalent of
`dev/check_translations.py`'s job above, since none of the gettext-era
tooling on this page applies to this app.

## Submitting

Open a PR against the relevant repo (each of the five is translated
independently — you don't need to touch all of them). See each repo's
`CONTRIBUTING.md` for the general PR process.
