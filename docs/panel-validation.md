# Compact membership UI — server 1.52.0 / integration 3.3.0

- 156 server pytest tests pass, including authenticated calendar export matching
  the existing event, configured alarms and location.
- 38 Node tests pass: full-width quota allocation, unknown/zero capacity,
  standby/uncertain commitments, multiple filters, context guards, confirmation
  gates, feedback and journal behavior. Shared assets pass the bundle drift check.
- `scripts/check-membership-ui.cjs` runs the actual standalone frontend and HA
  web component against synthetic data only. It checks 1440, 390 and 320 px,
  light/dark mode, continuous quota bars, initially collapsed Studio cards,
  inline editing, draft preservation on polling, a scoped per-workout membership
  change, all 42 fixture classes, multiple coach selections and day/week/month.
  All checks passed with no page errors or real Arbox calls. Screenshots are
  written to ignored `tmp/ui-review/`.
- Run the browser check from the repository root with `node
  scripts/check-membership-ui.cjs` and an installed Playwright Chromium. Optional
  `PLAYWRIGHT_MODULE` and `CHROMIUM_PATH` reuse an existing local installation.
- This round tests the **HA component with a mock WebSocket transport**, not a
  running HA installation. The new fixed calendar read has an HA unittest ready
  for the existing workflow. Real-HA acceptance and HACS upgrade remain to be
  checked after publishing; no installed integration files were replaced.

The implementation only changes presentation and adds a read-only calendar
export for HA. Eligibility, quota allocation, booking windows and confirmation
gates remain on the server. No real bookings, cancellations or notifications
were performed during these checks.

---

# Full panel validation — v1.49.0 / integration 3.0.0

Validated against Home Assistant 2026.8.3 in an isolated local Podman container.
The production HACS installation was not modified manually.

- 124 server pytest tests: includes stale studio rejection, real studio switch
  waiting for a protected action, identity fallback and shared tick/sync locking.
- 15 HA unittest tests: real ConfigEntry install/unload/reload, actual options
  flow, authenticated WebSocket dispatch, view/action/admin grants, revocation,
  account separation, fixed routes and request coalescing.
- 19 Node tests: panel transport, explicit confirmation, no mutation retries or
  double writes, slow/unchanged polling, studio changes and legacy feedback.
- Shared feedback assets verified identical between server and integration.

## Browser walkthrough

Normal HA login, five screens, 390×844 phone and 1440×960 desktop, light/dark
HA themes. Inspected the accessibility tree, responsive cards, weekly grid,
native modal sheets and bottom navigation. Fixed sidebar overlap, panel height,
scroll reset on navigation and unchanged polling detaching focused controls.

Against the isolated synthetic backend (reachable only by HA's loopback, not the
browser), verified saved feedback prefill, saved notes plus a strength exercise
with sets/reps/weight appearing back in the journal, rule creation/toggle and
single-occurrence skip/restore with updated quota. These are fixture mutations,
not live Arbox bookings. Protocol/action and conflict logic have automated tests;
no real upstream booking/cancellation was performed as part of visual QA.

Read-only comparison to the existing production API confirmed field shapes for
summary, memberships/quota, mine, journal/history, rules and vacations. At the
time of comparison: 2/10 used, 5 scheduled and 3 automatic plans; all 8 plans
were present in `/api/me`. No production data was changed by this comparison.

Production acceptance after the normal HACS upgrade remains the final check of
the user's real phone, reverse proxy/VPN and current account data.
