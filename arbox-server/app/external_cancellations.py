"""Reconcile facts separately from the user's explanation. Never writes to Arbox."""
import json
import secrets
from datetime import date, datetime, timedelta
from .notification_reply import NotificationReply


async def save_reason(engine, sid, code, text=None):
    if not await engine.store.get_session(sid):
        raise ValueError('האימון אינו שייך לסטודיו הפעיל')
    outcome = await engine.store.get_training_outcome(sid)
    if not outcome or outcome['status'] not in ('cancelled_late', 'cancelled_safe', 'cancelled_unknown'):
        raise ValueError('הביטול כבר אינו המצב הנוכחי של האימון')
    source = 'external_confirmed' if outcome['source'].startswith('external_') else outcome['source']
    await engine.store.set_training_outcome(sid, outcome['status'], source, code, text)
    await engine.store.expire_prompts(sid, ('external_cancel', 'external_cancel_reason'))


async def notify_pending(engine):
    timeline = await engine.store.training_events()
    for row in await engine.store.training_history():
        if row.get('source') != 'external_sync':
            continue
        # Historical imports remain visible, without flooding the user.
        if row['date'] < (date.today() - timedelta(days=7)).isoformat():
            continue
        sid = row['schedule_id']
        events = [e for e in timeline if e['schedule_id'] == sid and e['event_type'] == row['status']]
        event_id = events[-1]['id'] if events else None
        key = f"external_cancel_notice:{sid}:{row['status']}:{event_id}"
        delivery = await engine.store.get_meta(key) or {}
        if delivery is True or delivery.get('delivered'):
            continue
        cid = delivery.get('cid') or secrets.token_urlsafe(8)
        if not delivery:
            await engine.store.add_prompt(cid, sid, 'external_cancel', payload=json.dumps({'status':row['status'], 'event_id':event_id}))
            await engine.store.set_meta(key, {'cid':cid})
        fee = 'ביטול מאוחר אושר ב־Arbox · כניסה נוצלה' if row['status'] == 'cancelled_late' else 'ההרשמה נעלמה · הביטול והחיוב עדיין בבירור'
        delivered = await engine.notifier.send(
            f"⚠️ שינוי בהרשמה\n{row['date']} · {row['start_time']} · {row['category_name']}\n{fee}\nהאם אתה ביטלת?",
            [[{'text':'אני ביטלתי', 'data':f'xc_yes:{cid}'},
              {'text':'לא ביטלתי / פרטים', 'data':f'xc_info:{cid}'}]], kind='system',
            is_answered=lambda cid=cid: engine.store.any_answered([cid]))
        if delivered:
            await engine.store.set_meta(key, {'cid':cid, 'delivered':True})


async def callback(engine, action, cid, reply_text=None):
    prompt = await engine.store.peek_prompt(cid)
    if not prompt or prompt.get('answered_at') or prompt['action'] not in ('external_cancel','external_cancel_reason'):
        return 'הבקשה כבר טופלה; אפשר לעדכן סיבה בהיסטוריה'
    sid = prompt['schedule_id']
    if not await engine.store.get_session(sid):
        return 'האימון אינו שייך לסטודיו הפעיל; לא עודכן דבר'
    row = await engine.store.get_training_outcome(sid)
    if not row or row['status'] not in ('cancelled_late','cancelled_safe','cancelled_unknown'):
        return 'מצב האימון השתנה — לא עודכן דבר'
    events = [e for e in await engine.store.training_events() if e['schedule_id'] == sid and e['event_type'] == row['status']]
    expected = json.loads(prompt.get('payload') or '{}').get('event_id')
    if not events or events[-1]['id'] != expected:
        return 'מצב האימון השתנה; פתח את ההודעה העדכנית או את ההיסטוריה'
    if action == 'xc_info':
        fee = 'Arbox אישר ביטול מאוחר וחיוב כניסה.' if row['status'] == 'cancelled_late' else 'אין עדיין הוכחה לחיוב או לסיבת השינוי.'
        return NotificationReply(
            f"{row['category_name']} · {row['date']} {row['start_time']}\n{fee}\n"
            'אין לנו מידע מי ביטל ומתי. מועד הגילוי בהיסטוריה אינו מועד הביטול.\n'
            'אם לא ביטלת, כדאי לברר מול הסטודיו. לא סומן שביטלת.',
            [[{'text':'אני ביטלתי — הוסף סיבה','data':f'xc_yes:{cid}'},
              {'text':'היסטוריה','uri':engine.settings.browser_url.rstrip('/')+'/mine?history=1'}]])
    if action == 'xc_yes':
        return NotificationReply('מה סיבת הביטול?', [[{'text':label,'data':f'xc_reason_{code}:{cid}',
            **({'text_input':True,'placeholder':'סיבת הביטול'} if code=='other' else {})}]
            for code,label in engine.REASON_LABELS.items()])
    if action.startswith('xc_reason_'):
        code = action.removeprefix('xc_reason_')
        if code not in engine.REASON_LABELS:
            return 'סיבה לא מוכרת'
        if code == 'other' and not (reply_text or '').strip():
            await engine.store.set_meta('external_cancel_input', {'sid':sid,'cid':cid,'at':datetime.now().isoformat()})
            return 'כתוב את סיבת הביטול בהודעה הבאה, או דרך ההיסטוריה באתר'
        await save_reason(engine, sid, code, reply_text.strip()[:500] if code=='other' else None)
        await engine.store.set_meta('external_cancel_input', None)
        return 'סיבת הביטול נשמרה בהיסטוריה. החיוב לא נספר שוב ✓'
    return 'כפתור לא מוכר'


async def message(engine, text):
    pending = await engine.store.get_meta('external_cancel_input')
    if not pending or not all(k in pending for k in ('sid', 'cid', 'at')):
        return None
    if datetime.now() - datetime.fromisoformat(pending['at']) > timedelta(minutes=30):
        await engine.store.set_meta('external_cancel_input', None)
        return 'בקשת הסיבה פגה; אפשר לפתוח אותה שוב או לעדכן בהיסטוריה'
    if not text.strip() or len(text) > 500:
        return 'יש להזין סיבה באורך 1–500 תווים'
    return await callback(engine, 'xc_reason_other', pending['cid'], text)
