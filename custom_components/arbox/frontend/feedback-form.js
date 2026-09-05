// Shared by the standalone page and the authenticated Home Assistant panel.
export const feedbackTemplate = "<header><span class=\"brand\">A</span><span>Arbox <small>יומן האימונים שלך</small></span></header>\n<main><div id=\"loading\" role=\"status\">טוענים את האימון…</div><section id=\"failure\" hidden role=\"alert\"><h1>לא הצלחנו לפתוח את הטופס</h1><p id=\"error-text\"></p><button id=\"retry\">נסה שוב</button></section>\n<section id=\"complete\" hidden tabindex=\"-1\"><div class=\"success-icon\">✓</div><h1 id=\"complete-title\">המשוב נשמר</h1><p id=\"complete-text\">הדירוגים, ההערות והתרגילים מחכים לך ביומן האימונים.</p><p class=\"muted\">אפשר לסגור את החלון ולחזור ליום שלך.</p></section>\n<form id=\"feedback\" hidden><div id=\"demo\" class=\"demo\" hidden>🧪 תצוגת בדיקה · שום תשובה לא תישמר ביומן</div><p class=\"eyebrow\">רגע לעצמך, אחרי האימון</p><h1>איך היה האימון?</h1><article class=\"session\"><div class=\"session-icon\">✦</div><div><strong id=\"class-name\"></strong><p id=\"session-details\"></p></div></article>\n<div id=\"ratings\"></div>\n<details id=\"more\"><summary><span>הערות ותרגילים <small>לא חובה</small></span><span class=\"chevron\">⌄</span></summary><div class=\"details-body\"><label for=\"notes\">משהו שחשוב לזכור?</label><textarea id=\"notes\" maxlength=\"4000\" rows=\"3\" placeholder=\"מה עבד טוב, על מה תרצה לעבוד בפעם הבאה…\"></textarea><div class=\"exercises-title\"><h2>מה עשית באימון?</h2><small>מוסיפים רק מה שרוצים לעקוב אחריו</small></div><div id=\"exercises\"></div><button type=\"button\" class=\"secondary\" id=\"add-exercise\">＋ הוספת תרגיל</button></div></details>\n<div id=\"save-error\" role=\"alert\"></div><button class=\"primary\" id=\"save\" type=\"submit\">שמירת המשוב</button><p class=\"muted footer-note\" id=\"save-hint\">הכול נשמר ביומן האימונים שלך. אפשר לערוך שם בהמשך.</p></form></main>";

export function mountFeedback(root, request) {
  const $ = id => root.querySelector(`#${id}`);
  let data, counter = 0, busy = false;
  const metrics = {
    strength: [['sets', 'סטים'], ['reps', 'חזרות'], ['weight', 'משקל (ק״ג)']],
    reps: [['sets', 'סטים'], ['reps', 'חזרות']],
    duration: [['sets', 'סטים'], ['duration_seconds', 'שניות']],
    attempts: [['attempts', 'ניסיונות']], distance: [['distance', 'מרחק (מ׳)']], note: []
  };
  const el = (tag, text, cls) => { const node = document.createElement(tag); if (text) node.textContent = text; if (cls) node.className = cls; return node; };
  function finish(demo) {
    $('feedback').hidden = true; $('complete').hidden = false;
    $('complete-title').textContent = demo ? 'הבדיקה הסתיימה ✓' : 'המשוב נשמר';
    $('complete-text').textContent = demo ? 'כך תיראה השמירה באימון אמיתי. לא נשמר דבר ביומן ולא שונו נתוני ההגעה.' : 'הדירוגים, ההערות והתרגילים מחכים לך ביומן האימונים.';
    $('complete').focus();
  }
  function rating(name, title, subtitle) {
    const fieldset = el('fieldset'), legend = el('legend', title);
    if (subtitle) legend.append(el('small', subtitle));
    fieldset.append(legend);
    const options = el('div', '', 'rating-options');
    for (const [value, emoji, title] of [['negative', '😕', 'פחות'], ['neutral', '🙂', 'בסדר'], ['positive', '🤩', 'מעולה']]) {
      const label = el('label', '', 'rating-choice'), input = el('input');
      input.type = 'radio'; input.name = name; input.value = value;
      const span = el('span'); span.append(el('b', emoji), document.createTextNode(title));
      label.append(input, span); options.append(label);
    }
    fieldset.append(options);
    const na = el('label', '', 'na'), input = el('input'); input.type = 'radio'; input.name = name; input.value = 'not_applicable';
    na.append(input, document.createTextNode('אין לי דירוג הפעם')); fieldset.append(na); $('ratings').append(fieldset);
  }
  function exercise(seed) {
    if ($('exercises').children.length >= 50) return;
    const number = ++counter, card = el('article', '', 'exercise'), top = el('div', '', 'exercise-top');
    top.append(el('strong', 'תרגיל')); const remove = el('button', 'הסרה', 'remove'); remove.type = 'button';
    remove.onclick = () => {card.remove(); $('add-exercise').disabled = false;}; top.append(remove); card.append(top);
    const label = el('label', 'בחירת תרגיל'), select = el('select'); select.id = `exercise-${number}`; label.htmlFor = select.id;
    select.required = true; select.append(new Option('בחרו תרגיל…', ''));
    data.catalogue.forEach((item, index) => select.append(new Option(item.name, String(index))));
    select.append(new Option('תרגיל אחר / טקסט חופשי', 'custom'));
    card.append(label, select);
    const extra = el('div'), fields = el('div', '', 'metrics'); card.append(extra, fields);
    let current = null;
    const drawMetrics = kind => {
      fields.replaceChildren();
      for (const [key, title] of metrics[kind] || []) {
        const wrap = el('label', title, 'metric'), input = el('input'); input.type = 'number'; input.min = '0';
        input.max = ['weight', 'distance'].includes(key) ? '1000000' : '100000'; input.step = ['weight', 'distance'].includes(key) ? 'any' : '1';
        input.dataset.field = key; input.inputMode = ['weight', 'distance'].includes(key) ? 'decimal' : 'numeric'; wrap.append(input); fields.append(wrap);
      }
    };
    select.onchange = () => {
      extra.replaceChildren(); current = select.value === 'custom' ? {name: '', metric_type: 'note'} : data.catalogue[Number(select.value)];
      if (!select.value) {current = null; fields.replaceChildren(); return;}
      if (select.value === 'custom') {
        const customLabel = el('label', 'שם התרגיל'), name = el('input'); name.className = 'custom-name'; name.type = 'text'; name.required = true; name.maxLength = 120;
        name.id = `custom-${number}`; customLabel.htmlFor = name.id; name.oninput = () => current.name = name.value;
        const typeLabel = el('label', 'מה מודדים?'), type = el('select'); type.id = `metric-${number}`; typeLabel.htmlFor = type.id;
        Object.entries(data.metric_labels).forEach(([key, label]) => type.append(new Option(label, key))); type.value = 'note';
        type.onchange = () => {current.metric_type = type.value; drawMetrics(type.value);}; extra.append(customLabel, name, typeLabel, type);
      }
      drawMetrics(current.metric_type);
    };
    const notesLabel = el('label', 'הערה לתרגיל (לא חובה)'), notes = el('input'); notes.id = `exercise-notes-${number}`; notes.type = 'text'; notes.maxLength = 4000; notesLabel.htmlFor = notes.id;
    card.append(notesLabel, notes);
    card.read = () => { const item = {exercise_id: current.id || null, name: current.name.trim(), metric_type: current.metric_type, notes: notes.value.trim() || null};
      fields.querySelectorAll('input').forEach(input => {item[input.dataset.field] = input.value === '' ? null : Number(input.value);}); return item; };
    $('exercises').append(card);
    if (seed) {
      const index = data.catalogue.findIndex(item => seed.exercise_id ? item.id === seed.exercise_id : item.name === seed.name);
      select.value = index >= 0 ? String(index) : 'custom'; select.onchange();
      if (index < 0) {
        const name = extra.querySelector('.custom-name'); name.value = seed.name || ''; name.oninput();
        const type = extra.querySelector('select'); type.value = seed.metric_type || 'note'; type.onchange();
      }
      fields.querySelectorAll('input').forEach(input => {input.value = seed[input.dataset.field] ?? '';});
      notes.value = seed.notes || '';
    } else select.focus(); $('add-exercise').disabled = $('exercises').children.length >= 50;
  }
  async function load() {
    $('failure').hidden = true; $('loading').hidden = false;
    try {
      data = await request('GET'); $('loading').hidden = true;
      if (data.complete) {finish(data.demo); return;}
      $('demo').hidden = !data.demo;
      $('class-name').textContent = data.session.category_name || 'האימון שלך';
      const date = data.session.date ? new Date(`${data.session.date}T12:00:00`).toLocaleDateString('he-IL', {weekday:'long', day:'numeric', month:'numeric'}) : '';
      $('session-details').textContent = [data.session.coach_name, date, data.session.start_time?.slice(0,5)].filter(Boolean).join(' · ');
      $('ratings').replaceChildren();
      if (data.level !== 'quick' || !data.session.coach_name) rating('class_feedback', 'איך היה השיעור?');
      if (data.session.coach_name) rating('coach_feedback', 'איך היה עם המאמן/ת?', data.session.coach_name);
      $('more').hidden = data.level === 'quick';
      $('more').open = data.level === 'full';
      $('save-hint').textContent = data.demo ? 'זוהי בדיקה בלבד. אפשר להתנסות בחופשיות — דבר לא יישמר ביומן.' : 'הכול נשמר ביומן האימונים שלך. אפשר לערוך שם בהמשך.';
      if (data.saved) {
        for (const name of ['class_feedback', 'coach_feedback']) {
          const input = Array.from($('ratings').querySelectorAll('input')).find(i => i.name === name && i.value === data.saved[name]);
          if (input) input.checked = true;
        }
        $('notes').value = data.saved.notes || '';
        $('exercises').replaceChildren();
        for (const item of data.saved.exercises || []) exercise(item);
      }
      $('feedback').hidden = false;
    } catch (error) {$('loading').hidden = true; $('failure').hidden = false; $('error-text').textContent = error.message;}
  }
  $('add-exercise').onclick = () => exercise(); $('retry').onclick = load;
  $('feedback').onsubmit = async event => {
    event.preventDefault(); if (busy) return; $('save-error').textContent = '';
    const form = new FormData(event.target), body = {class_feedback: form.get('class_feedback'), coach_feedback: form.get('coach_feedback'), notes: $('notes').value.trim(), exercises: Array.from($('exercises').children, card => card.read())};
    if (!body.class_feedback && !body.coach_feedback && !body.notes && !body.exercises.length) {$('save-error').textContent = 'בחרו דירוג או הוסיפו הערה לפני השמירה.'; return;}
    busy = true; $('save').disabled = true; $('save').textContent = 'שומרים…';
    try {const result = await request('PUT', body); finish(result.demo);}
    catch (error) {$('save-error').textContent = `המשוב עדיין לא נשמר. ${error.message}`;}
    finally {busy = false; $('save').disabled = false; $('save').textContent = 'שמירת המשוב';}
  };
  return load();
}
