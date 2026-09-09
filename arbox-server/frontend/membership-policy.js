/* Shared by the standalone app and HA. All upstream strings are text, never HTML. */
const el = (tag, text) => { const n = document.createElement(tag); if (text != null) n.textContent = text; return n; };
const sources = {shop: 'פרטי המנוי מהסטודיו', rejection: 'תשובה מפורשת מארבוקס', manual: 'הגדרה ידנית שלכם'};
const periods = {day: 'ביום', week: 'בשבוע', month: 'בחודש', card: 'לכל תקופת הכרטיסייה'};
export function policySummary(member, openEditor) {
  const p = member.policy || {};
  const wrap = el('div');
  wrap.className = 'mu-policy';
  const source = el('details'); source.append(el('summary', 'מקור ופרטי אימות'));
  source.append(el('small', `מקור: ${sources[p.source] || 'טרם התקבל מידע מאומת'}`));
  if (p.verified_at) source.append(el('small', `נבדק: ${new Date(p.verified_at * 1000).toLocaleString('he-IL')}`));
  if (p.confirmed_category_ids?.length && !p.categories_known) source.append(el('small', `אישור שלכם מההתראה: ${p.confirmed_category_ids.length} סוגי שיעור. שאר הסוגים עדיין דורשים אימות.`));
  if (p.state !== 'ready') {
    const warning = el('strong', p.reason || 'דורש השלמה — ההרשמה האוטומטית מושהית');
    warning.setAttribute('role', 'status'); wrap.append(warning);
  }
  wrap.append(el('span', (p.limits || []).map(x => `${x.count} אימונים ${periods[x.period]}`).join(' · ') || 'המכסה לא אומתה'));
  if (p.unmatched?.length) source.append(el('small', `${p.unmatched.length} שמות לא הותאמו בוודאות. ההתאמות המפורשות נשמרו.`));
  wrap.append(source);
  if (openEditor) {
    const button = el('button', 'סוגי שיעורים ומכסה'); button.type = 'button';
    button.onclick = openEditor; wrap.append(button);
  }
  return wrap;
}

export function policyEditor({member, categories, save, close}) {
  const policy = member.policy || {};
  const d = el('section'); d.dir = 'rtl'; d.className = 'mu-editor';
  d.setAttribute('aria-label', `סוגי שיעורים ומכסה — ${member.plan || 'מנוי'}`);
  d.append(el('h2', member.plan || 'המנוי שלי'), el('p', 'בחרו רק שיעורים ומכסה שהסטודיו אישר למנוי הזה.'));
  const original = policy.rejection || policy.evidence || policy.previous_evidence;
  if (original || policy.read_error || policy.unsupported?.length) {
    const details = el('details'); details.append(el('summary', 'המידע שהתקבל מהסטודיו'));
    const pre = el('pre', JSON.stringify(original || policy.unsupported || policy.read_error, null, 2));
    pre.style.cssText = 'white-space:pre-wrap;overflow-wrap:anywhere;font:inherit'; details.append(pre); d.append(details);
  }
  const form = el('form'); form.style.cssText = 'display:grid;gap:16px';
  const fieldset = el('fieldset'); fieldset.append(el('legend', 'שיעורים מותרים'));
  const boxes = [];
  const choices = el('div'); choices.className = 'mu-categories'; fieldset.append(choices);
  for (const c of categories) {
    const label = el('label'); label.style.cssText = 'display:flex;gap:10px;align-items:center;min-height:44px';
    const input = el('input'); input.type = 'checkbox'; input.value = c.id;
    input.checked = (policy.categories_known !== false ? policy.category_ids || [] : policy.confirmed_category_ids || []).includes(c.id); boxes.push(input);
    label.append(input, el('span', c.name)); choices.append(label);
  }
  form.append(fieldset);
  if (member.sessions_on_purchase != null) form.append(el('p', `כרטיסייה: ${member.sessions_on_purchase} כניסות לכל התקופה. הנתון נלקח מארבוקס ואינו מתאפס בתחילת חודש.`));
  const limits = el('fieldset'); limits.append(el('legend', 'מגבלות תדירות'));
  const limitRows = [];
  const addLimit = (value = {}) => {
    const row = el('div'); row.style.cssText = 'display:flex;flex-wrap:wrap;gap:8px;margin:8px 0;align-items:center';
    const count = el('input'); count.type = 'number'; count.min = '1'; count.max = '10000'; count.required = true;
    count.value = value.count || ''; count.style.width = '80px'; count.setAttribute('aria-label', 'מספר האימונים');
    const period = el('select'); period.setAttribute('aria-label', 'תקופת המכסה');
    for (const v of ['month','week','day']) period.append(new Option(periods[v], v)); period.value = value.period || 'month';
    const start = el('select'); start.setAttribute('aria-label', 'תחילת שבוע המכסה לפי הסטודיו');
    start.append(new Option('בחרו את תחילת השבוע לפי הסטודיו', ''));
    ['שני','שלישי','רביעי','חמישי','שישי','שבת','ראשון'].forEach((name, i) => start.append(new Option(`השבוע מתחיל ביום ${name}`, i)));
    start.value = value.week_start ?? ''; start.hidden = period.value !== 'week'; start.required = !start.hidden;
    period.onchange = () => { start.hidden = period.value !== 'week'; start.required = !start.hidden; };
    const remove = el('button', 'הסרה'); remove.type = 'button'; remove.onclick = () => { row.remove(); limitRows.splice(limitRows.indexOf(item), 1); };
    const item = {count, period, start}; limitRows.push(item); row.append(count, period, start, remove); limits.append(row);
  };
  for (const v of (policy.limits || []).filter(x => x.period !== 'card')) addLimit(v);
  if (!limitRows.length && member.sessions_on_purchase == null) addLimit();
  const add = el('button', 'הוספת מגבלה נוספת'); add.type = 'button'; add.onclick = () => addLimit(); limits.append(add); form.append(limits);
  const help = el('details'); help.append(el('summary', 'איך ההגדרה משמשת לתכנון?'), el('p', 'ההגדרה משמשת לתזמונים ולאוטומציות. מגבלה שבועית מחייבת לבחור את יום תחילת השבוע לפי הסטודיו; אין המרה למכסה חודשית.')); form.append(help);
  const error = el('p'); error.setAttribute('role', 'alert'); form.append(error);
  const actions = el('div'); actions.style.cssText = 'display:flex;gap:12px';
  const submit = el('button', 'שמירת ההגדרה'); submit.type = 'submit';
  const cancel = el('button', 'סגירה'); cancel.type = 'button'; cancel.onclick = () => close(); actions.append(submit, cancel); form.append(actions);
  form.onsubmit = async event => {
    event.preventDefault(); if (submit.disabled) return;
    const ids = boxes.filter(x => x.checked).map(x => Number(x.value));
    if (!ids.length) { error.textContent = 'בחרו לפחות סוג שיעור אחד'; return; }
    if (limitRows.some(r => r.period.value === 'week' && r.start.value === '')) {
      error.textContent = 'בחרו את יום תחילת שבוע המכסה לפי הסטודיו'; return;
    }
    submit.disabled = true; error.textContent = '';
    try {
      await save({category_ids: ids, fingerprint: policy.fingerprint,
        limits: limitRows.map(r => ({count: Number(r.count.value), period: r.period.value,
          ...(r.period.value === 'week' ? {week_start: Number(r.start.value)} : {})}))});
      close();
    } catch (e) { error.textContent = e.message || 'השמירה נכשלה. הטיוטה נשמרה כאן.'; submit.disabled = false; }
  };
  d.append(form); return d;
}


export function policyDialog({host = document.body, ...options}) {
  const d = el('dialog'); d.dir = 'rtl';
  d.style.cssText = 'box-sizing:border-box;width:min(94vw,620px);max-height:90dvh;overflow:auto;border:1px solid var(--line,#ccc);border-radius:18px;padding:20px;background:var(--card,#fff);color:var(--ink,var(--text,#172c38));font:inherit;line-height:1.6';
  d.setAttribute('aria-label', `סוגי שיעורים ומכסה — ${options.member.plan || 'מנוי'}`);
  d.append(policyEditor({...options, close: () => d.close()}));
  d.onclose = () => d.remove(); host.append(d); d.showModal(); return d;
}
