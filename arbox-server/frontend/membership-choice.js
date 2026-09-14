/** Shared in-place membership repair for scheduled and automatic workouts. */
const node = (tag, text, cls) => {
  const el = document.createElement(tag);
  if (text != null) el.textContent = text;
  if (cls) el.className = cls;
  return el;
};

export function membershipChoice({load, save, onSaved, onInventory, isCurrent = () => true}) {
  const root = node('section', null, 'membership-choice');
  root.setAttribute('aria-label', 'בחירת מנוי לאימון');
  const header = node('div', null, 'membership-choice-header');
  const refresh = node('button', 'רענון מנויים'); refresh.type = 'button';
  header.append(node('strong', 'מנוי לאימון הזה'), refresh);
  const status = node('p', '', 'membership-choice-status'); status.setAttribute('role', 'status');
  const optionsHost = node('fieldset', null, 'membership-choice-options');
  const confirmation = node('label', null, 'membership-choice-confirm');
  const checkbox = node('input'); checkbox.type = 'checkbox';
  const confirmationText = node('span'); confirmation.append(checkbox, confirmationText);
  confirmation.hidden = true;
  const submit = node('button', 'שמירת המנוי לאימון', 'primary'); submit.type = 'button';
  submit.disabled = true;
  root.append(header, status, optionsHost, confirmation, submit);
  let data, selected, busy = false, requireRefresh = false, saved = false;
  const group = `membership-${Math.random().toString(36).slice(2)}`;

  const update = () => {
    refresh.disabled = busy;
    optionsHost.disabled = busy || saved;
    checkbox.disabled = busy || saved;
    submit.disabled = busy || saved || requireRefresh || !selected?.available || (!!selected?.manual && !checkbox.checked);
    root.setAttribute('aria-busy', String(busy));
  };
  const choose = (option, row) => {
    selected = option; checkbox.checked = false;
    row.append(confirmation);
    confirmation.hidden = !option.manual;
    confirmationText.textContent = `אישרתי מול הסטודיו שהמנוי כולל ${data.category_name}`;
    update();
  };
  const reload = async () => {
    if (busy || !isCurrent()) return;
    busy = true; status.textContent = 'מעדכנים מנויים ומכסה…'; update();
    try {
      const result = await load();
      if (!isCurrent()) return;
      data = result; selected = null; saved = false; requireRefresh = false;
      checkbox.checked = false; confirmation.hidden = true;
      optionsHost.replaceChildren(node('legend', 'באיזה מנוי להשתמש?'));
      for (const option of data.options || []) {
        const row = node('div', null, 'membership-choice-row');
        const label = node('label', null, 'membership-choice-option');
        const radio = node('input'); radio.type = 'radio'; radio.name = group;
        radio.value = String(option.id); radio.disabled = !option.available;
        const text = node('span'); text.append(node('span', option.name + (option.entries != null ? ` · ${option.entries} כניסות` : '')));
        const detail = !option.available ? option.reason : option.manual ? 'נדרש אישור שסוג השיעור כלול' :
          `${option.preflight ? 'עבר בדיקה מוקדמת' : 'מתאים'}${option.remaining_after != null ? ` · ${option.remaining_after} פנויים לאחר השיוך` : ''}`;
        text.append(node('small', detail)); label.append(radio, text);
        row.append(label);
        radio.onchange = () => choose(option, row);
        if (option.selected && option.available) { radio.checked = true; choose(option, row); }
        optionsHost.append(row);
      }
      status.textContent = data.options?.length ? '' : 'לא נמצאו מנויים. אפשר לרענן אחרי עדכון בסטודיו.';
      onInventory?.(data.memberships || []);
    } catch (error) {
      requireRefresh = true; status.textContent = error.message || 'לא ניתן לטעון את המנויים';
    } finally { busy = false; update(); }
  };
  refresh.onclick = reload;
  checkbox.onchange = update;
  submit.onclick = async () => {
    if (submit.disabled || !isCurrent()) return;
    busy = true; status.textContent = 'שומרים את הבחירה…'; update();
    try {
      const result = await save({token: selected.token, confirm_category: !!selected.manual && checkbox.checked});
      if (!isCurrent()) return;
      saved = true; status.textContent = result.message || 'המנוי שויך לאימון';
      try { await onSaved?.(result); }
      catch { status.textContent += ' · יש לרענן את רשימת האימונים'; }
    } catch (error) {
      // An interrupted response must not silently repeat a planning change.
      requireRefresh = true;
      status.textContent = `${error.message || 'השמירה לא הושלמה'}. רעננו את המנויים לפני ניסיון נוסף.`;
    } finally { busy = false; update(); }
  };
  root.ready = reload();
  return root;
}
