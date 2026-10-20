/* Shared social view. Only capabilities verified by the server are displayed. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.ArboxFriends = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  function decorate(host, data) {
    const sessions = new Map((data?.sessions || []).map(s => [String(s.schedule_id), s]));
    for (const button of host.querySelectorAll('[data-social-schedule]')) {
      button.dataset.socialLabel ||= button.textContent;
      const matches = sessions.get(button.dataset.socialSchedule)?.connections || [];
      button.textContent = button.dataset.socialLabel + (matches.length ? ` · ${matches.length} קשרים רשומים` : '');
      if (matches.length) button.title = matches.map(p => p.name).join(' · ');
      else button.removeAttribute('title');
    }
  }
  function render({document, host, data, openSession}) {
    const el = (tag, text, cls) => {
      const node = document.createElement(tag);
      if (text) node.textContent = text;
      if (cls) node.className = cls;
      return node;
    };
    host.replaceChildren();
    const note = el('p', 'מוצגים קשרים שהגיעו מארבוקס. סטטוס האישור שלהם עדיין לא אומת; חיפוש אנשים וניהול בקשות זמינים בינתיים באפליקציית הסטודיו.', 'hint');
    const range = el('p', `שיעורים בטווח ${data.date_from} – ${data.date_to}. מוצגים רק שיעורים שבמערכת ושרשימת הנרשמים שלהם גלויה.`, 'hint');
    host.append(note, range);
    if (data.status === 'unavailable') {
      host.append(el('p', 'רשימת הקשרים אינה זמינה בארבוקס כרגע.', 'empty')); return;
    }
    if (!data.people.length) {
      host.append(el('p', 'אין קשרים להצגה בסטודיו הזה.', 'empty')); return;
    }
    const input = el('input');
    input.type = 'search'; input.placeholder = 'סינון הקשרים שלי';
    input.setAttribute('aria-label', 'סינון הקשרים שלי');
    const list = el('div', '', 'friends-list');
    const empty = el('p', 'אין תוצאות לחיפוש הזה.', 'empty'); empty.hidden = true;
    host.append(input, list, empty);
    const cards = [];
    for (const person of data.people) {
      const card = el('details', '', 'friend-card');
      const summary = el('summary');
      const avatar = el('span', '', 'friend-avatar');
      const fallback = el('span', person.name.trim().split(/\s+/).map(s => Array.from(s)[0]).slice(0, 2).join(''));
      avatar.append(fallback);
      if (person.image_url) {
        const img = el('img'); img.alt = ''; img.loading = 'lazy'; img.referrerPolicy = 'no-referrer';
        img.onload = () => { fallback.hidden = true; };
        img.onerror = () => { img.remove(); fallback.hidden = false; };
        img.src = person.image_url; avatar.append(img);
      }
      summary.append(avatar, el('span', person.name), el('small', 'קשר מארבוקס · סטטוס לא מאומת'));
      card.append(summary);
      const sessions = data.sessions.filter(s => s.connections.some(p => p.user_id === person.user_id));
      if (!sessions.length) card.append(el('p', 'לא נמצאו שיעורים גלויים בטווח הזה.', 'hint'));
      for (const s of sessions) {
        const row = el('div', '', 'friend-session');
        row.append(el('span', `${s.date} · ${s.start_time} · ${s.category_name || 'אימון'}${s.coach_name ? ' · ' + s.coach_name : ''}`));
        const button = el('button', 'פרטי האימון והרשמה');
        button.type = 'button'; button.onclick = () => openSession(s);
        row.append(button); card.append(row);
      }
      cards.push({card, name: person.name.toLocaleLowerCase()}); list.append(card);
    }
    input.oninput = () => {
      const query = input.value.trim().toLocaleLowerCase();
      for (const {card, name} of cards) card.hidden = !name.includes(query);
      empty.hidden = cards.some(({card}) => !card.hidden);
    };
  }
  return {render, decorate};
});
