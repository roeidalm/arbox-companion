/* Keep the view mode across page loads, while starting at the studio's today. */
(function (root) {
  const modes = new Set(['day', 'week', 'month']);
  function today(timeZone, now = new Date()) {
    let formatter;
    try {
      formatter = new Intl.DateTimeFormat('en', {
        timeZone, year: 'numeric', month: '2-digit', day: '2-digit',
      });
    } catch (_) {
      formatter = new Intl.DateTimeFormat('en', {
        year: 'numeric', month: '2-digit', day: '2-digit',
      });
    }
    const parts = Object.fromEntries(formatter.formatToParts(now).map(p => [p.type, p.value]));
    return `${parts.year}-${parts.month}-${parts.day}`;
  }
  function validDate(value) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value || '')) return false;
    const parsed = new Date(value + 'T12:00:00Z');
    return Number.isFinite(+parsed) && parsed.toISOString().slice(0, 10) === value;
  }
  function initial(search, currentDay, {restoreDate = false} = {}) {
    const params = new URLSearchParams(search);
    return {
      mode: modes.has(params.get('mode')) ? params.get('mode') : 'week',
      // A reload or restored tab must not mistake yesterday's generated URL
      // for a new date selection. Only in-page history restores that choice.
      anchor: restoreDate && validDate(params.get('d')) ? params.get('d') : currentDay,
    };
  }
  function url(mode, anchor, currentDay) {
    const params = new URLSearchParams({mode: modes.has(mode) ? mode : 'week'});
    if (validDate(anchor) && anchor !== currentDay) params.set('d', anchor);
    return '/schedule?' + params;
  }
  const api = {today, initial, url};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.ScheduleNavigation = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
