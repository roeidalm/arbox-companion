# Arbox read-only API (schema 1)

Each installation runs its own server. Cloudflare Access may protect external
requests, but the API also requires a separate read credential. Nothing in this
API books, cancels, probes eligibility, refreshes upstream data, or writes planning
state. GET projects the current local database. Existing administrator routes and
Home Assistant authentication are unchanged.

## Connecting

Open `/view` in Arbox. An existing administrator browser can use its saved admin
key. Under read-access management create a read key and copy it once. Creating a
replacement invalidates the previous key. The administrator can revoke it.
Store the key as a secret in the consuming service, never in a URL or browser JS.

```
GET http://arbox-server:8000/api/view/summary
X-Api-Key: <read-key>
```

`arbox-server:8000` works only from containers sharing a Docker network with it.
Use the deployment's internal address otherwise. Keep the external browser URL
as the widget click target. Do not bypass Cloudflare for an external API request;
external clients also need the deployment's Cloudflare machine authentication.
Polling every 60 seconds is appropriate; it does not trigger an upstream refresh.

The key grants all view sections for the currently selected studio (events are
explicitly instance-wide). It is not anonymous/public access: workout schedules,
card balances and feedback ratings are personal data. No passwords, API keys,
OAuth credentials, contact information, raw upstream payloads, feedback prose,
exercise notes or event message/detail text are exposed. Event metadata includes
id, timestamp, level, allowlisted source and related schedule ID; unknown sources
become `other`. Raw logs remain administrator-only.

## Endpoints

All collections are wrapped as `{meta, data}`. `meta` includes `schema_version`,
`generated_at`, `last_sync`, `age_seconds`, `stale`, `timezone`, `studio_id` and
`app_url`. Missing source timestamps are stale; over 900 seconds is stale.
Generation time is NOT source freshness. A stale reading is not a zero reading.

- `GET /api/view`: section discovery, schema version and read-only capability.
- `/summary`: `next_class` (booked only, or null), `next_7_days` counts for booked,
  planned and standby; `memberships`, `attention`, `automations.enabled`,
  `google_calendar`, `notifications`, `event_counts_7_days`.
- `/sessions`: `sessions`, including available classes and mutually exclusive
  `state`: booked, standby, planned, available. Automatic plans are included.
- `/calendar`: `sessions` restricted to commitments/plans.
- `/memberships`: `memberships`, `attention`. Quota figures use the existing ledger
  for the current month; `period_start/end` show the actual card/month/week period.
  Use `available_after_planned` per card and `verification_state`; null is unknown,
  not zero. Never sum unrelated membership periods into an invented balance.
- `/automations`: `automations`: rule id, enabled, coaches, categories, weekdays,
  time_from/to and mode. User-supplied rule names are omitted.
- `/history`: `history`: persisted attendance/cancellation outcomes, without reasons.
- `/reviews`: `reviews`: session/date/category/coach and coach/class feedback enum,
  with updated_at. No free-text notes or exercise records.
- `/events`: `events`, `scope: instance`, `has_more`, `next_after_id`. Ascending IDs.
  Persist the cursor and request `?after_id=<cursor>&limit=100` until has_more=false.
  Delivery is bounded by the server's event retention. This is metadata, not a
  lossless raw-log export. Do not assume continuity across a database restore.
- `/status`, `/notifications`: sanitized Google status, enabled notification
  channels and seven-day event severity counts. No channel destinations or errors
  copied verbatim from external systems.
- `/settings`: timezone, calendar alarms, journal level and retention values.
- `/studio`: selected studio id/name. Does not discover or switch studios.

Collection pagination: `limit` 1–500, `offset` 0–100000, with total/has_more.
Events use after_id instead of offset. Summary is intentionally not paginated.
Date filters: date_from/date_to (YYYY-MM-DD), maximum 366 days. Sessions/calendar
start today and end in 30 days; history/reviews default to previous 30 days.
The summary's next_7_days is today through the next six dates in the studio zone;
today's already-started sessions are excluded. Dates/times in rows are local to
meta.timezone. Empty arrays mean no results; null means unknown/not available.
Optional X-Arbox-Studio-Id asserts context; mismatch returns 409.
401 means missing/invalid key; 409 means missing/changed studio; 422 invalid filters.
`refresh` is explicitly rejected. All data responses use Cache-Control: no-store.

## Homepage example

Use server-side Custom API configuration and secret substitution supported by
your installed Homepage version. Replace the credential placeholder via its
secret mechanism; do not commit a real key.

```yaml
- Arbox:
    href: https://arbox.onesmartman.com/
    widget:
      type: customapi
      url: http://arbox-server:8000/api/view/summary
      refreshInterval: 60000
      headers:
        X-Api-Key: <READ_KEY_FROM_SECRET_STORE>
      mappings:
        - field: data.next_class.category_name
          label: האימון הבא
        - field: data.next_7_days.booked
          label: רשומים השבוע
        - field: data.next_7_days.planned
          label: מתוכננים
        - field: data.attention.action_required
          label: דורש פעולה
```

Handle next_class=null and request failures explicitly; do not show failure as 0.
Use meta.last_sync for freshness. Do not recompute quotas in Homepage/Grafana.
Grafana needs a suitable JSON API datasource/plugin and server-side credentials;
this endpoint is JSON, not Prometheus exposition format.

## Administrative key lifecycle

Only the existing ADMIN X-Api-Key can call these routes:
- GET /api/view-access → enabled boolean (never returns the key)
- POST /api/view-access → rotates/creates and returns `key` once
- DELETE /api/view-access → revokes

Read keys cannot call administrative endpoints or existing write APIs.
