<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Contributing

The full, versioned guide is
[`CONTRIBUTING.md`](https://github.com/missing-foss/trobar-server/blob/main/CONTRIBUTING.md)
at the repo root (it's also surfaced in GitHub's issue/PR composer). The short
version:

- This is a young, household-driven project, maintained in
  evenings-and-weekends time. **Bug reports and small, focused PRs are very
  welcome; grand redesigns probably won't land.**
- **Open an issue before a large PR** so you don't build something that can't be
  merged. Small fixes (typos, obvious bugs, doc corrections) can go straight to a
  PR.
- **Licensing:** the server is `AGPL-3.0-or-later`; the Android, desktop,
 Garmin, and Home Assistant integration are `GPL-3.0-or-later`. By
  contributing you agree your contribution is licensed under the same
  license as the component it touches. Per-file SPDX headers are enforced by
 `reuse lint` in CI.
- **AI-assisted patches are fine — say so in the PR.** You're still the
  author: you vouch that you understand the change and that it isn't a
  verbatim lift of incompatibly-licensed code. Some of this project's own
  accounts are AI-operated. See
 [`CONTRIBUTING.md`](https://github.com/missing-foss/trobar-server/blob/main/CONTRIBUTING.md#ai-assisted-contributions)
  for the full version.

## Contributing a translation

See [Translating Trobar](translations.md) — covers all five apps, each with
its own i18n system and CI completeness gate.

## Running it for development

- **Server** — the `dev/` folder has a throwaway compose setup (own data volume,
 `AUTH_MODE=local`). `dev/verify.sh` runs `py_compile`, unit tests, mypy,
  inline-JS lint, a Tailwind CSS drift check, translation completeness,
  leak/secret scan and REUSE lint. CI runs all of that plus a docker build and
 a Playwright end-to-end suite, so a green `verify.sh` is necessary but not
  sufficient.
- **Android** — standard Gradle; `./gradlew assembleDebug` must pass. Signing
  your own builds: [Troubleshooting → Building your own APK](../troubleshooting.md#building-your-own-apk).
- **Desktop** — Flutter; `flutter analyze && flutter test` must pass.
- **Garmin** — Connect IQ (Monkey C); needs the
  [Connect IQ SDK](https://developer.garmin.com/connect-iq/sdk/) and a
 throwaway developer key for a local compile check. `dev/verify.sh` is the
  only place the device compile and the simulator tests run at all — CI there
  checks what the source can answer for on its own (version consistency,
  packaged defaults, gitleaks, REUSE lint), so run it before opening a PR.
- **Home Assistant integration** — Python; a venv with
 `pip install -r requirements-dev.txt`. `dev/verify.sh` runs lint, the test
 suite (`pytest-homeassistant-custom-component`), gitleaks and REUSE lint.
 It doesn't run `hassfest` — that needs Docker and only runs in CI.

Every change goes through a pull request with a passing CI check and an
approving review. Pushing to a pull request that has already been approved
dismisses that approval — that is deliberate, so what was reviewed is what
ships. Expect to need a re-review after pushing a fix-up.

Pull requests opened here are reviewed and then shipped by the maintainers
rather than merged in place, so your commits may arrive under a release commit
rather than your own. Your pull request is closed with a note crediting you
when the change lands. Keep changes focused — a small pull request with a
clear rationale is much easier to take than a large one.
