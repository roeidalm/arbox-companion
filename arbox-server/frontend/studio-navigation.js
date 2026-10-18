/* Fresh pages use the default; selector refreshes preserve deliberate browsing. */
(function (root) {
  async function load(api, {useDefault = false} = {}) {
    let studios = await api('/api/studios');
    const target = Number(studios.default_studio_id);
    if (useDefault && target && target !== Number(studios.selected_studio_id)) {
      if (!(studios.studios || []).some(s => Number(s.id) === target && !s.ignored)) {
        throw new Error('סטודיו ברירת המחדל אינו זמין כרגע');
      }
      await api('/api/studios/select', {
        method: 'POST', body: JSON.stringify({studio_id: target}),
      });
      studios = await api('/api/studios');
      if (Number(studios.selected_studio_id) !== target) {
        throw new Error('בחירת הסטודיו השתנתה. נסו לפתוח שוב');
      }
    }
    return studios;
  }
  function options(data) {
    const active = (data.studios || []).filter(s => !s.ignored);
    const ids = new Set(active.map(s => Number(s.id)));
    return [...active.map(s => ({...s, available: true})),
      ...(data.affiliations || []).filter(s => !s.ignored && !ids.has(Number(s.id)))
        .map(s => ({...s, available: false,
          unavailableReason: s.has_active_membership === false ? 'ללא מנוי פעיל' : 'החיבור אינו זמין'}))];
  }
  const api = {load, options};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.StudioNavigation = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
