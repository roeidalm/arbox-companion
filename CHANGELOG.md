# Changelog

This file describes user-facing changes. Published artifacts and generated commit
lists are available in [GitHub Releases](https://github.com/roeidalm/arbox-companion/releases).

## 1.51.0

- Match each planned workout to an eligible membership before reserving its
  capacity. Count bookings, pending pins and recurring occurrences together,
  deduplicate workouts and retain uncovered intentions for correction.
- Reconcile actual membership attribution from the owned-membership schedule
  history. Punch cards retain their lifetime allowance across month boundaries;
  recurring limits require explicit evidence or a manual definition.
- Add the shared membership policy editor to My in the app and HA panel 3.2.0.
  Show evidence, unresolved category names, missing quotas and paused plans.
  Update the server first, then HACS, and review membership definitions.
- Review newly discovered plans before registration opens, with bounded early
  attempts only for desired classes outside a verified closed window. Preserve
  ambiguous results across restarts and require reconciliation before retrying.
- Record missed-window decisions once, keep notification state across restarts,
  and include existing future plans in the nightly digest's same-day section.
- Remove only legacy automatic global category blocks identified in log evidence.
  New eligibility evidence is scoped to account, studio and membership instance.

## 1.48.1

- Arbox integration 2.9.0 includes the feedback form inside Home Assistant. The
  phone uses its existing authenticated HA connection; only HA contacts the
  private Arbox server. VPN and HTTPS reverse-proxy access are supported.
- Update the integration through HACS and restart HA before enabling
  Settings → Home Assistant → Open feedback inside Home Assistant. The default
  remains direct links for compatibility. HA 2024.8 or newer is required.
- Server and HA share the same form assets. Added HA transport/authentication
  and frontend tests to the release workflow.

## 1.47.1

- Expanded release history shows only notes not already visible in the summary.

## 1.47.0

- Workout feedback opens in one phone-friendly form from Telegram or Home Assistant.
  Rate the class and coach, optionally add notes and exercises, and save once to the
  workout journal. A rehearsal uses the same form without saving a workout.
- My classes includes projected automatic registrations. Skip a single occurrence
  or restore it without changing the recurring automation; quota planning respects
  the exception.
- A short “What's new” card introduces changes after upgrading. Reopen it in
  Settings → Advanced → What's new. Dismissal is remembered per browser and server
  origin, per installed version. Clearing browser storage or using another device
  shows it again; no server-wide acknowledgement hides news from other people.

## 1.46.0

- Added end-to-end workout feedback rehearsals for Telegram and Home Assistant,
  without adding demo workouts to the journal.

## Maintaining in-app release notes

The image contains `arbox-server/frontend/release-notes.json`. Keep `current.notes`
to three concise Hebrew user-facing items, describing what people can now do.
Each item has a `kind`: `added`, `changed`, `removed`, or `action` (requires user
action), plus `title` and `text`. Optional `href` and `link_label` point to one of
the supported application pages. Record detailed changes here as well.

No release number is invented in source: the card uses the immutable installed
version from `/api/health`, baked by the existing tag-triggered GitHub workflow.
`current.after` is the last published version before these changes; the current
summary only applies to a newer installed release. Local `dev` and `local-demo`
builds explicitly say that the changes are in development. Other unknown build
identifiers do not automatically show a release announcement.

Before preparing the next feature release, move the previously shipped current
notes into `releases` with their actual version, update `current.after`, and give
the new current block a new stable `id`. This keeps archived notes tied to their
real release. Do this for maintenance releases too, so old feature announcements
do not appear as newly added. The `id` also identifies a local development preview.
If a user skips versions, archived action-required and removal notices since their
last acknowledgement are prioritized in the three-item summary. The expandable
history always contains all available notes through the installed version.

Validation: `node --test arbox-server/tests/test_release_notes.cjs`. The normal
pytest release checks also validate the manifest. Build and publish releases only
through the existing GitHub tag workflow.
