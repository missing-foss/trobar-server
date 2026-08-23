<!--
SPDX-FileCopyrightText: 2026 missing-foss

SPDX-License-Identifier: AGPL-3.0-or-later
-->

# Contributing to Trobar

Thanks for your interest. A few honest expectations first: this is a young,
household-driven project — it was built for one family's real daily use and
is maintained in evenings-and-weekends time. Bug reports and small, focused
PRs are very welcome; grand redesigns probably won't land.

## Where development happens

Public issues and pull requests live here on GitHub and are actively
watched; that is all you need to contribute.

Pull requests opened here are reviewed and then shipped by the maintainers
rather than merged in place, so your commits may arrive under a release
commit rather than your own. Your pull request is closed with a note
crediting you when the change lands. Keep changes focused — a small pull
request with a clear rationale is much easier to take than a large one.

## Before you start

- **Open an issue before a large PR.** Small fixes (typos, obvious bugs,
  doc corrections) can go straight to a PR; anything that changes behaviour
  or adds a feature should be discussed first so you don't build something
  that can't be merged.
- **Licensing**: the server is `AGPL-3.0-or-later`; the Android, desktop,
 Garmin, and Home Assistant integration clients are `GPL-3.0-or-later`. By
  contributing you agree your contribution is licensed under the same
  license as the component it touches.

## AI-assisted contributions

Using an AI assistant to write a patch is fine — parts of this project are
maintained that way, and it would be dishonest to forbid it. Two conditions:

- **Say so in the PR.** One line is enough ("drafted with <tool>"). This
  isn't a legal requirement — the EU AI Act's labelling duties fall on the
  people who *build* AI systems, not on people who use their output — but a
  reviewer who knows what generated a diff reviews it differently, and this
  project verifies claims empirically rather than trusting them.
- **You are the author.** You vouch that you understand the change, that
  you have the right to contribute it under the licence above, and that it
  isn't a verbatim lift of incompatibly-licensed code the model memorised.
  Provenance is the real risk here, not the tooling.

Unreviewed, machine-generated PRs opened in bulk will be closed without
discussion.

Some of this project's own accounts are AI-operated. If you would rather a
human read your PR, ask in the PR and one will.

## Contributing a translation

See [Translating Trobar](https://missing-foss.github.io/trobar-server/project/translations/)
— covers all five apps (server, Android, desktop, Garmin, Home Assistant
integration), each with its own i18n system and CI completeness gate.

## Running it for development

Every repo has a `dev/verify.sh` that mirrors what CI will run. Run it
before opening a PR — it is the fastest way to find out that something
fails, and it fails for the same reasons CI does.

- **Server**: the `dev/` folder has a compose setup for a throwaway
 instance (own data volume, `AUTH_MODE=local`) — safe to experiment
 against, no SSO stack needed. `dev/verify.sh` runs `py_compile`, the
  unit suite, mypy, inline-JS syntax + eslint checks over the templates,
  a Tailwind CSS drift check, translation completeness, leak and secret
  scans, and REUSE lint. CI runs all of that plus a docker build and a
  Playwright end-to-end suite that boots a real server and drives it
 with a browser, so a green `verify.sh` is necessary but not sufficient.
- **Android**: standard Gradle project. `./gradlew assembleDebug` must
  pass. Release signing is maintainer-only (see
 `docs/troubleshooting.md` if you want to sign your own builds).
- **Desktop**: Flutter. `flutter analyze && flutter test` must pass; the
  engine tests run against temp folders and a mocked API, no server needed.
- **Garmin**: Connect IQ (Monkey C). A local compile check needs the
  [Connect IQ SDK](https://developer.garmin.com/connect-iq/sdk/) and a
  throwaway developer key.
- **Home Assistant integration**: Python, in a venv from
 `requirements-dev.txt`. Its suite runs on
 `pytest-homeassistant-custom-component`. `hassfest` is not run locally
  — it needs Docker and only runs in CI.

## Style

Match what's around you. The codebase favours explicit, commented decisions
over cleverness — if a change needs a paragraph to justify, put that
paragraph in the code or the PR, not in the void.

## Reporting security issues

Not in the public tracker — see [SECURITY.md](SECURITY.md)
(missing_foss@etik.com).
