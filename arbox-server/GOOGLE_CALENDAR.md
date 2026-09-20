# Optional Google Calendar

Settings → Calendar contains setup, JSON upload, OAuth consent and preferences.
The connection stays off until the user explicitly creates/enables the calendar.
The existing ICS downloads and Google add-event links continue to work.

## Server setup

Provide a valid HTTPS address reachable by the user's browser. Configure
`GOOGLE_CALENDAR_REDIRECT_URI=https://your-server/api/calendar/google/callback`
in the container environment. Its hostname must match Settings → Advanced →
Server address. The wizard shows this exact address for the Google Web client.
A tailnet-only HTTPS reverse proxy works when the browser is on that tailnet;
Google's servers do not need inbound access to the callback. Preserve the original
HTTP UI address if existing clients use it: OAuth starts on HTTPS, sets its secure
browser-binding cookie there, and returns to the configured UI address.

## Google setup

The wizard links to project creation, Calendar API enablement, consent branding,
External test audience, data access, client creation and an explicit Production step. Download the Web OAuth
client JSON and upload it; do not manually paste secrets. Select the exact HTTPS
callback from the file. The OAuth request asks for `calendar.app.created` and
`openid email`. This creates and manages a dedicated secondary calendar, without
access to unrelated calendars. The email identifies the connected Google account.

An External app in Testing has refresh tokens valid for seven days for this
scope. For ongoing use, select Audience → Publish app and confirm In production.
If publishing is disabled, complete Branding using real homepage, privacy policy
and terms URLs for your deployment. Reconnect here after changing to Production;
reuse the same client JSON and calendar. Personal-use verification exceptions may
apply, but Production removes the test-user allowlist: restrict access to your
server separately. The wizard cannot read the project publishing status and does
not claim to verify it; Google's verification requirements still apply. An expired/revoked token
shows a reconnect message. Calendar creation is a separate explicit UI action.

## Behavior

- Each Arbox account and selected studio has separate credentials/preferences,
  Google account identity, secondary calendar and event mappings. This is the
  existing single-account server model, not independent browser-user accounts.
  The scheduler syncs the active Arbox account/studio only.
- Future sessions in the next 30 days are projected every minute. Registered and
  waiting-list commitments take precedence over planned/automatic occurrences.
- Event colors, free/busy and up to five popup reminders are configurable per
  status. Registered workouts default to reminders 60 and 30 minutes before.
- State changes update the same event. Removing a plan or excluding its status
  removes its future event. Past events remain untouched. Pause/disconnect leaves
  existing events in place and stops updates; disconnect drops local OAuth tokens.
- This is one-way synchronization. Edit bookings in Arbox Companion. Periodic
  reconciliation (15 minutes) restores manually changed/deleted managed events.
- Stable event IDs make uncertain insert responses retryable without duplicates.
  Deleted IDs are retired before replanning. Google calendar creation has no
  idempotency key: an uncertain response pauses creation, and the UI can recover
  the existing calendar by ID after verifying its connection marker.
- Secrets live in `/data/google-calendar.json` (atomic writes, mode 0600), never
  in status responses, frontend assets or repository files. Include this file in
  private backups. OAuth state is one-use, expires after ten minutes, bound to a
  secure HttpOnly browser cookie and account/studio context, and uses PKCE.

References: [Google OAuth web flow](https://developers.google.com/identity/protocols/oauth2/web-server),
[Calendar scopes](https://developers.google.com/workspace/calendar/api/auth),
[Event insertion](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert).

### Internal and external addresses

Keep `base_url` as the internal browser address and optionally configure
`external_url` for links opened from notifications. HA's configured API endpoint
is independent. Both configured hostnames are allowed by the server.

To move an existing Google connection, add the new HTTPS callback to the same
Google OAuth client first. In Calendar settings, update the callback and reconnect
to verify it. This preserves the managed calendar and event IDs. Do not disconnect
or upload a different OAuth client just to change the server address. Reverse
proxies must preserve the original Host header. Cloudflare Access must allow the
signed-in browser through the start and callback routes; background Google sync
uses outbound API calls and does not need a public authentication bypass.
