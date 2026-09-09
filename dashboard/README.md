# Home Assistant setup resources

The Arbox interface is installed automatically by the HACS integration. No
Lovelace dashboard YAML, iframe or extra cards are required.

- [Panel installation and usage](arbox-panel.md)
- [Phone notification and feedback setup](feedback-notifications.md)
- [Importable phone notification blueprint](arbox-notification-blueprint.yaml)
- [Notification and callback automation example](arbox-callback-automation.yaml)

The notification files remain at their existing URLs so blueprint imports keep
working. They are separate from the panel and are still needed for the notification
setup described in the guide.

The legacy dashboard examples have been removed. An integration update does not
remove dashboards you previously added in HA; after switching to the Arbox panel,
you can remove those old dashboards yourself. Sensors, calendars and services
remain available for your own automations and dashboards.
# Updating planning notification actions (server 1.53.0)

The existing webhook and callback automations also support planning corrections.
The server supplies opaque, expiring buttons scoped to the active account,
studio, workout and membership. Confirmation checks current eligibility and
capacity; it does not place an upstream booking immediately.

Re-import **arbox-notification-blueprint.yaml** if you use the blueprint. If you
copied the automation manually, edit **Arbox: notification webhook** in HA and
replace the nested notification `data` value with:

```yaml
data: >-
  {% set payload = dict(actions=trigger.json.actions | default([])) %}
  {{ dict(payload, tag=trigger.json.tag, alert_once=trigger.json.alert_once | default(false))
     if trigger.json.tag is defined else payload }}
```

Keep your existing webhook ID, notification service, title and message. Keep
the callback automation forwarding `ARBOX_` actions to `/api/ha/callback` with
`X-Api-Key`. A planning step reuses its notification tag; ordinary unrelated
messages have no shared tag. Telegram updates its source message automatically.
HA replacement follows the Companion app's normal platform behavior. These
changes require no public access to Arbox and no manual HACS file replacement.
