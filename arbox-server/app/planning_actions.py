"""Scoped, one-use planning actions shared by Telegram and HA notifications.

Navigation only reads the local ledger. Confirmation never calls Arbox book:
it updates planning intent, which still passes the normal booking gates.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time

from .membership_policy import eligible, fingerprint
from .arbox_client import ArboxError
from .notification_reply import NotificationReply
from .store import NO_SCHEDULE
from .planning_intent import snapshot, token as intent_token


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def session_identity(session):
    return snapshot(session)


class PlanningActions:
    def __init__(self, engine):
        self.e = engine

    async def button(self, text, operation, context, **extra):
        cid = secrets.token_urlsafe(12)
        payload = {**context, **extra, 'operation': operation}
        await self.e.store.add_prompt(cid, context.get('schedule_id', NO_SCHEDULE), 'planning_action',
                                      batch_id=context.get('group'),
                                      payload=json.dumps(payload))
        return {'text': text, 'data': f'plan:{cid}',
                'notification_tag': context['tag'], 'authentication_required': True}

    def label(self, session):
        from .rules import fmt_when
        return f"{fmt_when(session)}\n{session.get('category_name') or 'שיעור'} · {session.get('coach_name') or ''}".strip(' ·')

    async def notice_buttons(self, problems, *, force_list=False):
        tag = 'arbox-plan-' + secrets.token_urlsafe(12)
        context = {'studio': self.e.store.active_box_id,
                   'account': self.e.membership_policy.key(0),
                   'tag': tag, 'created': time.time()}
        items = []
        for plan, _, _ in problems:
            session = await self.e.store.get_session(plan['schedule_id']) or plan
            items.append({**context, 'schedule_id': plan['schedule_id'],
                          'identity': session_identity(session),
                          'selected': plan.get('membership_user_id'),
                          'group': secrets.token_urlsafe(12)})
        if len(items) == 1 and not force_list:
            return await self.actions(items[0])
        return [[await self.button('בחירת אימון לתיקון', 'list', context, items=items, page=0)]]

    async def actions(self, context):
        session = await self.e.store.get_session(context['schedule_id'])
        if session and await self.e.store.intent_change(session):
            return [[await self.button('אישור האימון המעודכן', 'keep_change', context),
                     await self.button('ביטול התכנון הזה', 'drop_change', context)]]
        return [[await self.button('התעלם מסוג השיעור', 'ignore', context),
                 await self.button('בחירת מנוי', 'members', context, page=0)]]

    def reply(self, context, text, buttons=None):
        return NotificationReply(text, buttons or [], context.get('tag'))

    async def callback(self, cid):
        # The same lock also serializes studio switches and every booking tick.
        async with self.e._tick_lock:
            prompt = await self.e.store.peek_prompt(cid)
            if not prompt or prompt.get('action') != 'planning_action':
                return NotificationReply('הבקשה כבר טופלה או שפגה.')
            try:
                c = json.loads(prompt['payload'])
            except (TypeError, ValueError, KeyError):
                return NotificationReply('הבקשה אינה תקינה.')
            if prompt.get('answered_at'):
                return self.reply(c, 'הבקשה כבר טופלה.')
            if (c.get('studio') != self.e.store.active_box_id
                    or c.get('studio') != self.e.syncer.box_id
                    or c.get('account') != self.e.membership_policy.key(0)
                    or time.time() - c.get('created', 0) > 14 * 86400):
                return self.reply(c, 'הבקשה כבר לא שייכת לחיבור הפעיל או שפגה. לא בוצע שינוי.')
            if c['operation'] == 'list':
                reply = await self.choose_session(c)
                await self.e.store.take_prompt(cid)
                return reply
            session = await self.e.store.get_session(c['schedule_id'])
            if not session or session_identity(session) != c['identity']:
                return self.reply(c, 'פרטי האימון השתנו. פתחו את האימון המעודכן לפני פעולה.')
            from .rules import date, datetime
            if datetime.fromisoformat(f"{session['date']}T{session['start_time']}") <= datetime.now():
                return self.reply(c, 'האימון כבר התחיל. לא בוצע שינוי.')
            plans = await self.e._planned_sessions(date.today().isoformat(), '9999-12-31')
            plan = next((p for p in plans if p['schedule_id'] == c['schedule_id']), None)
            if (not plan or session.get('user_booked') is not None
                    or session.get('user_in_standby') is not None or self.e._blocked(session)):
                return self.reply(c, 'האימון כבר אינו ממתין לתכנון הזה. לא בוצע שינוי.')
            if plan.get('membership_user_id') != c.get('selected'):
                return self.reply(c, 'בחירת המנוי כבר השתנתה. פתחו את התכנון המעודכן.')
            op = c['operation']
            if op in ('keep_change', 'drop_change'):
                if not await self.e.store.intent_change(session):
                    return self.reply(c, 'השינוי כבר טופל. לא בוצע שינוי נוסף.')
                if op == 'keep_change':
                    try:
                        result = await self.e.confirm_plan_change(session['schedule_id'], intent_token(session))
                    except ArboxError as err:
                        return self.reply(c, str(err))
                    message = '✓ האימון המעודכן אושר.\n' + result.get('reason', 'התכנון נשמר')
                else:
                    if plan.get('planning_source') == 'autobook':
                        await self.e.store.set_automation_skip(session['schedule_id'], True)
                    else:
                        await self.e.store.unwatch(session['schedule_id'])
                    message = '✓ התכנון הזה בוטל.'
                await self.e.store.take_prompt(cid)
                await self.e.store.answer_batch(c.get('group'))
                return self.reply(c, message)
            if op in ('open', 'ignore', 'members'):
                # Opening the correction flow is an answer too: stop the
                # notifier from escalating the original notice to another channel.
                await self.e.store.take_prompt(cid)
            if op == 'open':
                change = await self.e.store.intent_change(session)
                detail = change['reason'] + '\nהתכנון מושהה · נדרש אישור' if change else 'איך לטפל באימון?'
                return self.reply(c, self.label(session) + '\n\n' + detail, await self.actions(c))
            if op == 'ignore':
                return self.reply(c, f"להפסיק לתכנן {session['category_name']} בסטודיו הזה?\n\nהרשמות קיימות לא יבוטלו.",
                    [[await self.button('כן, התעלם מהסוג הזה', 'ignore_confirm', c),
                      await self.button('חזרה', 'open', c)]])
            if op == 'ignore_confirm':
                if not await self.e.store.take_prompt(cid):
                    return self.reply(c, 'הבקשה כבר טופלה.')
                self.e.settings.block_category(session['category_name'])
                for row in plans:
                    if row.get('category_name') == session['category_name']:
                        await self.e.store.unwatch(row['schedule_id'])
                await self.e.store.answer_batch(c.get('group'))
                await self.e.reconcile_planned_quota()
                await self.e.schedule_openings()
                await self.e.store.log_event('info', 'planning',
                    f"התעלמות מסוג שיעור · {session['category_name']}",
                    'לבקשת המשתמש מההתראה; התכנונים הוסרו מהמכסה', session['schedule_id'])
                return self.reply(c, f"✓ {session['category_name']} נוסף להתעלמות.\nהתכנונים הוסרו מהמכסה. ניתן לשנות זאת בהגדרות הסטודיו.")
            if op == 'members':
                return await self.choose_member(c, session)
            if op in ('member', 'assign'):
                return await self.assign(c, session, cid, confirm=op == 'assign')
            return self.reply(c, 'פעולה לא מוכרת.')

    async def choose_session(self, c):
        page = int(c.get('page', 0))
        items = c['items']
        buttons = []
        for item in items[page:page + 2]:
            session = await self.e.store.get_session(item['schedule_id'])
            if session:
                text = f"{session['date'][8:10]}.{session['date'][5:7]} · {session['start_time'][:5]} · {session['category_name']}"
                buttons.append([await self.button(text[:80], 'open', item)])
        if len(items) > 2:
            buttons.append([await self.button('אימונים נוספים', 'list', c,
                                               page=(page + 2) % len(items))])
        return self.reply(c, 'איזה אימון לבדוק?', buttons)

    async def member_option(self, c, session, member):
        policy = await self.e.membership_policy.get(member)
        category = session.get('category_id')
        compatible = eligible({**member, 'policy': policy}, session)
        manual = bool(category is not None and not compatible
                      and not policy.get('categories_known')
                      and not policy.get('contradiction')
                      and category not in policy.get('denied_category_ids', [])
                      and policy.get('quota_known'))
        if not compatible and not manual:
            return {'member': member, 'policy': policy, 'available': False,
                    'reason': 'לא כולל את סוג השיעור' if policy.get('categories_known') else policy.get('reason') or 'נדרשת השלמה'}
        proposed = dict(policy)
        if manual:
            proposed.update(state='ready', confirmed_category_ids=[*policy.get('confirmed_category_ids', []), category])
        quota = await self.e.quota_status(target_date=session['date'],
            extra_plans=[{**session, 'membership_user_id': member['id']}],
            policy_overrides={member['id']: proposed}) or {}
        state = quota.get('plan_states', {}).get(str(session['schedule_id']), {})
        return {'member': member, 'policy': policy, 'manual': manual,
                'available': state.get('state') == 'ready', 'reason': state.get('reason'),
                'quota': next((m for m in quota.get('memberships', []) if m['id'] == member['id']), {})}

    async def choose_member(self, c, session):
        members = [m for m in await self.e.store.get_meta('memberships') or []
                   if self.e._membership_valid_on(m, session['date'])]
        options = [await self.member_option(c, session, m) for m in members]
        lines = [self.label(session), '', 'בחירת מנוי לאימון הזה:']
        for option in options:
            status = ('דרוש אישור שהשיעור כלול' if option.get('manual') else 'מתאים · המכסה מספיקה') if option['available'] else option['reason']
            lines += [f"• {option['member'].get('plan') or 'מנוי'} — {status}"]
        selectable = [o for o in options if o['available']]
        page = int(c.get('page', 0))
        buttons = []
        for o in selectable[page:page + 2]:
            buttons.append([await self.button(o['member'].get('plan') or 'מנוי', 'member', c,
                member_id=o['member']['id'], fingerprint=fingerprint(o['member']),
                policy_digest=digest(o['policy']))])
        if len(selectable) > 2:
            buttons.append([await self.button('מנויים נוספים', 'members', c, page=(page + 2) % len(selectable))])
        else:
            buttons.append([await self.button('חזרה', 'open', c)])
        if not selectable:
            lines += ['', 'אין כרגע מנוי שניתן לשייך בבטחה. התכנון נשאר מושהה.']
        return self.reply(c, '\n'.join(lines), buttons)

    async def assign(self, c, session, cid, *, confirm):
        members = await self.e.store.get_meta('memberships') or []
        member = next((m for m in members if m['id'] == c.get('member_id')), None)
        if (not member or fingerprint(member) != c.get('fingerprint')
                or not self.e._membership_valid_on(member, session['date'])):
            return self.reply(c, 'פרטי המנוי השתנו. בחרו מחדש.', await self.actions(c))
        option = await self.member_option(c, session, member)
        if digest(option['policy']) != c.get('policy_digest'):
            return self.reply(c, 'מידע ההתאמה השתנה. בחרו מחדש.', await self.actions(c))
        if not option['available']:
            return self.reply(c, f"לא ניתן לשייך: {option['reason']}\nהתכנון נשאר מושהה.", await self.actions(c))
        if not confirm:
            extra = (f"נדרש אישורך לפי מידע מהסטודיו: המנוי כולל {session['category_name']}.\n" if option.get('manual') else '')
            return self.reply(c, f"{self.label(session)}\n\nלשייך ל־{member.get('plan')}?\n{extra}המכסה נבדקה ומספיקה לאימון.",
                [[await self.button('מאשר ומשייך', 'assign', c), await self.button('חזרה', 'members', c, page=0)]])
        if not await self.e.store.take_prompt(cid):
            return self.reply(c, 'הבקשה כבר טופלה.')
        if option.get('manual'):
            await self.e.membership_policy.confirm_category(member, session['category_id'], c['fingerprint'])
        # Pinning an automatic occurrence makes this membership choice explicit
        # for this occurrence only; it never changes the recurring rule/default.
        watches = await self.e.store.list_watchlist(pending_only=True)
        watch = next((w for w in watches if w['schedule_id'] == session['schedule_id']), None)
        if watch:
            await self.e.store.set_watch_membership(session['schedule_id'], member['id'])
        else:
            await self.e.store.watch(session['schedule_id'], membership_user_id=member['id'])
        await self.e.store.answer_batch(c.get('group'))
        await self.e.reconcile_planned_quota()
        await self.e.schedule_openings()
        await self.e.store.log_event('info', 'planning', 'מנוי שויך מההתראה',
                                     member.get('plan'), session['schedule_id'])
        return self.reply(c, f"✓ האימון שויך ל־{member.get('plan')}.\nהתכנון פעיל, בכפוף לבדיקת ההתאמה והמכסה בעת ההרשמה.")
