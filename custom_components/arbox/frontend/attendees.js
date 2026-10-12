/* One on-demand roster for Mine and the schedule. No persistent roster cache. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.Attendees = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  function create({document, request, getStudio, mount = document.body}) {
    const element = (tag, className, text) => {
      const el = document.createElement(tag);
      if (className) el.className = className;
      if (text) el.textContent = text;
      return el;
    };
    const dialog = element('dialog', 'attendees-dialog');
    dialog.dir = 'rtl'; dialog.lang = 'he';
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
    const photoDialog = element('dialog', 'attendees-photo-dialog');
    photoDialog.dir = 'rtl'; photoDialog.lang = 'he';
    photoDialog.setAttribute('aria-label', 'תמונת נרשם מוגדלת');
    const largePhoto = element('img', 'attendees-photo-large');
    largePhoto.referrerPolicy = 'no-referrer';
    const photoClose = element('button', 'attendees-photo-close', '×');
    photoClose.type = 'button'; photoClose.setAttribute('aria-label', 'סגירת התמונה');
    photoDialog.append(largePhoto, photoClose);
    function clearPhoto() { largePhoto.removeAttribute('src'); largePhoto.alt = ''; }
    function closePhoto() {
      if (photoDialog.open) photoDialog.close();
      clearPhoto();
    }
    photoClose.onclick = closePhoto;
    photoDialog.addEventListener('close', () => { if (!photoDialog.open) clearPhoto(); });
    photoDialog.addEventListener('click', event => { if (event.target !== largePhoto) closePhoto(); });
    largePhoto.onerror = closePhoto;
    function attendee(person) {
      const row = element('li');
      const avatar = element(person.image_url ? 'button' : 'span', 'attendees-avatar');
      if (person.image_url) {
        avatar.type = 'button'; avatar.disabled = true;
        avatar.setAttribute('aria-label', `הגדלת התמונה של ${person.name}`);
        avatar.setAttribute('aria-haspopup', 'dialog');
        avatar.onclick = () => {
          if (avatar.disabled || !dialog.open || context?.studio !== getStudio()) return;
          largePhoto.alt = person.name; largePhoto.src = person.image_url;
          photoDialog.showModal(); photoClose.focus();
        };
      } else avatar.setAttribute('aria-hidden', 'true');
      const parts = person.name.trim().split(/\s+/);
      const initials = [parts[0], ...(parts.length > 1 ? [parts.at(-1)] : [])]
        .map(part => Array.from(part)[0]).join('');
      const fallback = element('span', '', initials); fallback.setAttribute('aria-hidden', 'true');
      avatar.append(fallback);
      if (person.image_url) {
        const img = element('img', 'attendees-photo');
        img.alt = ''; img.width = 44; img.height = 44;
        img.loading = 'lazy'; img.decoding = 'async'; img.referrerPolicy = 'no-referrer';
        img.onload = () => { fallback.hidden = true; img.className = 'attendees-photo is-loaded'; avatar.disabled = false; };
        img.onerror = () => { fallback.hidden = false; avatar.disabled = true; avatar.replaceChildren(fallback); };
        img.src = person.image_url;
        avatar.append(img);
      }
      row.append(avatar, element('span', 'attendees-name', person.name));
      return row;
    }
    head.append(title, closeButton); dialog.append(head, subtitle, status, list, retry);
    mount.append(dialog, photoDialog);
    let generation = 0, controller = null, context = null;
    function invalidate() {
      closePhoto();
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
        for (const person of data.attendees) list.append(attendee(person));
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
    return {open, close, destroy() { close(); dialog.remove(); photoDialog.remove(); }};
  }
  return {create};
});
