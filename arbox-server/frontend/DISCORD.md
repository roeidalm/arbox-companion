# Discord notifications

Settings → Notifications: enable Discord, enter a channel webhook URL, choose event
kinds and save. The test button saves first. The secret is masked in settings and
never included in the read-only API. For managed deployments mount a read-only
secret file and set `ARBOX_DISCORD_WEBHOOK_URL_FILE` to its path. Alternatively set
`ARBOX_DISCORD_WEBHOOK_URL`. File configuration has priority; a missing file does
not silently fall back to a different destination. Keep Telegram enabled while
validating the new channel. Disable Discord to roll back without changing others.

All ordinary events go to every enabled, configured channel selected for that
kind: digest, automatic booking, standby, studio, cancellation deadline, system
logs, vacation, attendance and memberships. Workout journal is separately opt-in.
Log severity remains configurable per channel. The order can be dragged or moved
with arrow buttons. Zero escalation minutes preserves broadcasting. A positive
value sends an actionable prompt to the first eligible channel, then sends to the
remaining channels if its existing callback is still unanswered. Immediate failure
tries the next channel without waiting. The escalation timer remains in memory,
as before; restarting loses that reminder timer, not Discord's delivery queue.

Discord's incoming webhook cannot receive action callbacks. URI buttons become
links and ordinary callback buttons become a link to the application's My page.
Telegram/HA callbacks remain available. Opening Discord's link does not acknowledge
a Telegram/HA prompt; its timed reminder can still follow. Journal forms keep their
existing limited form token and can be submitted through the linked form. Calendar
file notifications use their existing download link rather than uploading a file.
Existing channel-specific interactive reply/edit flows remain Telegram/HA-only.

Discord messages are escaped and have mentions disabled. Parts are <=1800 UTF-16
code units. Webhook endpoints are validated HTTPS Discord URLs; redirects are
rejected. Each delivery has a UUID event identity separate from Discord's string
message ID, and a hashed destination identity. `(event_id, destination, part)` is
unique. A new invocation is a new event; this does not deduplicate independently
repeated business events. Booking/cancellation code is never called by delivery.

Delivery records persist in `/data/discord-delivery.db` (0600). A queued response
means accepted for later delivery, not confirmed received. Discord 429 and transient
server/connect failures get at most three attempts, respecting Retry-After with
jitter. Delayed notifications expire after an hour. A timeout or interrupted send
is marked uncertain and is not automatically retried. HTTP 5xx retries can still
produce duplicates if Discord accepted the original request; delivery is not
exactly-once. Rotation never sends an old queued event to a different webhook.
Disabling the channel pauses queued delivery. Terminal records retain sanitized
metadata for 30 days (pruned at startup); their message bodies are removed.

System status shows queued, failed and uncertain counts; the event log explains
sanitized failures. No raw HTTP exception or response body is logged for Discord.
