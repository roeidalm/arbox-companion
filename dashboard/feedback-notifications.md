# Workout feedback inside Home Assistant

The phone opens the form in HA. HA reads and saves feedback through its internal
connection to Arbox. The phone does **not** need direct access to Arbox, and Arbox
does not need a public URL. Your existing remote access to HA (VPN or HTTPS through
Traefik, for example) must work, including HA's authenticated WebSocket connection.
Telegram still opens the direct Arbox page.

## What needs to be installed?

| Component | Requirement | Where it is installed |
| --- | --- | --- |
| Arbox server | **1.48.1 or newer**, configured with your Arbox account | Your server/container host; upgrade the published image through your Compose deployment |
| Home Assistant | **2024.8 or newer** | Your existing HA installation |
| Arbox integration | **2.9.0 or newer** | Install/update this repository through HACS, then restart HA |
| HA Companion app | Registered with your HA, with notifications allowed | Your phone |
| Notification automation | One webhook-to-phone automation | In HA; use the existing Arbox automation or import the blueprint below |

**The feedback panel is included in the integration.** There is no separate custom
card, frontend resource, dashboard YAML or add-on to install. A separate HA
long-lived token is not required. The integration uses the Arbox API key already
configured in HA; that key is never sent to the phone.

### What creates the screen?

This is a **custom HA panel**, not a Lovelace dashboard or card. When the Arbox
integration loads, [`feedback.py`](../custom_components/arbox/feedback.py)
registers `/arbox-feedback`, static frontend assets and the
authenticated feedback WebSocket commands.
[`frontend/panel.js`](../custom_components/arbox/frontend/panel.js) renders the
form inside HA using the shared form module and stylesheet. HACS installs these
files as part of the integration. The notification blueprint only delivers the
button that opens this panel; it does not create the screen.

The repository release tag and integration version are different: release
**v1.48.1** includes integration **2.9.0**. A release/update screen may show the
release tag rather than the manifest version.

## First-time installation

1. Deploy and configure the [Arbox server](../deploy/README.md). Keep persistent
   `/data` storage. HA must be able to reach its internal address.
2. In HACS, add `https://github.com/roeidalm/arbox-companion` as a custom repository
   with type **Integration**, then download Arbox. Restart HA.
3. In HA **Settings → Devices & services → Add integration**, select **Arbox**.
   Enter the server URL and Arbox API key from the server's initial setup.
   Use `http://arbox-server:8000` only when HA and Arbox share a container network;
   otherwise use a server address reachable from HA. `localhost` inside the HA
   container does not refer to another container.
4. Register the Companion app with HA and set up the [notification automation](#new-home-assistant-notification-installation)
   below. If an Arbox webhook automation already exists, reuse it.
5. Complete **Enable and verify** below. Installing the integration alone does
   not switch existing notification links to HA.

## Upgrading an existing installation

1. Upgrade the Arbox server to **1.48.1 or newer** using your existing Compose
   deployment and a published image. Keep the data volume.
2. Update Arbox through **HACS** to the release containing integration **2.9.0 or
   newer**, then restart HA normally. No manual replacement of live integration
   files is needed. Existing integration credentials and notification automations
   can stay in place.
3. Complete **Enable and verify** below. This is required even after a successful
   HACS update.

## Enable and verify — do not skip the Save step

1. Refresh the Arbox **Settings** page after upgrading.
2. In its **Home Assistant** section, enable **Open feedback inside Home Assistant**
   (**פתיחת טופס המשוב בתוך Home Assistant**), then **Save settings**.
   This option defaults **off** to keep older integrations working; a HACS update
   does not enable it automatically. Testing uses the **saved** destination.
3. Under **Tracking**, select Feedback or Full and press **Try in HA**.
   Rehearsals work even when regular HA notifications are disabled.
4. Open the **new notification**, expand it and tap **Fill feedback**. Old
   notifications retain their original links. The new destination is
   `/arbox-feedback#…` on the Companion app's current HA server, not an Arbox URL.
5. The form should show the workout and a rehearsal banner. Try ratings, optional
   notes and exercises, then save. The confirmation stays in the form and **no
   workout or attendance data is saved for a rehearsal**.
6. For real prompts, enable the HA notification channel and its **journal** kind,
   select an active tracking level, put HA first among enabled journal channels,
   and save. Real prompts go to the first eligible channel only; a successful
   rehearsal does not enable regular notifications automatically.

Starting with integration **3.0.0**, the single sidebar item **Arbox** opens the
[full panel](arbox-panel.md). `/arbox-feedback#…` remains available for existing
and new notification links, without a second sidebar item. Opening that URL
without an invitation shows guidance to open a workout notification. Token form
requests still require both an authenticated HA session and the private,
expiring workout link; full-panel user permissions do not broaden token access.

## Troubleshooting

| What you see | What to check |
| --- | --- |
| The notification still opens the Arbox address | Enable **Open feedback inside Home Assistant**, **save**, and send a **new** test. Refresh an old settings tab before saving again. |
| HA opens but the panel is missing / the WebSocket command is unknown | Confirm Arbox integration **2.9.0+**, restart HA after the HACS update, and refresh/reopen Companion. |
| The panel says to open a notification | Open **Fill feedback** from the new notification; the bare sidebar link has no workout token. |
| HA cannot reach Arbox | Check the integration's internal server URL and the network path from HA to Arbox. The phone's network path to Arbox is irrelevant in HA mode. |
| It works at home but not remotely | Confirm Companion can open and use HA remotely. The VPN or HTTPS reverse proxy must also pass HA's WebSocket connection. |
| Two notifications arrive | Check for duplicate webhook automations. Do not import another blueprint automation alongside an existing listener. |
| The test works but regular prompts never arrive | Enable the HA channel, journal notifications and tracking; check channel order and save. Tests deliberately bypass normal channel enablement. |

## Existing Arbox notification installation

If your webhook automation already passes `trigger.json.actions` into the
phone notification's `data.actions`, nothing needs to change. Do **not** add a
second notification automation: two listeners on the same webhook can produce
duplicate notifications. The checked-in
[automation example](arbox-callback-automation.yaml) already supports URI links.

For direct Arbox forms (the default while the HA-hosted setting is off):

1. Set Arbox **Settings → Server URL** to the address the phone can open,
   including port if necessary. This is the Arbox address, not HA's address.
2. Enable the HA channel and its journal notifications if you want real prompts.
   Choose HA first in notification order if both HA and Telegram are enabled;
   the journal sends one prompt through the first eligible channel.
3. Under **Tracking**, select Feedback or Full and press **Try in HA**.
   Expand the notification, tap **Fill feedback**, and submit the form.
   The form explicitly says it is a rehearsal and saves no workout data.

## New Home Assistant notification installation

1. Import the [Arbox phone notification blueprint](arbox-notification-blueprint.yaml)
   through HA **Settings → Automations & scenes → Blueprints → Import blueprint**
   using this import URL:
   `https://github.com/roeidalm/arbox-companion/blob/main/dashboard/arbox-notification-blueprint.yaml`.
2. Create an automation from it. Pick a long random private webhook ID and your
   phone's `notify.mobile_app_…` action.
3. Set Arbox's HA webhook URL to
   `http://HOME_ASSISTANT:8123/api/webhook/YOUR_PRIVATE_WEBHOOK_ID` using an
   address reachable from the Arbox server. The blueprint accepts local webhook
   calls only. A phone-reachable Arbox Server URL is needed only for direct
   links (including Telegram), not for the HA-hosted form.
4. Test as above. Booking/cancellation buttons additionally need the callback
   automation and `rest_command` in [the complete example](arbox-callback-automation.yaml).
   HA-hosted feedback submissions pass through the integration to Arbox; direct
   forms go straight to Arbox. Neither needs that callback automation or a
   separately configured long-lived HA token.

## Storage and privacy

Real submissions update the existing SQLite `workout_journals` and
`workout_exercises` records and mark the workout attended. Find and edit them
in **Journal**. Opening the form alone changes no workout data. Saving shows a
confirmation in the page; it never triggers another phone notification.

Links are private, scoped to one workout/studio, expire after seven days, and
are consumed by a successful save. Rehearsal links expire after 30 minutes.
They survive server restarts. The opaque capability is kept in the fragment
(`…/feedback#…`) and submitted in `X-Feedback-Token`, never in a query parameter.
The global API key is never included. With direct links, the phone needs network access to Arbox. With HA-hosted
forms, only HA needs that access; the phone connects to HA. The capability is
forwarded over HA’s authenticated WebSocket connection and then the internal
request header. Neither mode opens network access or includes the global API key.
