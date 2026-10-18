/* Local studio visibility; upstream memberships and bookings are untouched. */
(function (root) {
  const node = (tag, text, className = '') => {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    el.className = className;
    return el;
  };

  function confirmRemoval(studio, replacements, needsDefault, action = 'remove') {
    const ignoring = action === 'ignore';
    const dialog = node('dialog');
    dialog.setAttribute('aria-label', `${ignoring ? 'התעלמות מ־' : 'הסרת '}${studio.name}`);
    const form = node('form'); form.method = 'dialog';
    form.append(node('h2', ignoring ? `להתעלם מ־${studio.name}?` : `להסיר את ${studio.name} מהרשימה?`),
      node('p', ignoring
        ? 'הסטודיו יישאר ברשימה באפור. המעקב וההרשמה האוטומטית אליו יושהו עד לביטול ההתעלמות.'
        : 'הסטודיו יוסר מהבורר והמעקב וההרשמה האוטומטית אליו יושהו. ההיסטוריה תישמר ואפשר יהיה להחזיר אותו דרך ההגדרות.'),
      node('p', 'המנוי וההרשמות שכבר בוצעו אצל הסטודיו לא יבוטלו.', 'hint'));
    const select = node('select');
    if (needsDefault) {
      const label = node('label', 'בחר סטודיו ברירת מחדל חלופי');
      const empty = node('option', 'בחר סטודיו'); empty.value = '';
      select.append(empty); select.required = true;
      for (const candidate of replacements) {
        const option = node('option', candidate.name); option.value = candidate.id;
        select.append(option);
      }
      label.append(select); form.append(label);
    }
    const actions = node('div', undefined, 'dialog-actions');
    const cancel = node('button', 'חזרה'); cancel.value = 'cancel'; cancel.formNoValidate = true;
    cancel.autofocus = true;
    const confirm = node('button', ignoring ? 'התעלם מסטודיו' : 'הסר מהרשימה', ignoring ? 'primary' : 'studio-remove'); confirm.value = 'confirm';
    confirm.disabled = needsDefault;
    select.onchange = () => { confirm.disabled = !select.value; };
    actions.append(cancel, confirm); form.append(actions); dialog.append(form);
    document.body.append(dialog);
    return new Promise(resolve => {
      dialog.addEventListener('close', () => {
        const result = dialog.returnValue === 'confirm'
          ? {replacement: needsDefault ? Number(select.value) : null} : null;
        dialog.remove(); resolve(result);
      }, {once: true});
      dialog.showModal();
    });
  }

  function mount(host, data, api, afterSave, {confirm = confirmRemoval} = {}) {
    const affiliations = data.studio_affiliations || [];
    const available = data.studios || [];
    const ignored = new Set((data.ignored_studio_ids || []).map(Number));
    const removed = new Set((data.removed_studio_ids || []).map(Number));
    const defaultId = Number(data.default_studio_id || data.selected_studio_id);
    const active = available.filter(s => !ignored.has(Number(s.id)) && !removed.has(Number(s.id)));
    if (!affiliations.length) return;
    const manage = node('div', undefined, 'studio-manage');
    manage.append(node('h3', 'הסטודיואים שלי'));
    let busy = false;
    const controls = [];
    const notice = node('p', '', 'hint'); notice.setAttribute('role', 'status');
    const button = (text, action, className = '') => {
      const el = node('button', text, className); el.type = 'button'; el.onclick = action;
      controls.push(el); return el;
    };
    const save = async (nextIgnored, nextRemoved, nextDefault, message) => {
      if (busy) return;
      busy = true;
      const disabled = controls.map(el => el.disabled);
      controls.forEach(el => { el.disabled = true; });
      notice.textContent = 'שומר…';
      let saved = false;
      try {
        const result = await api('/api/studios/preferences', {method: 'POST', body: JSON.stringify({
          default_studio_id: nextDefault, ignored_studio_ids: [...nextIgnored],
          removed_studio_ids: [...nextRemoved],
        })});
        saved = true; notice.textContent = result.warning || message;
        await afterSave();
      } catch (error) {
        notice.textContent = (saved ? 'השינוי נשמר, אך התצוגה לא התרעננה. רעננו את העמוד. ' : 'השינוי לא נשמר: ') + error.message;
      } finally {
        busy = saved; // A stale form must not undo a saved removal after a failed refresh.
        if (!saved) controls.forEach((el, i) => { el.disabled = disabled[i]; });
      }
    };
    const label = node('label', 'סטודיו ברירת מחדל');
    const select = node('select'); select.id = 'defaultStudio';
    for (const studio of active) {
      const option = node('option', studio.name); option.value = studio.id; select.append(option);
    }
    select.value = String(defaultId); select.disabled = active.length < 2; controls.push(select);
    label.append(select); manage.append(label,
      node('p', 'זה הסטודיו שייפתח בכל כניסה. מעבר מהכותרת הוא זמני.', 'hint'));
    const setDefault = button('שמור ברירת מחדל', () => save(ignored, removed, Number(select.value), 'ברירת המחדל נשמרה'), 'primary');
    setDefault.disabled = true;
    select.onchange = () => { setDefault.disabled = Number(select.value) === defaultId; };
    manage.append(setDefault, node('p', 'התעלמות משאירה את הסטודיו ברשימה ומשהה את המעקב וההרשמה האוטומטית. הסרה מסתירה אותו מהבורר.', 'hint'));
    const list = node('div', undefined, 'studio-manage-list');
    for (const studio of affiliations) {
      const id = Number(studio.id), isRemoved = removed.has(id), isIgnored = ignored.has(id);
      const row = node('div', undefined, 'sc-studio');
      const details = node('div', undefined, 'sc-identity');
      const name = node('strong', studio.name); name.dir = 'auto';
      const status = isRemoved ? 'הוסר מהרשימה · המעקב מושהה'
        : isIgnored ? 'בהתעלמות · נשאר ברשימה, המעקב מושהה'
        : studio.has_active_membership === false ? 'ללא מנוי פעיל · מוצג באפור בבורר'
        : id === defaultId ? 'ברירת המחדל שלך' : 'מופיע בבורר הסטודיואים';
      details.append(name, node('small', status, 'hint')); row.append(details);
      const actions = node('div', undefined, 'studio-actions');
      if (isRemoved) {
        actions.append(button('החזר לרשימה', () => save(ignored, new Set([...removed].filter(x => x !== id)), defaultId, 'הסטודיו הוחזר לרשימה')));
      } else {
        const replacements = active.filter(s => Number(s.id) !== id);
        const exclude = async action => {
          if (busy) return;
          busy = true;
          let choice;
          try {
            choice = action === 'ignore' && id !== defaultId ? {replacement: null}
              : await confirm(studio, replacements, id === defaultId, action);
          }
          finally { busy = false; }
          if (!choice) return;
          if (id === defaultId && !replacements.some(s => Number(s.id) === choice.replacement)) return;
          await save(action === 'ignore' ? new Set([...ignored, id]) : ignored,
            action === 'remove' ? new Set([...removed, id]) : removed,
            id === defaultId ? choice.replacement : defaultId,
            action === 'ignore' ? 'ההתעלמות הופעלה' : 'הסטודיו הוסר מהרשימה');
        };
        const ignore = isIgnored
          ? button('ביטול התעלמות', () => save(new Set([...ignored].filter(x => x !== id)), removed, defaultId, 'ההתעלמות בוטלה'))
          : button('התעלם מסטודיו', () => exclude('ignore'));
        const remove = button('הסר מהרשימה', () => exclude('remove'), 'studio-remove');
        if (!replacements.length) {
          remove.disabled = true;
          if (!isIgnored) ignore.disabled = true;
          details.append(node('small', 'כדי להתעלם או להסיר, יש להוסיף קודם סטודיו אחר עם מנוי פעיל.', 'hint'));
        }
        actions.append(ignore, remove);
      }
      row.append(actions);
      list.append(row);
    }
    manage.append(list, notice); host.append(manage);
  }
  root.StudioManagement = {mount};
  if (typeof module !== 'undefined' && module.exports) module.exports = {mount};
})(typeof globalThis !== 'undefined' ? globalThis : this);
