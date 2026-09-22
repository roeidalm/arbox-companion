# Registration learning and optional waiting

Both switches default to off. Enabling learning alone leaves booking timing unchanged.
Settings → Registration and learning controls both independently. The settings persist
in settings.json under registration_timing. No container or external service is needed.

Broad learning observes every known class in the active studio during its configured learning window (1–120 minutes, default 30)
 after the effective registration opening (including membership advance).
Otherwise only pending pins and active autobook matches are sampled. A randomized 20–40 second
scheduler batches candidates into one schedule request per tick, with a 15-second
request timeout. Unknown openings are excluded. Existing booking/sync jobs are retained.

Samples store only class identifiers, timing, aggregate occupancy/capacity and whether
our own booking was present. They do not update the booking cache or eligibility.
SQLite appends observations and sampling outcomes without automatic age deletion.
Each opening snapshots its settings; future changes do not reinterpret old observations.
Learning continues after an occupancy threshold or our own booking is observed,
but stops permanently for that opening at the first observation of full capacity.
Later cancellations do not restart learning; existing booking/waitlist monitoring is unaffected.
A closing observation can occur up to 55 seconds after the chosen window.
The authenticated GET /api/registration-learning endpoint returns history and controls.
Incomplete or failed reads are recorded as such; missing data is never treated as zero.
A complete window starts within 55 seconds, ends at or after the configured window and has no gap
longer than 75 seconds. Reported crossings are first observations, not exact signup times.
Two weeks is an initial recommendation, not a promise of sufficient predictive history.

With waiting enabled, all scheduled/autobook targets inherit the global occupancy
threshold (1–50%) and timeout (1–10 minutes from opening, default 10). Book at the first observation
of occupancy >= threshold, remaining capacity <= threshold, or timeout. Missing/stale
occupancy (>60 seconds) falls back to the existing immediate booking flow. This cannot
guarantee a place: occupancy can jump between samples. Eligibility and existing booking
guards still apply. Manual booking buttons remain immediate.

Overrides use session:<studio>:<schedule> or rule:<studio>:<rule> keys, with immediate
or wait mode and optional threshold/timeout. Session overrides take precedence; among
matching rule overrides the lowest rule id wins. Pins use global/session settings.
The global waiting switch overrides all exceptions when off. Learning never turns
waiting on automatically. With waiting enabled, sampling ticks also reevaluate pending
bookings; with learning alone, they never invoke booking ticks.

Reports include each timestamp, occupancy percentage and counts, plus a replay against
the policy saved at opening. Replay outcomes (threshold, timeout, incomplete, pending)
are explicitly simulations, not reasons for an actual immediate booking. A threshold
first seen after the deadline is ambiguous and cannot prove a timeout. Actual waiting
releases are recorded separately and do not claim booking success. Legacy samples
without policy snapshots remain available and are labeled unknown-policy.

Fast-fill warnings use observed evidence: full in the first observation within 55 seconds,
or an observed threshold-to-full margin of at most 55 seconds without gaps over 75 seconds.
This includes sampling (up to 40s) plus request time (up to 15s). Warnings recommend
immediate booking but never change settings or promise a forecast. A late first full
observation alone is labeled insufficient evidence of speed. Reports count coverage
until the first full observation or the configured window, whichever comes first.


## Contextual guidance

GET /api/registration-guidance provides a read-only, authenticated summary for future
sessions and current rules, reusing matching studio/category/coach/series/weekday/time
cohorts. Rule summaries deduplicate history across future occurrences. Inline warnings
re-evaluate stored observation points against the current threshold; original reports
and policy snapshots stay unchanged. No guidance request performs a booking or changes
settings. Missing history and failed loads are shown distinctly.

My Classes, automation cards/editor and the scheduling dialog show the effective mode,
evidence and a link to matching histories. Inline choices use authenticated, studio-scoped
PUT /api/registration-policy/{session|rule}/{id} with immediate or inherit mode. They do
not enable global waiting or trigger a booking. Pin creation accepts optional timing_mode
and applies it after preflight checks, before any immediate watchlist execution. Cancelling
the dialog writes nothing. Existing API clients omit the new field and retain behavior.
