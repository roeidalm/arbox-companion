/* Presentation only: capacity, allocation and eligibility come from the server. */
const node = (tag, text, cls) => {
  const n = document.createElement(tag);
  if (text != null) n.textContent = text;
  if (cls) n.className = cls;
  return n;
};
const count = value => Number.isFinite(value) ? Math.max(0, value) : 0;
const periods = {month: 'חודשי', week: 'שבועי', day: 'יומי', card: 'לכל תקופת הכרטיסייה'};

export function quotaSegments(member) {
  const quota = member.quota;
  if (!Number.isFinite(quota) || quota <= 0) return [];
  const parts = [
    ['used', 'נוצלו', count(member.used)],
    ['reserved', 'רשומים', count(member.reserved)],
    ['standby', 'בהמתנה', count(member.pending_standby ?? member.standby)],
    ['planned', 'בתכנון', count(member.planned)],
    ['uncertain', 'בבירור', count(member.uncertain)],
    ['free', 'פנויים', count(member.available_after_planned)],
  ];
  const total = parts.reduce((sum, part) => sum + part[2], 0);
  // An unverified / unavailable remainder is never relabelled as free capacity.
  if (total < quota) parts.push(['unknown', 'יתרה ללא שיוך', quota - total]);
  return parts.filter(part => part[2] > 0).map(([kind, label, value]) => ({
    kind, label, value, percent: value / Math.max(quota, total) * 100,
  }));
}

export function quotaRow(member, {shortName} = {}) {
  const row = node('article', null, 'mu-quota-row');
  const name = node('div', null, 'mu-member-name');
  const title = node('strong', shortName || member.plan || 'מנוי'); title.title = member.plan || '';
  name.append(title);
  if (!shortName) name.append(node('small', periods[member.period] || 'מכסה לא מאומתת'));
  const available = member.available_after_planned;
  const free = node('span', Number.isFinite(available) && Number.isFinite(member.quota)
    ? `${available} פנויים מתוך ${member.quota}` : 'היתרה טרם אומתה', 'mu-free-label');
  const usage = node('div', null, 'mu-usage');
  const values = [['used', 'נוצלו'], ['reserved', 'רשומים'], ['planned', 'בתכנון'], ['pending_standby', 'בהמתנה'], ['uncertain', 'בבירור']];
  const text = values.filter(([key], index) => Number.isFinite(member[key]) && (index < 3 || member[key] > 0))
    .map(([key, label]) => `${member[key]} ${label}`).join(' · ') || 'פירוט השימוש אינו זמין';
  usage.append(node('span', text, 'mu-counts'));
  const bar = node('div', null, 'mu-bar');
  bar.setAttribute('role', 'img');
  const segments = quotaSegments(member);
  bar.setAttribute('aria-label', `${member.plan || 'מנוי'}: ${text}. ${free.textContent}`);
  for (const part of segments) {
    const n = node('span', null, `mu-segment mu-${part.kind}`);
    n.style.flexBasis = `${part.percent}%`;
    n.title = `${part.value} ${part.label}`;
    bar.append(n);
  }
  if (!segments.length) bar.classList.add('mu-unverified');
  usage.append(bar);
  row.append(name, free, usage);
  if (member.policy?.state && member.policy.state !== 'ready')
    row.append(node('small', member.policy.reason || 'התאמת המנוי דורשת השלמה', 'mu-warning'));
  if (Number.isFinite(member.quota) && count(member.used) + count(member.reserved) + count(member.planned) + count(member.pending_standby) + count(member.uncertain) > member.quota)
    row.append(node('small', 'ההתחייבויות חורגות ממכסת המנוי', 'mu-warning'));
  return row;
}

export function quotaSummary(quota, manage) {
  const wrap = node('section', null, 'mu-summary');
  wrap.setAttribute('aria-label', 'המנויים והמכסה שלי');
  if (quota?.memberships?.length) for (const member of quota.memberships) {
    // Use the verified period, never guesses based on the studio's plan name.
    // Keep full names when two memberships would otherwise become ambiguous.
    const unique = quota.memberships.filter(m => m.period === member.period).length === 1;
    const shortName = unique && member.period === 'month' && Number.isFinite(member.quota) ? 'מנוי חודשי' : null;
    wrap.append(quotaRow(member, {shortName}));
  }
  else wrap.append(node('p', 'פירוט המכסה אינו זמין כרגע', 'mu-counts'));
  const alerts = [];
  if (quota?.uncovered_plans?.length) alerts.push(`${quota.uncovered_plans.length} תכנונים ללא כיסוי`);
  if (quota?.unresolved_plans?.length) alerts.push(`${quota.unresolved_plans.length} תכנונים דורשים השלמה`);
  if (quota?.unattributed_sessions?.length) alerts.push(`${quota.unattributed_sessions.length} אימונים טרם שויכו למנוי`);
  if (quota?.overcommitted && !quota.uncovered_plans?.length) alerts.push('יש חריגה ממכסת מנוי');
  if (alerts.length) wrap.append(node('p', alerts.join(' · '), 'mu-warning'));
  if (manage) {
    const b = node('button', 'ניהול מנויים בסטודיו', 'mu-manage');
    b.type = 'button'; b.onclick = manage; wrap.append(b);
  }
  return wrap;
}

export function membershipDisclosure(member, details, {open = false, onToggle} = {}) {
  const card = node('details', null, 'mu-membership'); card.open = open;
  const heading = node('summary');
  const title = node('span', member.plan || 'מנוי', 'mu-disclosure-name');
  const verifiedLimits = (member.policy?.limits || []).map(x => `${x.count} כניסות · ${periods[x.period] || 'לתקופה'}`).join(' · ');
  const limit = member.quota == null ? verifiedLimits || 'מכסה לא מאומתת' : `${member.quota} כניסות · ${periods[member.period] || 'לתקופה'}`;
  const info = node('small', `${limit}${member.active === false ? ' · לא פעיל' : ''}`);
  const status = node('span', member.policy?.state !== 'ready' ? 'דורש השלמה' : `${member.available_after_planned ?? '—'} פנויים`, member.policy?.state !== 'ready' ? 'mu-warning' : '');
  title.append(info); heading.append(title, status);
  const body = node('div', null, 'mu-membership-body'); body.append(details);
  card.append(heading, body);
  card.ontoggle = () => onToggle?.(card.open);
  return card;
}
