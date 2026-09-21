# Changelog

This file describes user-facing changes. Published artifacts and generated commit
lists are available in [GitHub Releases](https://github.com/roeidalm/arbox-companion/releases).

## 1.54.14

- Add a generic read-only API and viewer for dashboards: sessions, memberships,
  planning, calendar, history, feedback ratings and sanitized event metadata.
- Provide a separate revocable read credential, pagination and freshness metadata.
  Read projections do not refresh upstream services or write planning state.
- Include integration documentation and a Homepage example.

## 1.54.13

- Add a local Google Calendar privacy information page and link it from setup,
  including accurate retention and disconnect behavior. Clarify the required
  Branding fields shown by Google without changing reverse-proxy access rules.

## 1.54.12

- Simplify personal Google setup into six compact steps with project-aware links,
  exact callback copying, remembered progress, and automatic project detection
  from saved credentials or uploaded JSON. Clarify which Google approvals remain
  manual and distinguish temporary Testing from permanent Production setup.

## 1.54.11

- Make Google Production setup explicit in the calendar wizard, explain the
  seven-day Testing expiry, and link existing connections to their project
  audience settings without replacing credentials or calendars.

## 1.54.10

- Refresh cached frontend assets so existing browsers load the new address and
  Google callback settings immediately.

## 1.54.9

- Preserve membership history after confirmed bookings and immediately reconcile
  balances. Retry pending verification and notify persistent or urgent failures
  without treating a temporary sync gap as a membership eligibility problem.
- Configure separate internal and external server addresses. Notification links
  prefer the external address while Home Assistant keeps its internal API URL.
- Migrate the Google OAuth callback without replacing the existing calendar,
  events, or credentials, and provide a reconnect action to verify the new URL.

## 1.54.8

- Home Assistant integration 3.5.1 removes the one-tap Cancel next class button
  and cleans up its existing entity on integration setup. Cancellation remains
  available through explicit class selection and the existing cancellation flow.

## 1.54.7

- Home Assistant integration 3.5.0 adds Google Calendar status, last sync and
  errors to Overview, with a permitted sync action and link to website settings.
- Expose a Google Calendar status sensor and sync button for HA dashboards and
  automations. Connection setup and calendar preferences stay on the website.

## 1.54.6

- Save the current calendar preferences before manual sync. Show unsaved changes,
  progress and completion beside the action buttons.

## 1.54.5

- Choose reminder amounts in minutes, hours or days in Google Calendar settings,
  with readable reminder labels and automatic conversion.

## 1.54.4

- Show the actual Google calendar event palette as accessible color swatches,
  including all 24 default colors and custom label names. Use Google event labels
  for syncing these colors, preserving existing event identities and preferences.

## 1.54.3

- Include the original class description, attendance counts and waiting-list
  notice in synced Google events, using the same content as calendar files.
  Existing future events receive the missing description on the next sync.
- Verify that changing a status color patches existing events without duplicates.

## 1.54.2

- Return from Google OAuth to the validated browser origin, preserving sign-in
  when setup starts on a local hostname alias. Offer an explicit Google link if
  automatic browser navigation does not complete.

## 1.54.1

- Put manual calendar settings first and align Google Calendar setup with the
  existing settings cards. Collapse the optional wizard and preview, shorten
  instructions, and use a single-column layout with touch-friendly controls on phones.

## 1.54.0

- Add optional Google Calendar sync with an in-app setup wizard and OAuth JSON
  upload. Each Arbox account/studio can connect its own dedicated calendar.
- Customize colors, availability and up to five reminders for scheduled,
  automated, booked, waiting-list and review events; booked events default to
  60- and 30-minute reminders. Existing manual calendar exports remain available.
- Reconcile future events with deterministic inserts, ownership checks, safe
  retries and cancellation/replanning support. Pause or disconnect preserves
  existing events. OAuth secrets remain in private server storage.

## 1.53.5

- Clear an older class-type denial when refreshed Arbox membership details
  explicitly allow that exact, uniquely identified class type. Failed reads,
  omitted or ambiguous class lists and unrelated denials retain their safeguards;
  quota and workout-date validity checks still apply.

## 1.53.4

- Reuse established membership/class eligibility across weeks and restarts without
  age-based expiry or repeated early booking attempts.
- Invalidate eligibility on changed membership details or explicit upstream denial;
  preserve the daily workout review and final identity, validity and quota checks.
- Preserve established class eligibility when refreshed metadata omits its class
  list; an explicit new list still replaces the previous one.

## 1.53.3

- Accept a complete timing-only early-registration refusal for the checked
  membership revision and class type. Reuse cached checks for seven days;
  preserve explicit denials, dates, quota and final booking checks.
- Review existing plans when refreshing memberships and keep other confirmed
  class types in the quota calculation before any early attempt.
- Keep the fallback confirmation beside the selected membership on narrow
  screens, with explicit native checkbox styling and refreshed asset versions.
- Update dependency locks to include the merged uvicorn 0.53.0 and tzdata 2026.4 updates.

## 1.53.2

- Choose a membership for an automatic occurrence directly in My, on desktop,
  mobile and HA panel 3.4.2. Preserve the recurring rule and allow restoring
  automatic membership selection for the occurrence.
- Refresh inventory and evidence when opening the picker, with an explicit
  Refresh memberships button there and in Studio so new cards can be selected.
- Require explicit class-type confirmation when Arbox omits eligibility. Keep
  denial, quota and history checks; report missing eligibility rather than full
  capacity when an unverified extra card has available entries.
- Disable memberships outside the workout date's validity and re-read inventory
  before saving. Bind confirmations to the account, studio, workout and current
  membership revision, with one-use actions and no automatic write retry.

## 1.53.1

- Keep original workout details for planned occurrences. Pause on changes to
  class type, coach or time, including automatic occurrences that stop matching
  their rule. Require fresh, scoped confirmation before resuming; quota still applies.
- Review only My workouts once daily immediately before the nightly digest.
  Include changes in that message, with Telegram/HA confirmation or cancellation,
  and prevent duplicate daily sends across restarts. Background syncs remain quiet
  about changed workout details; a fresh check guards every booking/probe.
- Keep one HA daily notification even with many actions; full class actions remain
  in Telegram and the panel. Reject stale notification buttons after a replacement.
- Keep disappeared planned workouts visible for review. Preserve unknown booking
  outcomes and existing registrations. Initialize old pins from cached details.
- Add in-place confirmation to My and HA panel 3.4.1; install through HACS normally.
- Preserve detected changes, acceptance and cancellation in the existing workout
  history, including before/after details, reason and detection/decision time.
  Accepted plans show the new class name with the former name beside it in My.
  Cancellation removes the active plan without deleting history or consuming quota.

## 1.53.0

- Share colored, searchable multi-select filters across the calendar, My,
  journal and automation editor in the app and HA panel 3.4.0. Selected items
  stay first across searches; Everything clears the current filter group.
- Show compact workout/coach counts beside membership quotas on desktop,
  with an expandable summary row on phones. My workouts remain visible.
- Resolve planning notices directly in Telegram and HA: confirm ignoring a
  class type in the active studio, or choose a membership for one occurrence.
  Preserve booked workouts and recurring rules; recheck eligibility, quota,
  membership revision and studio before accepting each confirmation.
- Partial user confirmation allows only the selected class type when studio
  eligibility is unknown. Explicit studio restrictions and quota limits still
  take precedence. Do not probe a class type already confirmed by the user.
- Edit the source Telegram message and replace the HA notification by tag.
  Update the HA webhook relay/template as described in dashboard/README.md;
  existing callback forwarding remains compatible. Existing notices retain
  their old buttons; new actions appear on newly issued notices.

## 1.52.1

- Keep My workouts and actions visible; move membership editing to Studio.
  Include the free remainder in every quota track, and retain day/week/month
  calendar views with multi-select coach and class filters.

## 1.51.1

- Make planning notices easier to read: separate workout details, show the
  relevant monthly capacity, and offer one clear review action. Keep the footer
  short and preserve notification deduplication when upgrading.

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
