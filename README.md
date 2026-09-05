# Arbox Companion — self-hosted booking server + Home Assistant integration

Replaces the Arbox mobile app for day-to-day use: see the weekly schedule,
filter by coach/category, book / cancel / join standby in one tap, and get a
nightly message offering to book tomorrow's matching classes — or have the
server book them automatically the moment registration opens.

Two parts, one repo:

| Part | What it does |
| --- | --- |
| [`arbox-server/`](arbox-server/) | The brain. Owns the Arbox session, syncs the schedule into SQLite, serves a Hebrew RTL web UI, evaluates booking rules, sends notifications (Telegram / HA). |
| [`custom_components/arbox/`](custom_components/arbox/) | HACS integration. Hosts the full Arbox panel inside HA, with schedule, bookings, journal and automations; also exposes sensors/calendar/services. Talks only to your internal server. |

**One Arbox client only:** the integration talks to the server, the server
talks to Arbox. Never run the old direct-to-Arbox integration alongside the
server.

## arbox-server

### Run

Compose files live in [`deploy/`](deploy/) — one standalone, one that shares a
network with Home Assistant. Neither holds any secret, so either can be pasted
straight into Portainer (Stacks → Add stack → Web editor) or run with:

```bash
docker compose -f deploy/docker-compose.yml up -d
```

See [`deploy/README.md`](deploy/README.md) for first run, upgrades and backup.

To build from source instead of pulling the published image:

```bash
cd arbox-server
docker build -t arbox-server .
docker run -d --name arbox-server \
  -p 127.0.0.1:8177:8000 \
  -v arbox-data:/data \
  -e TZ=Asia/Jerusalem \
  arbox-server
```

Open the UI, enter your Arbox email + password and your studio's whitelabel
(the branded app name — e.g. `Moveom`; leave blank to try `Arbox`).
Credentials and tokens persist in `/data` (mode 0600) and survive restarts
and recreates. The setup
response hands the browser an API key (also in `/data/settings.json`) —
required for booking actions and settings, not for viewing.

Local development:

```bash
pip install -r arbox-server/requirements.txt
DATA_DIR=/path/to/data TZ=Asia/Jerusalem uvicorn app.main:app --app-dir arbox-server
```

### Sync model

The UI and HA read SQLite only — opening the week view costs zero Arbox calls.
Upstream refresh is tiered by how fast each thing actually changes:

- full 14-day window: 2×/day (one `betweenDates` range call), plus a nightly
  roll at 03:05 that drops past days
- next 48h (free spots, standby position): every 30 min
- membership inventory: daily, one hour before the nightly digest; all active
  memberships are tracked and a preferred one is used first
- `⟳` in the UI / `?refresh=1` on the API: stale-while-revalidate background sync

### Rules & notifications

Rules match on coach / category / weekday / time window, in two modes:

- **notify** — matching classes for tomorrow appear in one nightly message
  with a book button per class
- **autobook** — the server books the class itself the moment its registration
  window opens (`start − enable_registration_time`, 168h at this studio) and
  tells you it did

**My classes** includes future automatic rule matches alongside bookings and
one-class schedules. Automatic rows use the same projection as the membership
quota counter. “Skip this class” excludes only that occurrence, frees its planned
quota, and leaves the recurring rule unchanged. The skipped row stays visible
with a restore button. Exclusions are saved per studio in SQLite and survive
syncs and restarts. Explicit one-class schedules and existing bookings retain
their own cancellation actions; an automation skip never cancels a booking.
Restoring returns the occurrence to the normal booking-window and quota rules.
HA booked/next-class sensors continue to show actual registrations only.

Standby promotions are detected on every sync and announced.

Five minutes before a booked class the server asks whether you arrived. The
answer remains live until midnight; no answer defaults to attended and is
recorded as an automatic decision. A "no" answer asks for a reason (including
free-text Other and No reason). Completed classes move into history at their
end time, where attendance and reasons remain editable. Every cancellation
also records a local reason and appears as either on-time or late; the reason
is never sent to Arbox. Clicking a history status reveals the append-only
timeline for that `schedule_id` (booking, cancellation, rebooking and attendance).
A successful rebooking becomes the current state and re-arms attendance tracking
without erasing the earlier cancellation or its entry-count decision.

Channels (Settings tab, any combination):

- **Telegram (direct)** — bot token + chat id; buttons answered via
  `getUpdates` long-polling, no public webhook, no HA dependency
- **Home Assistant** — a webhook URL only (no long-lived token: the
  unguessable webhook id is the secret, and it can fire exactly one
  automation — least privilege). Two automations in
  [`dashboard/arbox-callback-automation.yaml`](dashboard/arbox-callback-automation.yaml):
  one turns the webhook into an actionable phone notification, the other
  forwards pressed buttons back to `/api/ha/callback`

### Trying workout feedback

In **Settings → Tracking**, select Quick, Feedback, or Full and press
**Try in Telegram** or **Try in HA**. The selected level is sent without
saving settings. Each rehearsal uses the saved connection for just that
channel, even when its regular notifications are disabled; it does not use
channel ordering or escalation. Save connection details first if they changed.

The notification contains one **Fill feedback** link. It opens an Arbox page
with ratings together, optional notes and multiple catalogue/custom exercises,
and one save button. Quick shows one rating; Feedback adds both ratings and
optional notes/exercises; Full opens the optional section immediately. Saving
confirms in the page and sends no further messages. Real journal prompts use
only the first enabled journal channel in notification order, without escalation.

For a form hosted **inside Home Assistant**, update the Arbox integration to
**2.9.0 or later through HACS**, restart HA, and enable **Settings → Home
Assistant → Open feedback inside Home Assistant** in Arbox. Save before testing.
The notification navigates to `/arbox-feedback` on the phone's current HA server.
HA authentication protects the panel's data requests; its integration forwards
submissions to Arbox over the internal network. Only HA needs to be reachable
from the phone, whether through VPN or HTTPS behind a reverse proxy.

The setting defaults off for compatibility with older integrations. In that
mode, and for Telegram, set **Settings → Server URL** to a phone-reachable Arbox
URL (HTTPS outside a trusted network; a tailnet URL requires the phone VPN).
The existing webhook automation passes URI actions through unchanged.
A ready-to-import HA blueprint and setup steps are in
[`dashboard/feedback-notifications.md`](dashboard/feedback-notifications.md).

Links authorize only one workout and expire after seven days (30 minutes for
rehearsals), surviving server restarts. The capability is in the URL fragment,
never the server API key. Direct forms send it in an HTTP header; HA forms send
it through HA’s authenticated WebSocket connection to the integration.
Treat the link as private. Successful submission consumes it; duplicate submits
are harmless. Real answers use the existing SQLite workout journal and exercise
records, remain editable in **Journal**, and mark attendance as attended. Demo
submissions write only temporary prompt metadata, never attendance or journals.

API: `POST /api/settings/test-journal/{telegram|ha}` with `X-Api-Key` and
`{"level":"quick|feedback|full"}`. The response includes the same notification
URL for convenient testing. No upcoming or completed class is required.

### Calendar

Booking a class hands you the event as a file — no subscription, no calendar
credentials on the server, and it works the same for Google and Apple because
the phone decides where the event lands:

- **Telegram**: the bot sends an `.ics` attachment right after a booking; one
  tap opens the calendar picker.
- **Home Assistant**: the confirmation notification carries an "add to
  calendar" action. Needs `base_url` in settings — the externally reachable
  address of this server, since the phone opens that link itself.
- **Web UI**: a `📅 ליומן` link on each booked class, and on the toast right
  after you book.
- Reminders are configurable and can be stacked — tick any of 10 min / 30 min
  / 1 hour / 2 hours / 1 day and the event carries one alarm per choice
  (none ticked = no reminder). Standby entries get no alarm, since the spot
  isn't confirmed.
- The studio's name and street address are attached as the event location,
  derived from the account profile on sync — so the calendar entry maps and
  navigates like any other appointment.

`GET /api/calendar/event/{schedule_id}.ics` is intentionally unauthenticated:
it returns one class from the studio's own schedule with no personal
identifiers, and both the web link and the HA action are plain URLs that
cannot carry an `X-Api-Key` header.

### API

`GET /api/health` · `GET /api/schedule?date_from&date_to&coach&category&mine`
· `POST /api/refresh {date_from?, date_to?}` (on-demand sync from Arbox —
awaits completion; omitted range = full window; clamped to [today, +31d];
debounced 10s per range) · `GET /api/facets` · `GET /api/me` ·
`GET /api/summary` (everything HA needs) ·
`POST /api/book|standby {schedule_id}` ·
`POST /api/cancel {schedule_id, late_cancel?, reason_code, reason_text?}` — a cancel inside the studio's
12h window returns 409 `late_cancel_required` until resent with
`late_cancel: true` · `GET /api/history` ·
`PUT /api/history/{schedule_id}/attendance {status, reason_code?, reason_text?}` ·
`GET|POST /api/rules` · `DELETE /api/rules/{id}` ·
`GET /api/calendar/event/{schedule_id}.ics` ·
`GET|POST /api/settings` · `POST /api/settings/test/{channel}` ·
`POST /api/setup` (409 once configured; `DELETE` to reset) ·
`POST /api/ha/callback`

Mutating endpoints require the `X-Api-Key` header.

Upstream payloads (cancel needs `schedule_user_id`, leaving a waitlist is its
own `scheduleStandBy/delete` endpoint, every write returns the full updated
session object) were verified against a HAR capture of the real app performing
book / cancel / standby-join / standby-leave; see the documented methods in
[`app/arbox_client.py`](arbox-server/app/arbox_client.py).

## Home Assistant integration

Install through HACS: add `https://github.com/roeidalm/arbox-companion` as a custom
repository of type **Integration**, download Arbox, and restart HA. Then use
**Settings → Devices & services → Add integration → Arbox**. Enter the server
URL (e.g. `http://arbox-server:8000` only when both share a container network)
and the Arbox API key. Update future versions through HACS and restart HA.

**Feedback inside HA:** requires server **1.48.1+**, integration **2.9.0+** and
HA **2024.8+**. The form comes with the integration; no separate card or add-on
is needed. After updating, enable **Open feedback inside Home Assistant** in
Arbox settings and **save before sending a new test**. This setting defaults
off and is not enabled by a HACS update. Old notifications keep their old links.
See the [installation checklist, verification and troubleshooting guide](dashboard/feedback-notifications.md).

Entities:

- `sensor.arbox_next_class` — timestamp of your next booked class, with
  category/coach/schedule_id attributes
- `sensor.arbox_membership` — plan name, price, active
  (attributes also include every active membership plus used/reserved entries)
- `sensor.arbox_booked_classes` — count, with the full list as an attribute
- `calendar.arbox_schedule` — the week's classes; 🔵 booked · 🟡 standby ·
  🔴 full · 🟢 bookable

Services (for automations): `arbox.book_class`, `arbox.cancel_booking`,
`arbox.join_standby` — each takes `schedule_id`; cancellation also accepts
`late_cancel`, `reason_code`, and free-text `reason_text` for `other`.

### Full Arbox panel (recommended)

Integration **3.1.0** adds **Arbox** to the HA sidebar automatically. It includes
Overview, Schedule, My classes, Journal and Automations, with a bottom navigation
bar on phones. No dashboard YAML, iframe, custom cards or manual resource
registration is required. Your browser needs access to HA only.

Upgrade the server to **1.49.0+** through Compose first, then update the
integration through HACS and restart HA. In **Settings → Devices & services →
Arbox → Configure**, choose users allowed to view and users allowed to perform
actions. Administrators have full access by default; action access includes
viewing. Other users start without panel access. Configure each Arbox connection
separately. The existing entity/service permissions are unchanged.

See the [panel installation and usage guide](dashboard/arbox-panel.md) for
permissions, notification links, multiple connections and troubleshooting.
The old Lovelace YAML examples have been removed; use the automatically installed
panel instead. Existing sensors, calendars and services remain available, and
previously installed dashboards are not deleted by an update. You can remove your
old dashboard from HA after switching to the panel. If you configured an iframe
with the same `/arbox` path, remove or rename it before using the panel.

## Security notes

- Runtime state (credentials, tokens, DB, settings) lives in the `/data`
  volume, outside any git worktree. Nothing sensitive is in this repo.
- HAR captures and probe responses contain live JWTs — `.gitignore` blocks
  `*.har`; keep it that way.
- Expose the server on localhost/tailnet only; the API key protects booking,
  not viewing.
