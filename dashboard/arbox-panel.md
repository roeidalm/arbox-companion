# Arbox inside Home Assistant

The recommended dashboard is the **Arbox** panel installed by the integration.
It uses the existing Arbox server's schedule, quota and booking logic. HA serves
the interface and forwards a fixed set of authenticated requests to the internal
server. Your phone does not connect to Arbox directly.

## Install or upgrade

1. Deploy server **1.49.0 or newer**, using a published image in your Compose
   configuration. Keep the existing data volume and credentials.
2. Install/update Arbox through **HACS**, to the release containing integration
   **3.0.0 or newer**, then restart HA normally. Do not copy files into a running
   HACS installation.
3. For a new installation, add **Arbox** in **Settings → Devices & services**.
   Enter the server's internal URL and API key. HA must be able to reach that
   URL. `localhost` inside the HA container refers to HA's container itself.
4. Open the integration's **Configure** options as a HA administrator. Select
   users for **viewing** and for **booking and editing**. Save. Administrators
   always have access; users not selected have no full-panel access.
5. Open **Arbox** in the sidebar. Existing dashboard YAML, iframe or extra cards
   are not needed. If an old iframe already occupies `/arbox`, remove or rename
   that optional iframe configuration. Existing sensors and calendar remain.

HA **2024.8+** is required; integration tests run against **2026.8.3**. The repo
release tag and manifest versions are distinct: release **v1.49.0** contains
integration **3.0.0**. Update the server first so the new panel can validate the
active studio. An older server continues serving existing integrations, but
cannot serve the new full panel.

## Everyday use

| Screen | What you can do |
| --- | --- |
| Overview | See the next booking, memberships and quota, upcoming plans and feedback awaiting completion. |
| Schedule | Choose a date, browse a week, filter classes/coaches and open a class to book or join standby. |
| My classes | Follow bookings, standby, one-class schedules and recurring automation occurrences together. Cancel a booking or schedule, or skip/restore just one automatic occurrence. |
| Journal | Browse past training, filter feedback, change attendance and reasons, and edit ratings, notes and exercises. |
| Automations | Create/edit/toggle/delete booking or notification rules, and manage vacations. |

Class details open as a bottom sheet on phones and a side sheet on wide screens.
When several memberships are available, choose one or allow the server to choose.
Late cancellations, vacation overrides and over-quota schedules retain the
server's confirmation prompts. No failed or uncertain action is retried
automatically; check the refreshed state before choosing to retry.

## Connections and access

Permissions belong to each configured Arbox connection. **Action access includes
view access**. With several accessible connections, use the account selector;
their data is kept separate. The studio shown is the active studio selected on
the server, not a separate preference for each HA user. If that studio changes
while a form is open, the server rejects its action; refresh and reopen it.

These permissions cover full-panel WebSocket commands, including checks after
reads finish. Revocation takes effect on subsequent requests; cached data already
displayed is not a substitute for authorization. The permissions do not change
HA's existing entity/service access model.

The API key stays in HA. The bridge accepts only named, predefined operations,
never an arbitrary browser-supplied path or URL. Remote access requires your
normal authenticated HA access (VPN or HTTPS reverse proxy supporting WebSocket).
There is no requirement to expose Arbox publicly.

## Refresh and connection loss

Entering a screen, returning to the app and a successful action refresh the data.
Visible screens also reload every 30 seconds. These reads use the server's local
data; they do not trigger an upstream Arbox sync. **Refresh** explicitly requests
the existing upstream sync. The timestamp shows the last Arbox sync.

When a connection fails, the panel keeps the last data and open draft in memory
and displays the error. A browser reload discards unsaved drafts. Data refreshes
do not replace an open form. An action is successful only once the server confirms
it; a lost response can mean its outcome is unknown.

## Notifications and scope

Existing `/arbox-feedback#…` notification links still open the scoped feedback
form after HA login. Only **Arbox** appears in the sidebar. These expiring links
retain their existing permissions and do not grant general panel access. Follow
the [feedback notification guide](feedback-notifications.md) to configure phone
notifications; installing the panel does not enable notification channels.

Server connection secrets, maintenance and global exercise catalogue management
remain in their existing settings, outside this panel's first version.

## Troubleshooting

- **No sidebar item:** confirm HACS updated the integration and HA restarted;
  check integration load errors and an old iframe using the same path.
- **No authorized connection:** a HA administrator must add your user in the
  integration's options. Being logged into HA alone does not grant panel access.
- **Read-only:** ask the administrator to add action access for that connection.
- **Server unavailable / missing data:** check the URL/API key in HA and server
  version. A browser being able to reach the URL does not prove HA can reach it.
- **Studio changed:** reload the screen and reopen the class in its current
  context. Do not blindly repeat a mutation with an unknown outcome.
- **Notification opens Arbox directly:** enable the HA destination in Arbox
  settings, save, and send a new test. Older notifications keep their old URL.

Release images are built and published by the GitHub tag workflow. Server
deployment uses Compose; integration upgrades use HACS and HA's normal restart.

### Calendar and journal (integration 3.1.0)

Schedule and My classes offer day, Sunday-based week and month views; My classes also opens with all future dates. Booked classes, waiting lists, blue scheduled bookings, purple automatic bookings and skipped dates have distinct labels and colors. Month previews prioritize your bookings and plans. Dates without published classes remain empty.

Memberships show the server-provided quota for each subscription, alongside the aggregate used, booked, scheduled, automatic and remaining counts. Expand or collapse membership details as needed.

The journal includes feedback completion, attendance and rating summaries. Expand filters for dates, search, class, coach, attendance, feedback and exercise. Coach and exercise summaries open on demand; exercise details show comparable metrics and recorded measurements. Filters describe the displayed history, not your subscription quota.

Update through HACS and restart Home Assistant; no YAML or resource registration is needed. The existing server API (1.49.0+) supports these views.
