# Friends: verified read surface and pending native-client contracts

Implemented locally: web and HA Friends tab, current relationships with safe
photos, local filtering, collapsed future sessions, and badges beside roster
buttons. Opening a session uses the existing schedule/manual-booking UI. No new
background jobs, notifications, booking mutations, or persistent social tables.

`GET /api/friends?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD` requires the API key,
supports the existing studio header/lock, and returns `Cache-Control: no-store`.
Dates default to studio today through 14 days later, with a maximum span of 42
days inside the next 62 days. Only cached booking cards in that interval are
linked. A missing class is not evidence that a friend has no other bookings.

Reads use the owning connection's `/api/v2/user/profile`, not the merged account
profile. `friend_connection[].user.users_boxes_active` is studio-filtered.
Deleted, self, unrelated, mismatched-user and unscoped relationships are omitted.
`booked_users[].id` is matched to the other member's ID after roster visibility
checks; an explicitly empty/withheld preferred roster never falls back to
`schedule_user`. Cancelled entries and cross-studio/location data are excluded.
No names-based matching, emails, phone numbers or arbitrary avatar URLs.

## Not implemented or enabled

The observed numeric relationship status `1` has **not** been verified to mean
accepted. All relationships are labelled `unverified`; the UI explicitly says
"connection", not "accepted friend". Incoming/outgoing classifications, global
friend search, send/accept/reject/cancel/remove operations are not exposed.
`capabilities.manage`, `search`, and `classified_status` remain false. Do not
enable them by changing flags or guessing endpoints.

To finish, capture native-client requests/responses for: list friends; list
incoming and outgoing requests; search; send; cancel outgoing; reject incoming;
accept incoming; view friend's profile and sessions; remove. Preserve correlated
pseudonymous IDs and response statuses but strip credentials, cookies and tokens.
Validate both directions and accepted/pending/deleted states. Add scoped,
authenticated actions only for these observed contracts; no automatic retries of
ambiguous writes, and require explicit UI confirmation before removing a friend.
Update the UI, HA action allowlist, capability flags and tests together.

The existing saved August HAR contains booking requests, not these contracts.
Do not use real people as test recipients. Until native evidence is supplied,
this is a partial implementation, not completion of the full approved plan.
