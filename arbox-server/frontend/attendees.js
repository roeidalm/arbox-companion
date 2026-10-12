/* One on-demand roster for Mine and the schedule. No persistent name cache. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.Attendees = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  function create({document, request, getStudio}) {
    const element = (tag, className, text) => {
      const el = document.createElement(tag);
      if (className) el.className = className;
      if (text) el.textContent = text;
      return el;
    };
    const dialog = element('dialog', 'attendees-dialog');
    dialog.setAttribute('aria-labelledby', 'attendeesTitle');
    dialog.setAttribute('aria-describedby', 'attendeesSession');
    const head = element('div', 'attendees-head');
    const title = element('h2', '', 'נרשמים לאימון'); title.id = 'attendeesTitle';
    const closeButton = element('button', 'icon-close', '×');
    closeButton.type = 'button'; closeButton.setAttribute('aria-label', 'סגירת רשימת הנרשמים');
    const subtitle = element('p', 'attendees-session'); subtitle.id = 'attendeesSession';
    const status = element('p', 'attendees-status'); status.setAttribute('role', 'status');
    const list = element('ul', 'attendees-list'); list.setAttribute('aria-label', 'רשימת הנרשמים');
    const retry = element('button', 'attendees-retry', 'ניסיון נוסף'); retry.type = 'button'; retry.hidden = true;
    head.append(title, closeButton); dialog.append(head, subtitle, status, list, retry);
    document.body.append(dialog);
    let generation = 0, controller = null, context = null;
    function invalidate() {
      generation++; controller?.abort(); controller = null;
      list.replaceChildren(); status.textContent = ''; retry.hidden = true;
    }
    function close() {
      invalidate(); context = null;
      if (dialog.open) dialog.close();
    }
    closeButton.onclick = close;
    dialog.addEventListener('close', () => { if (!dialog.open) { invalidate(); context = null; } });
    dialog.addEventListener('click', event => {
      if (event.target !== dialog) return;
      const r = dialog.getBoundingClientRect();
      if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) close();
    });
    async function load() {
      invalidate();
      const token = generation, active = context;
      if (!active || active.studio !== getStudio()) { close(); return; }
      controller = new AbortController();
      status.textContent = 'טוען נרשמים…';
      const current = () => token === generation && dialog.open && active.studio === getStudio();
      try {
        const data = await request(active.session.schedule_id, active.studio, controller.signal);
        if (!current()) return;
        if (data.studio_id !== active.studio || data.schedule_id !== active.session.schedule_id) {
          throw new Error('האימון השתנה. סגרו ופתחו את הרשימה מחדש');
        }
        if (data.status === 'hidden') {
          status.textContent = 'הסטודיו אינו מאפשר להציג את רשימת הנרשמים'; return;
        }
        if (data.status === 'unavailable') {
          status.textContent = 'רשימת השמות אינה זמינה בארבוקס לאימון הזה'; return;
        }
        if (data.status !== 'available' || !Array.isArray(data.attendees)) throw new Error('התקבלה רשימה לא תקינה');
        const count = data.attendees.length;
        status.textContent = !count ? 'אין נרשמים לאימון כרגע' :
          Number.isInteger(data.registered) && data.registered !== count
            ? `מוצגים ${count} שמות · ${data.registered} נרשמים בסך הכול`
            : `${count} נרשמים`;
        for (const person of data.attendees) list.append(element('li', '', person.name));
      } catch (error) {
        if (!current() || error.name === 'AbortError') return;
        status.textContent = error.message || 'לא ניתן לטעון נרשמים כרגע'; retry.hidden = false;
      }
    }
    retry.onclick = load;
    function open(session, studio, label) {
      invalidate(); context = {session, studio}; subtitle.textContent = label;
      if (!dialog.open) dialog.showModal();
      closeButton.focus();
      return load();
    }
    return {open, close};
  }
  return {create};
});
