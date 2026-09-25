/* Calendar periods are civil dates in the server's timezone, not device instants. */
(function (root) {
  const iso = (d) => d.toISOString().slice(0, 10);
  const parse = (s) => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(s || '')) throw new Error('תאריך אינו תקין');
    const d = new Date(s + 'T12:00:00Z');
    if (!Number.isFinite(+d) || iso(d) !== s) throw new Error('תאריך אינו תקין');
    return d;
  };
  function range(period, anchor, end) {
    if (period === 'all') return {};
    const from = parse(anchor), to = new Date(from);
    if (period === 'week') {
      from.setUTCDate(from.getUTCDate() - from.getUTCDay());
      to.setTime(+from); to.setUTCDate(to.getUTCDate() + 6);
    } else if (period === 'month') {
      from.setUTCDate(1); to.setUTCMonth(to.getUTCMonth() + 1, 0);
    } else if (period === 'custom') {
      to.setTime(+parse(end));
      if (to < from) throw new Error('תאריך ההתחלה מאוחר מתאריך הסיום');
    }
    return {date_from: iso(from), date_to: iso(to)};
  }
  function move(period, anchor, end, direction) {
    const r = range(period, anchor, end);
    if (period === 'all') return {anchor, end};
    const d = parse(r.date_from);
    if (period === 'month') d.setUTCMonth(d.getUTCMonth() + direction);
    else d.setUTCDate(d.getUTCDate() + direction * (period === 'week' ? 7 : period === 'custom' ?
      Math.round((+parse(r.date_to) - +d) / 86400000) + 1 : 1));
    const length = +parse(r.date_to) - +parse(r.date_from);
    return {anchor: iso(d), end: period === 'month' ? range('month', iso(d)).date_to : iso(new Date(+d + length))};
  }
  function label(period, anchor, end, today) {
    const r = range(period, anchor, end);
    if (period === 'all') return {title:'כל התקופות', detail:'כל ההיסטוריה', current:true};
    const currentRange = range(period === 'custom' ? 'day' : period, today, today);
    const current = r.date_from === currentRange.date_from && r.date_to === currentRange.date_to;
    const format = (d, options) => new Intl.DateTimeFormat('he-IL', {timeZone:'UTC', ...options}).format(parse(d));
    const dateFormat = {day:'numeric',month:'long', ...(anchor.slice(0,4) !== today.slice(0,4) ? {year:'numeric'} : {})};
    const detail = period === 'day' ? format(anchor,{weekday:'long',...dateFormat}) : period === 'month' ? format(anchor,{month:'long',year:'numeric'}) :
      new Intl.DateTimeFormat('he-IL',{timeZone:'UTC',...dateFormat}).formatRange(parse(r.date_from),parse(r.date_to));
    return {title:current ? ({day:'היום',week:'השבוע',month:'החודש'}[period] || 'התקופה שנבחרה') :
      ({day:'היום שנבחר',week:'השבוע שנבחר',month:'החודש שנבחר',custom:'התקופה שנבחרה'}[period]),detail,current};
  }
  const api = {range, move, label};
  if (typeof module !== 'undefined') module.exports = api;
  else root.ActivityPeriod = api;
})(typeof window !== 'undefined' ? window : globalThis);
