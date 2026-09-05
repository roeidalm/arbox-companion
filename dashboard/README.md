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
