"""Arbox server — FastAPI app wiring.

DATA_DIR (default /data) holds credentials.json, settings.json and arbox.db —
all runtime state, all outside any git worktree.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .api import router
from .feedback_form import router as feedback_router
from .arbox_client import ArboxClient, ArboxError
from .notify import Notifier
from .rules import RulesEngine
from .settings import Settings
from .store import Store
from .sync import Syncer

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
_LOGGER = logging.getLogger(__name__)

DATA_DIR = os.environ.get("DATA_DIR", "/data")
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")


async def _startup_sync(store, syncer, rules_engine=None) -> None:
    """Window sync on boot, skipped when the last one is fresh (<30 min)."""
    if rules_engine:
        # seeds the studio-message dedupe list on the very first ever run
        try:
            await rules_engine.studio_messages_tick()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Startup studio-message check failed: %s", err)
        try:
            await rules_engine.membership_check()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Startup membership check failed: %s", err)
    fresh = False
    last = await store.get_meta("last_sync")
    if last:
        try:
            fresh = datetime.now() - datetime.fromisoformat(last) < timedelta(minutes=30)
        except ValueError:
            pass
    if fresh:
        _LOGGER.info("Startup sync skipped — last sync %s is fresh", last)
    else:
        try:
            await syncer.window_sync()
        except ArboxError as err:
            _LOGGER.error("Startup sync failed (will retry on schedule): %s", err)
            await store.log_event(
                "error", "sync", "סנכרון בעלייה נכשל", f"{err} · ננסה שוב בתזמון הבא")
    # Always re-arm, even when the sync was skipped: the one-shot grab jobs
    # live in the scheduler, which a restart empties. Returning early here
    # meant a deploy within 30 minutes of a sync left every pending grab
    # unarmed until the next half-hourly sweep — and a restart shortly
    # before an opening would miss it entirely, falling back to the 5-minute
    # tick, which for a class that fills in seconds is far too late.
    if rules_engine:
        await rules_engine.schedule_openings()
        # Establish attendance tracking immediately and catch a prompt that
        # became due while the container was restarting; do not wait for the
        # first one-minute scheduler interval.
        try:
            await rules_engine.attendance_tick()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Startup attendance check failed: %s", err)
        try:
            await rules_engine.journal_tick()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Startup journal check failed: %s", err)


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(DATA_DIR, exist_ok=True)

    settings = Settings(DATA_DIR)
    # before anything reads a clock: the scheduler's triggers, every
    # datetime.now(), and SQLite's 'localtime' all follow the process TZ
    _LOGGER.info("Timezone: %s (now %s)", settings.apply_timezone(),
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    store = Store(os.path.join(DATA_DIR, "arbox.db"))
    await store.open()
    client = ArboxClient(DATA_DIR, whitelabel=os.environ.get("WHITELABEL", "Arbox"))
    notifier = Notifier(settings)
    notifier.log_event = store.log_event
    store.on_event = notifier.push_event
    syncer = Syncer(client, store, settings=settings)
    notifier.studio_context = lambda: (syncer.studio_name, syncer.studio_count)
    rules_engine = RulesEngine(store, client, syncer, notifier)
    syncer.on_standby_promoted = rules_engine.on_standby_promoted

    app.state.settings = settings
    app.state.store = store
    app.state.client = client
    app.state.notifier = notifier
    app.state.syncer = syncer
    app.state.rules_engine = rules_engine

    # APScheduler's default misfire_grace_time is one second: a paused VM, a
    # redeploy, or a busy host at 20:00 discards the digest for that day
    # rather than running it late. An hour of grace is the difference between
    # "the message came a bit late" and "there was no message and no error".
    scheduler = AsyncIOScheduler(
        timezone=settings.timezone,
        job_defaults={"misfire_grace_time": 3600, "coalesce": True},
    )
    # Explicit ids on every cron job: _reschedule_cron matches by id, and
    # APScheduler's generated ids are uuid4 hex, which no name can match — a
    # timezone change used to move only the two jobs that had ids.
    scheduler.add_job(syncer.window_sync, CronTrigger(hour="7,15"),
                      id="window_sync")
    scheduler.add_job(syncer.nightly_roll, CronTrigger(hour=3, minute=5),
                      id="nightly_roll")
    scheduler.add_job(syncer.near_term_sync, IntervalTrigger(minutes=30))
    scheduler.add_job(syncer.mid_range_sync, IntervalTrigger(hours=2))
    scheduler.add_job(
        syncer.far_range_sync,
        CronTrigger(hour=(settings.digest_hour - 1) % 24, minute=30),
        id="far_range_sync",
    )
    scheduler.add_job(
        rules_engine.membership_check,
        CronTrigger(hour=(settings.digest_hour - 1) % 24, minute=0),
        id="membership_check")
    scheduler.add_job(
        rules_engine.nightly_digest,
        CronTrigger(hour=settings.digest_hour, minute=0),
        id="digest",  # rescheduled live by /api/settings when the hour changes
    )
    # Two minutes after the nightly message: on the first evening its future
    # registration section goes quiet, explain why. Existing-class reminders
    # may still have arrived at 20:00; on a vacation's last day this rides just
    # below them.
    scheduler.add_job(
        rules_engine.vacation_announce_tick,
        CronTrigger(hour=settings.digest_hour, minute=2),
        id="vacation_announce",
    )
    scheduler.add_job(rules_engine.autobook_tick, IntervalTrigger(minutes=5))
    # pinned classes: same 5-min safety net, plus exact-moment jobs below
    scheduler.add_job(rules_engine.watchlist_tick, IntervalTrigger(minutes=5))
    # refresh the one-shot opening jobs as the window rolls forward
    scheduler.add_job(rules_engine.schedule_openings, IntervalTrigger(minutes=30))
    # fast standby watch: no-ops instantly unless a waitlist spot is held
    scheduler.add_job(rules_engine.standby_watch_tick, IntervalTrigger(minutes=5))
    scheduler.add_job(rules_engine.studio_messages_tick, IntervalTrigger(hours=1))
    # no-ops instantly unless a pre-class reminder is configured
    scheduler.add_job(rules_engine.class_reminder_tick, IntervalTrigger(minutes=5))
    # Local-only check: one-minute cadence makes the yes/no question land
    # within the five minutes before class without adding any Arbox traffic.
    scheduler.add_job(rules_engine.attendance_tick, IntervalTrigger(minutes=1))
    # Post-class journal is opt-in and local-only; five minutes is precise
    # enough around the fixed 30-minute delay without extra Arbox traffic.
    scheduler.add_job(rules_engine.journal_tick, IntervalTrigger(minutes=5))
    # no-ops instantly unless a late-cancel lead time is configured
    scheduler.add_job(rules_engine.late_cancel_tick, IntervalTrigger(minutes=5))
    scheduler.start()
    app.state.scheduler = scheduler
    rules_engine.scheduler = scheduler

    notifier.start_telegram_poller()

    if client.configured:
        # in the background: the HTTP server (health, UI, HA) must not wait
        # on Arbox, and a restart right after a sync shouldn't re-pull at all
        startup_sync = asyncio.create_task(_startup_sync(store, syncer, rules_engine))
    else:
        startup_sync = None
        _LOGGER.info("No credentials yet — waiting for /api/setup")

    yield

    if startup_sync:
        startup_sync.cancel()

    scheduler.shutdown(wait=False)
    await notifier.close()
    await client.close()
    await store.close()


app = FastAPI(title="Arbox server", lifespan=lifespan)


@app.middleware("http")
async def _host_guard(request, call_next):
    """Refuse requests whose Host header is not this server.

    Without it, any page the user visits can rebind its own hostname to this
    server's address and read the open endpoints from inside the tailnet —
    the browser treats it as same-origin, so the VPN boundary the security
    model rests on does not apply.

    Deliberately permissive until base_url is set: it defaults to empty on a
    fresh install, and an allowlist built from it would lock the user out of
    their own setup page before they could fill it in.
    """
    configured = request.app.state.settings.base_url
    if configured:
        from urllib.parse import urlsplit

        want = (urlsplit(configured).hostname or "").lower()
        host = (request.headers.get("host") or "").split(":")[0].lower()
        # Rebinding needs a name an attacker can put in public DNS, which
        # means a registrable domain — so anything with a dot is checked, and
        # everything else is let through:
        #   - an IP literal is not a name and resolves to itself
        #   - a single-label name ("arbox-server", "homeassistant") only
        #     exists on a local network. Home Assistant reaches this server by
        #     exactly such a name over the shared Docker network, so rejecting
        #     those would break the integration to stop an attack they cannot
        #     carry.
        allowed = {want, "localhost", ""}
        if host not in allowed and "." in host and not _is_ip(host):
            _LOGGER.warning("Rejected request for Host %r (expected %r)",
                            host, want)
            return PlainTextResponse("bad host", status_code=400)
    return await call_next(request)


def _is_ip(host: str) -> bool:
    import ipaddress

    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


app.include_router(router)
app.include_router(feedback_router)


@app.middleware("http")
async def feedback_privacy(request, call_next):
    response = await call_next(request)
    if request.url.path == "/feedback" or request.url.path.startswith("/api/feedback"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'"
    return response


@app.get("/feedback")
async def feedback_page():
    return FileResponse(os.path.join(FRONTEND_DIR, "feedback.html"))


@app.get("/")
async def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


# Path-per-page so a refresh keeps you where you were; the SPA reads the path.
# Must stay in step with VIEW_PATHS in frontend/app.js — a view added there and
# not here loads fine by tab click and 404s on refresh.
SPA_PAGES = ("schedule", "mine", "automations", "journal", "system", "settings")


@app.get("/{page}")
async def page_route(page: str):
    if page in SPA_PAGES:
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
    from fastapi import HTTPException as _HTTPException
    raise _HTTPException(404, "not found")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
