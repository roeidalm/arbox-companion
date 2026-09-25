/* Optional per-kind destinations, hidden behind Advanced. */
function renderNotificationRoutes(settings) {
  const host = document.querySelector('#notificationRoutes'); host.replaceChildren();
  const kinds = {log:'אירועי מערכת',digest:'הודעה לילית',autobook:'הזמנות אוטומטיות',standby:'רשימת המתנה',studio:'הודעות מהסטודיו',latecancel:'לפני נעילת ביטול',vacation:'חופשות',attendance:'אישור הגעה',membership:'עדכוני מנויים',journal:'משוב אימון',system:'הודעות כלליות'};
  for (const [channel, title, placeholder] of [['discord','Discord','מזהה ערוץ או Webhook'],['telegram','טלגרם','Chat ID'],['ha','Home Assistant','Webhook URL']]) {
    const details = document.createElement('details'), summary = document.createElement('summary');
    summary.textContent = title; details.append(summary);
    for (const [kind, label] of Object.entries(kinds)) {
      const saved = settings[channel]?.routes?.[kind] || {};
      const row = document.createElement('div'); row.className = 'field-row notification-route';
      const field = document.createElement('label'); field.textContent = label;
      const input = document.createElement('input'); input.type = channel === 'ha' || saved.target === '***' ? 'password' : 'text';
      input.autocomplete = 'off'; input.placeholder = placeholder + ' · ריק = הראשי'; input.value = saved.target || '';
      input.dataset.routeChannel = channel; input.dataset.routeKind = kind; field.append(input);
      const copyLabel = document.createElement('label'), copy = document.createElement('input');
      copy.type = 'checkbox'; copy.checked = !!saved.copy; copy.dataset.routeCopy = channel + ':' + kind;
      copyLabel.append(copy, ' גם לראשי');
      const test = document.createElement('button'); test.type = 'button'; test.textContent = 'בדיקת יעד';
      test.onclick = async () => {
        test.disabled = true;
        try {
          if (!await saveSettings()) return;
          const result = await api(`/api/settings/test/${channel}?kind=${kind}`, {method:'POST'});
          toast(result.state === 'pending' ? 'הבדיקה ממתינה בתור; המסירה טרם אושרה' : 'השירות אישר קבלת הבדיקה');
        } catch (e) { toast('בדיקת היעד נכשלה: ' + e.message); }
        finally { test.disabled = false; }
      };
      row.append(field, copyLabel, test); details.append(row);
    }
    host.append(details);
  }
}
function collectNotificationRoutes(channel) {
  const result = {};
  for (const input of document.querySelectorAll(`[data-route-channel="${channel}"]`)) {
    const kind = input.dataset.routeKind;
    result[kind] = {target:input.value.trim(), copy:document.querySelector(`[data-route-copy="${channel}:${kind}"]`).checked};
  }
  return result;
}
