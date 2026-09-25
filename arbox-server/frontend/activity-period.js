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
  const api = {range, move};
  if (typeof module !== 'undefined') module.exports = api;
  else root.ActivityPeriod = api;
})(typeof window !== 'undefined' ? window : globalThis);
