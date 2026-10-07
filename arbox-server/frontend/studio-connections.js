/* Settings for discovering additional studios. Selection stays in the header. */
const node = (tag, className, text) => {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
};

function checkedLabel(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : 'נבדק: ' + date.toLocaleString('he-IL', {
    day: 'numeric', month: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

export function mountConnections(host, api, {afterEnable = async () => {}} = {}) {
  let connections = [], busy = false, message = '', failed = false;
  let brandOpen = false, brandName = '';

  const accept = data => {
    if (!Array.isArray(data?.connections)) throw new Error('תשובת החיבורים אינה זמינה. נסו שוב.');
    connections = data.connections;
  };

  const run = async (request, success, enabled = false) => {
    if (busy) return;
    busy = true; failed = false; message = '';
    render();
    try {
      const response = await request();
      accept(response);
      message = response.warning || success;
      if (enabled) {
        try { await afterEnable(); }
        catch (error) {
          failed = true;
          message = 'הסטודיו נוסף, אך הבורר למעלה לא התרענן. רעננו את העמוד כדי לראות אותו.';
        }
      }
    } catch (error) {
      failed = true;
      message = 'הפעולה לא הושלמה: ' + error.message;
    } finally {
      busy = false;
      render();
    }
  };

  const discover = whitelabel => run(() => api('/api/connections/discover', {
    method: 'POST', body: JSON.stringify({whitelabel}),
  }), 'הבדיקה הסתיימה. אפשר להוסיף את הסטודיואים שנמצאו.');

  const button = (label, action, primary = false) => {
    const el = node('button', primary ? 'primary' : '', label);
    el.type = 'button'; el.disabled = busy; el.onclick = action;
    return el;
  };

  function render() {
    host.replaceChildren();
    host.setAttribute('aria-busy', String(busy));
    const list = node('div', 'sc-list');
    // Arbox is always offered, even before the first discovery.
    const entries = connections.some(c => String(c.whitelabel).toLowerCase() === 'arbox')
      ? connections : [...connections, {id: null, whitelabel: 'Arbox', studios: []}];
    const enabledIds = new Set(connections.flatMap(c => (c.studios || [])
      .filter(s => s.enabled).map(s => String(s.id))));
    const offeredIds = new Set();
    for (const connection of entries) {
      const card = node('section', 'sc-connection');
      const head = node('div', 'sc-head');
      const identity = node('div', 'sc-identity');
      const title = node('h3', '', connection.whitelabel || 'Arbox'); title.dir = 'auto';
      const hasError = !!connection.error || ['error', 'failed'].includes(connection.status);
      const status = !connection.id ? 'מוכן לחיפוש סטודיואים'
        : hasError ? 'נדרשת בדיקת חיבור'
        : ['connected', 'ready', 'ok'].includes(connection.status) ? 'מחובר'
        : 'ממתין לבדיקה';
      identity.append(title, node('span', 'sc-status' + (hasError ? ' warn' : ''), status));
      const search = button(busy ? 'בודק…' : connection.id ? 'רענון חיבור' : 'חיפוש סטודיואים נוספים',
        () => discover(connection.whitelabel || 'Arbox'), !connection.id);
      head.append(identity, search); card.append(head);
      if (hasError) card.append(node('p', 'sc-error', connection.error || 'לא ניתן להתחבר כרגע. אפשר לנסות שוב.'));
      const studios = connection.studios || [];
      for (const studio of studios) {
        const id = String(studio.id);
        const row = node('div', 'sc-studio');
        const details = node('div', 'sc-identity');
        const name = node('strong', '', studio.name); name.dir = 'auto';
        const alreadyEnabled = studio.duplicate || enabledIds.has(id);
        details.append(name, node('small', 'hint', alreadyEnabled
          ? 'נוסף לבורר הסטודיואים למעלה'
          : studio.has_active_membership === true ? 'נמצא בחשבון שלך'
          : studio.has_active_membership === false ? 'לא נמצא מנוי פעיל'
          : 'לא ניתן לאמת את המנוי כרגע. רעננו את החיבור.'));
        row.append(details);
        if (alreadyEnabled) row.append(node('span', 'sc-added', '✓ נוסף'));
        else if (offeredIds.has(id)) row.append(node('span', 'hint', 'מופיע גם בחיבור שלמעלה'));
        else {
          const add = button('הוסף לסטודיואים שלי', () => run(
            () => api('/api/connections/enable', {method: 'POST', body: JSON.stringify({
              connection_id: connection.id, studio_id: studio.id,
            })}), `${studio.name} נוסף. אפשר לבחור בו בבורר למעלה.`, true), true);
          add.disabled = busy || hasError || connection.status !== 'connected' || studio.has_active_membership !== true;
          row.append(add);
          if (!hasError && connection.status === 'connected' && studio.has_active_membership === true) offeredIds.add(id);
        }
        card.append(row);
      }
      if (connection.id && !studios.length && connection.status === 'connected')
        card.append(node('p', 'hint sc-empty', 'לא נמצאו סטודיואים בחיבור הזה.'));
      const checked = checkedLabel(connection.last_checked || connection.checked_at);
      if (checked) card.append(node('small', 'sc-checked', checked));
      list.append(card);
    }
    host.append(list);
    const brandToggle = button(brandOpen ? 'סגירת הוספת חיבור' : '+ חיבור לאפליקציה ממותגת נוספת', () => {
      brandOpen = !brandOpen; render();
      if (brandOpen) host.querySelector('input')?.focus();
    });
    brandToggle.className = 'sc-brand-toggle';
    brandToggle.setAttribute('aria-expanded', String(brandOpen));
    host.append(brandToggle);
    if (brandOpen) {
      const form = node('form', 'sc-brand-form');
      const label = node('label', '', 'שם האפליקציה'); label.htmlFor = 'studioConnectionBrand';
      const input = node('input'); input.id = 'studioConnectionBrand'; input.dir = 'ltr';
      input.autocomplete = 'off'; input.placeholder = 'למשל Moveom';
      input.required = true; input.maxLength = 64; input.value = brandName; input.disabled = busy;
      input.oninput = () => { brandName = input.value; };
      const row = node('div', 'sc-brand-row');
      const submit = button(busy ? 'בודק…' : 'בדיקת חיבור', null, true); submit.type = 'submit';
      row.append(input, submit);
      form.append(label, row, node('p', 'hint', 'שם האפליקציה הממותגת שבה מתחברים, לא בהכרח שם הסטודיו. נשתמש בפרטי ההתחברות השמורים.'));
      form.onsubmit = event => {
        event.preventDefault();
        const name = input.value.trim();
        if (!name || busy) return;
        brandName = name;
        return discover(name);
      };
      host.append(form);
    }
    const notice = node('p', 'sc-message' + (failed ? ' warn' : ''), message || (busy ? 'בודק חיבורים…' : ''));
    notice.setAttribute('role', 'status'); notice.setAttribute('aria-live', 'polite');
    host.append(notice);
  }

  async function refresh() {
    if (busy) return;
    busy = true; message = ''; failed = false; render();
    try { accept(await api('/api/connections')); }
    catch (error) { failed = true; message = 'לא ניתן לטעון חיבורים: ' + error.message; }
    finally { busy = false; render(); }
  }

  render();
  return {refresh, ready: refresh()};
}
