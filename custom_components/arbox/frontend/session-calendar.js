/* Export through HA's fixed authenticated resource; the server builds the event. */
export function calendarLinks(session, read, report) {
  const wrap = document.createElement('span'); wrap.className = 'session-calendar-links';
  let pending;
  const data = () => pending ||= read().finally(() => {pending = null;});
  const button = (label, run) => {
    const b = document.createElement('button'); b.type = 'button'; b.textContent = label;
    b.onclick = async () => {
      if (b.disabled) return; b.disabled = true;
      try {await run();} catch(e) {report(e.message || 'לא ניתן לטעון את פרטי היומן');}
      finally {b.disabled = false;}
    };
    return b;
  };
  const file = button('📅 קובץ', async () => {
    const result = await data();
    if (typeof result.ics !== 'string' || !result.ics.startsWith('BEGIN:VCALENDAR')) throw new Error('קובץ היומן לא התקבל מהשרת');
    const url = URL.createObjectURL(new Blob([result.ics], {type:'text/calendar;charset=utf-8'}));
    const link = document.createElement('a'); link.href = url; link.download = `arbox-${session.schedule_id}.ics`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  });
  file.title = 'קובץ יומן עם המיקום והתזכורות שהגדרת';
  const google = button('🗓 Google', async () => {
    // Open synchronously with the gesture so mobile popup blockers allow it.
    const tab = window.open('about:blank', '_blank');
    if (tab) tab.opener = null;
    try {
      const result = await data();
      const url = new URL(result.google);
      if (url.protocol !== 'https:' || url.hostname !== 'calendar.google.com') throw new Error('כתובת היומן אינה תקינה');
      if (!tab) throw new Error('אפשרו פתיחת חלון ליומן ונסו שוב');
      tab.location.replace(url.href);
    } catch(e) {tab?.close(); throw e;}
  });
  wrap.append(file, google); return wrap;
}
