"""Once-per-period reminders about verified, unallocated membership balance."""
from datetime import date, datetime
import logging

_LOGGER = logging.getLogger(__name__)


class BalanceReminder:
    def __init__(self, engine):
        self.e = engine

    async def check(self, today=None):
        config = getattr(self.e.settings, 'balance_reminder', {})
        if not config.get('enabled'):
            return
        today = today or date.today()
        # Read membership balances and history, without preflight booking calls.
        await self.e.syncer.refresh_membership()
        await self.e.refresh_planning_evidence(force_history=True)
        quota = await self.e.quota_status(target_date=today, read_only=True) or {}
        if quota.get('unattributed_sessions'):
            return
        for member in quota.get('memberships', []):
            if (not member.get('active') or member.get('period') not in ('month', 'card')
                    or member.get('policy', {}).get('state') != 'ready' or member.get('uncertain')):
                continue
            end = min(x for x in (member.get('period_end'), member.get('end')) if x) if member.get('period_end') else None
            if not end:
                continue
            remaining_days = (date.fromisoformat(end) - today).days
            free = member.get('available_after_planned')
            if (not 0 <= remaining_days <= config['days_before'] or free is None
                    or free < config['min_entries']):
                continue
            key = f"balance_reminder:{self.e.store.active_box_id}:{member['id']}:{member.get('period_start')}:{end}"
            if await self.e.store.get_meta(key):
                continue
            text = (f"🎟️ יתרת כניסות · {member.get('plan') or member['id']}\n"
                    f"נותרו {free} כניסות פנויות עד {end} ({remaining_days} ימים).\n"
                    f"כבר נלקחו בחשבון {member.get('reserved', 0)} הרשמות, "
                    f"{member.get('planned', 0)} אימונים מתוכננים ו־{member.get('standby', 0)} בהמתנה.\n"
                    "כדאי לפזר את האימונים שנותרו לאורך התקופה.")
            if await self.e.notifier.send(text, kind='membership'):
                await self.e.store.set_meta(key, {'sent_at': datetime.now().isoformat(), 'free_entries': free})
