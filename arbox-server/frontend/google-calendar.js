/* Optional Google connection. Secrets are uploaded once and never rendered. */
(function (root) {
  'use strict';
  const kinds = {scheduled:'מתוזמן', automation:'אוטומציה', booked:'רשום', standby:'רשימת המתנה', review:'דורש בדיקה'};
  const colors = {
    1:['לבנדר','#7986cb'],2:['מרווה','#33b679'],3:['סגול','#8e24aa'],4:['ורוד','#e67c73'],
    5:['צהוב','#f6bf26'],6:['כתום','#f4511e'],7:['טורקיז','#039be5'],8:['אפור','#616161'],
    9:['כחול','#3f51b5'],10:['ירוק','#0b8043'],11:['אדום','#d50000']
  };
  function projectId(value) {
    let id = String(value || '').trim();
    try { if (id.startsWith('https://')) id = new URL(id).searchParams.get('project') || ''; } catch (_) { return ''; }
    return /^[a-z][a-z0-9-]{4,28}[a-z0-9]$/.test(id) ? id : '';
  }
  function readUpload(data, host) {
    if (!data || !data.web || !data.web.client_id || !data.web.client_secret) throw Error('בחרו קובץ JSON של Web application שהורד מ־Google.');
    const options = (data.web.redirect_uris || []).filter(uri => {
      try { const u = new URL(uri); return u.protocol === 'https:' && u.hostname === host && u.pathname === '/api/calendar/google/callback' && !u.search && !u.hash && !u.username && !u.password; } catch (_) { return false; }
    });
    if (!options.length) throw Error('בקובץ אין כתובת חזרה מתאימה לשרת הזה. הוסיפו ב־Google את הכתובת מהאשף והורידו JSON מעודכן.');
    return options;
  }
  function reminderText(minutes) {
    return minutes === 0 ? 'בזמן האימון' : minutes === 60 ? 'שעה לפני' : minutes % 60 === 0 ? `${minutes / 60} שעות לפני` : `${minutes} דקות לפני`;
  }
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent=text; if(cls)n.className=cls; return n; };
  let refresh;
  function mount(host, api) {
    let status = null, prefs = null, selected = 'booked', step = 1, busy = false, uploaded = null;
    host.innerHTML = `<div class="gc-heading"><div><h2>האימונים מגיעים ליומן שלך</h2><p>מחברים את Google פעם אחת. האימונים, הצבעים והתזכורות מתעדכנים אוטומטית.</p></div><span class="gc-badge" id="gcBadge">חיבור אופציונלי</span></div>
      <ol class="gc-steps" aria-label="שלבי חיבור"><li><button type="button" data-step="1">1 · הכנת Google</button></li><li><button type="button" data-step="2">2 · העלאת קובץ</button></li><li><button type="button" data-step="3">3 · היומן שלך</button></li></ol>
      <p id="gcMessage" role="status" aria-live="polite" hidden></p>
      <section data-gc-step="1"><h3>נכין את החיבור ל־Google</h3><p>הגדרה חד־פעמית בחשבון שלך. בכל שלב נפתח את המסך המתאים ונציג בדיוק מה למלא.</p>
      <label for="gcProject">קישור לפרויקט Google או Project ID</label><input id="gcProject" dir="ltr" placeholder="הדביקו קישור מהדפדפן של Google Cloud">
      <div id="gcGuide"></div><button type="button" class="primary" id="gcPrepared">כבר יש לי קובץ JSON — להעלאה</button></section>
      <section data-gc-step="2" hidden><h3>מעלים את הקובץ ש־Google נתן לך</h3><p>אין צורך לפתוח אותו או להעתיק מתוכו פרטים. הקובץ נשמר בשרת שלך בלבד.</p>
      <label class="gc-upload" for="gcFile"><strong>בחירת קובץ JSON</strong><span>אפשר גם לגרור את הקובץ לכאן</span><input id="gcFile" type="file" accept=".json,application/json"></label>
      <p id="gcFileInfo" role="status"></p><label id="gcRedirectLabel" hidden>כתובת החזרה מתוך הקובץ<select id="gcRedirect" dir="ltr"></select></label>
      <div class="gc-actions"><button type="button" class="primary" id="gcUpload" disabled>שמירת הקובץ</button><button type="button" id="gcConnect" hidden>חיבור ל־Google</button></div>
      <p class="hint">לאחר השמירה נפתח את Google לבחירת חשבון ולאישור גישה ליומן הייעודי.</p></section>
      <section data-gc-step="3" hidden><div class="gc-connected"><div><h3 id="gcConnectionTitle">כך ייראה היומן שלך</h3><p id="gcConnectionInfo">בחרו צבע ותזכורות לכל מצב. ניתן לשנות הכול גם בהמשך.</p></div></div>
      <div class="gc-personalize"><div><div id="gcKindList" class="gc-kind-list" role="group" aria-label="מצבי האימון"></div>
      <div class="gc-editor"><h4 id="gcEditorTitle"></h4><label class="gc-check"><input type="checkbox" id="gcVisible">הצגת המצב הזה ביומן</label>
      <label for="gcColor">צבע האירוע</label><select id="gcColor"></select>
      <label class="gc-check"><input type="checkbox" id="gcBusy">סימון הזמן כ״עסוק״</label>
      <h4>תזכורות לפני האימון</h4><div class="gc-reminders" id="gcReminders"></div>
      <div class="gc-reminder-add"><label for="gcMinutes">דקות לפני</label><input type="number" id="gcMinutes" min="0" max="40320" value="30"><button type="button" id="gcAddReminder">הוסף תזכורת</button></div><p class="hint">עד 5 תזכורות לכל אירוע. ללא תזכורות? הסירו את כולן.</p></div></div>
      <aside class="gc-preview"><p class="gc-preview-label">תצוגה מקדימה · דוגמה</p><div class="gc-day"><strong>יום רביעי</strong><span>האימונים שלי</span></div><div class="gc-timegrid"><span>08:00</span><article id="gcPreviewEvent"><strong>Movement basics</strong><span>08:00–09:00 · רוני גוזלי</span><b id="gcPreviewStatus"></b><div id="gcPreviewAlarms"></div></article><span>09:00</span></div><p class="hint">כשמצב האימון משתנה, אותו אירוע מתעדכן ביומן.</p></aside></div>
      <div class="gc-actions"><button type="button" class="primary" id="gcSave">שמירת העדפות</button><button type="button" class="primary" id="gcEnable" hidden>יצירת יומן והפעלת הסנכרון</button><button type="button" id="gcSync" hidden>סנכרון עכשיו</button><button type="button" id="gcPause" hidden>השהיית הסנכרון</button><button type="button" id="gcDisconnect" hidden>ניתוק Google</button></div>
      <p class="hint">הסנכרון מציג את 30 הימים הקרובים ומתעדכן בכל דקה. ביטול או דילוג מסירים אירוע עתידי. השהיה וניתוק משאירים את האירועים שכבר נוצרו. שינויים בהרשמות עושים ב־Arbox Companion. עריכות ידניות באירועים המנוהלים ב־Google נדרסות בבדיקה תקופתית.</p>
      <div id="gcRecovery" hidden><p>אם היומן כבר נוצר ב־Google, אפשר לחבר אותו בלי ליצור עותק נוסף. בהגדרות היומן ב־Google, תחת ״שילוב היומן״, העתיקו את מזהה היומן.</p><input id="gcRecoverId" aria-label="מזהה היומן שנוצר" dir="ltr"><button id="gcRecover" type="button">חיבור ליומן שנוצר</button></div></section>`;
    const $ = s => host.querySelector(s);
    function message(text, error=false) { const n=$('#gcMessage');n.hidden=!text;n.textContent=text;n.className=error?'gc-error':'gc-success'; }
    function show(n) { step=n;host.querySelectorAll('[data-gc-step]').forEach(x=>x.hidden=Number(x.dataset.gcStep)!==n);host.querySelectorAll('[data-step]').forEach(b=>{b.classList.toggle('active',Number(b.dataset.step)===n);b.setAttribute('aria-current',Number(b.dataset.step)===n?'step':'false');}); }
    async function action(fn) { if(busy)return;busy=true;host.setAttribute('aria-busy','true');message('');try{await fn();}catch(e){message(e.message,true);}finally{busy=false;host.removeAttribute('aria-busy');} }
    function guide() {
      const id=projectId($('#gcProject').value), q=id?'?project='+encodeURIComponent(id):'';
      const suggested=status?.redirect_uri||status?.suggested_redirect||'';
      const steps=[
        ['יצירת פרויקט','https://console.cloud.google.com/projectcreate','שם הפרויקט: Arbox Calendar. לאחר היצירה הדביקו למעלה את הקישור מלוח הבקרה.'],
        ['הפעלת Calendar API','https://console.cloud.google.com/apis/library/calendar-json.googleapis.com'+q,'לחצו Enable. אם מופיע Manage, ה־API כבר פעיל.'],
        ['מסך ההתחברות','https://console.cloud.google.com/auth/branding'+q,'Get started → שם האפליקציה Arbox Calendar, המייל שלכם, קהל External, ופרטי קשר.'],
        ['משתמש בדיקה','https://console.cloud.google.com/auth/audience'+q,'ב־Test users הוסיפו את חשבון היומן שלכם. אם הוא כבר ברשימה, אין צורך להוסיף שוב.'],
        ['הרשאת היומן','https://console.cloud.google.com/auth/scopes'+q,'Add or remove scopes → הוסיפו את ההרשאה הבאה ושמרו:'],
        ['יצירת קובץ החיבור','https://console.cloud.google.com/auth/clients'+q,'Create client → Web application → שם: Arbox Calendar Connection. השאירו JavaScript origins ריק. ב־Authorized redirect URIs הדביקו את כתובת החזרה. לחצו Create והורידו JSON.']
      ];
      const out=$('#gcGuide');out.replaceChildren();
      steps.forEach(([title,url,text],i)=>{const d=el('details');if(i===0&&!id)d.open=true;d.append(el('summary',`${i+1}. ${title}`),el('p',text));if(i===4||i===5){const value=i===4?'https://www.googleapis.com/auth/calendar.app.created':suggested;const code=el('code',value||'נדרשת כתובת HTTPS תקינה של השרת עם הנתיב /api/calendar/google/callback');code.dir='ltr';d.append(code);if(value){const b=el('button','העתק');b.type='button';b.onclick=()=>action(async()=>{try{await navigator.clipboard.writeText(value);message('הועתק');}catch(_){message('סמנו את הטקסט המוצג והעתיקו אותו');}});d.append(b);}}const a=el('a','פתיחת המסך ב־Google');a.href=url;a.target='_blank';a.rel='noopener noreferrer';d.append(a);out.append(d);});
      out.append(el('p','במצב Testing הרשאת החיבור עשויה לפוג לאחר שבוע. לשימוש קבוע יש להסדיר מעבר ל־Production בהתאם לדרישות Google.','hint'));
    }
    function renderPrefs() {
      if(!prefs)return;
      const list=$('#gcKindList');list.replaceChildren();
      Object.entries(kinds).forEach(([k,label])=>{const b=el('button',label);b.type='button';b.className=k===selected?'active':'';b.setAttribute('aria-pressed',String(k===selected));const dot=el('span');dot.className='gc-dot';dot.style.background=colors[prefs[k].color][1];b.prepend(dot);b.onclick=()=>{selected=k;renderPrefs();};list.append(b);});
      const p=prefs[selected];$('#gcEditorTitle').textContent=kinds[selected];$('#gcVisible').checked=p.enabled;$('#gcBusy').checked=p.busy;$('#gcColor').value=p.color;
      const chips=$('#gcReminders');chips.replaceChildren();p.reminders.forEach(m=>{const b=el('button',reminderText(m)+' ×');b.type='button';b.setAttribute('aria-label','הסרת תזכורת '+reminderText(m));b.onclick=()=>{p.reminders=p.reminders.filter(x=>x!==m);renderPrefs();};chips.append(b);});if(!p.reminders.length)chips.append(el('span','ללא תזכורות','hint'));
      $('#gcPreviewEvent').style.borderInlineStartColor=colors[p.color][1];$('#gcPreviewEvent').style.opacity=p.enabled?'1':'.4';$('#gcPreviewStatus').textContent=kinds[selected]+(p.enabled?'':' · לא מוצג ביומן');
      const alarms=$('#gcPreviewAlarms');alarms.replaceChildren();p.reminders.forEach(m=>alarms.append(el('span','◷ '+reminderText(m))));
    }
    function renderStatus() {
      $('#gcBadge').textContent=status.enabled?'סנכרון פעיל':status.connected?'Google מחובר':status.uploaded?'הקובץ מוכן':'חיבור אופציונלי';
      $('#gcConnect').hidden=!status.uploaded;$('#gcConnectionTitle').textContent=status.connected?'היומן שלך, בדרך שלך':'כך ייראה היומן שלך';
      $('#gcConnectionInfo').textContent=status.connected?`${status.email||'חשבון Google מחובר'}${status.last_sync?' · עודכן '+new Date(status.last_sync).toLocaleTimeString('he-IL',{hour:'2-digit',minute:'2-digit'}):''}`:'בחרו צבע ותזכורות לכל מצב. ניתן לשנות הכול גם בהמשך.';
      $('#gcEnable').hidden=!status.connected||status.enabled||status.creation_pending;$('#gcEnable').textContent=status.calendar_id?'הפעלת הסנכרון':'יצירת יומן והפעלת הסנכרון';
      $('#gcSync').hidden=!status.enabled;$('#gcPause').hidden=!status.enabled;$('#gcDisconnect').hidden=!status.connected;$('#gcRecovery').hidden=!status.creation_pending;
      if(status.error)message(status.error,true);
      if(status.uploaded)$('#gcFileInfo').textContent='✓ קובץ החיבור שמור בשרת';
    }
    async function load() {await action(async()=>{status=await api('/api/calendar/google/status');prefs=structuredClone(status.preferences);guide();renderPrefs();renderStatus();if(!status.available){message('יש להתחבר ל־Arbox ולבחור סטודיו לפני חיבור היומן',true);return;}show(status.connected?3:status.uploaded?2:step);});}
    async function receive(file){await action(async()=>{uploaded=null;$('#gcUpload').disabled=true;if(!file)return;if(file.size>32768)throw Error('הקובץ גדול מדי. בחרו את קובץ ה־JSON שהורד מ־Google.');let d;try{d=JSON.parse(await file.text());}catch(_){throw Error('הקובץ אינו JSON תקין');}const hostname=new URL(status?.suggested_redirect||window.location.href).hostname;const options=readUpload(d,hostname);uploaded=d;const select=$('#gcRedirect');select.replaceChildren();options.forEach(uri=>{const o=el('option',uri);o.value=uri;select.append(o);});$('#gcRedirectLabel').hidden=options.length===1;$('#gcFileInfo').textContent=`${file.name} · ${d.web.project_id||'פרויקט Google'} · הקובץ תקין`;$('#gcUpload').disabled=false;});}
    host.querySelectorAll('[data-step]').forEach(b=>b.onclick=()=>show(Number(b.dataset.step)));
    $('#gcProject').oninput=guide;$('#gcPrepared').onclick=()=>show(2);
    $('#gcFile').onchange=e=>receive(e.target.files[0]);const drop=$('.gc-upload');drop.ondragover=e=>{e.preventDefault();drop.classList.add('dragover');};drop.ondragleave=()=>drop.classList.remove('dragover');drop.ondrop=e=>{e.preventDefault();drop.classList.remove('dragover');receive(e.dataTransfer.files[0]);};
    $('#gcUpload').onclick=()=>action(async()=>{status=await api('/api/calendar/google/credentials',{method:'POST',body:JSON.stringify({credentials:uploaded,redirect_uri:$('#gcRedirect').value})});uploaded=null;$('#gcFile').value='';$('#gcUpload').disabled=true;renderStatus();message('הקובץ נשמר. עכשיו אפשר להתחבר ל־Google.');});
    $('#gcConnect').onclick=()=>action(async()=>{const d=await api('/api/calendar/google/connect',{method:'POST'});window.location.assign(d.url);});
    Object.entries(colors).forEach(([id,[name]])=>{const o=el('option',name);o.value=id;$('#gcColor').append(o);});
    $('#gcVisible').onchange=e=>{prefs[selected].enabled=e.target.checked;renderPrefs();};$('#gcBusy').onchange=e=>{prefs[selected].busy=e.target.checked;renderPrefs();};$('#gcColor').onchange=e=>{prefs[selected].color=e.target.value;renderPrefs();};
    $('#gcAddReminder').onclick=()=>{const n=Number($('#gcMinutes').value),p=prefs[selected];if($('#gcMinutes').value===''||!Number.isInteger(n)||n<0||n>40320)return message('בחרו מספר דקות בין 0 ל־40320',true);if(p.reminders.length>=5)return message('אפשר עד חמש תזכורות',true);p.reminders=[...new Set([...p.reminders,n])].sort((a,b)=>b-a);message('');renderPrefs();};
    async function save(){status=await api('/api/calendar/google/preferences',{method:'POST',body:JSON.stringify(prefs)});renderStatus();}
    $('#gcSave').onclick=()=>action(async()=>{await save();message('ההעדפות נשמרו. אירועים עתידיים יתעדכנו בסנכרון הבא.');});
    $('#gcEnable').onclick=()=>action(async()=>{await save();status=await api('/api/calendar/google/enable',{method:'POST'});renderStatus();message(status.error||'הסנכרון פעיל. אפשר לפתוח את Google Calendar ולראות את האימונים.',!!status.error);});
    ['Sync','Pause','Disconnect'].forEach(name=>{$('#gc'+name).onclick=()=>action(async()=>{status=await api('/api/calendar/google/'+name.toLowerCase(),{method:'POST'});renderStatus();message(status.error||(name==='Disconnect'?'החיבור נותק. האירועים הקיימים נשארו ביומן.':name==='Pause'?'הסנכרון הושהה. האירועים הקיימים נשארו ביומן.':'הסנכרון הושלם'),!!status.error);});});
    $('#gcRecover').onclick=()=>action(async()=>{status=await api('/api/calendar/google/recover',{method:'POST',body:JSON.stringify({calendar_id:$('#gcRecoverId').value.trim()})});renderStatus();message('היומן חובר');});
    refresh=load;load();
  }
  root.GoogleCalendarUI={mount,refresh:()=>refresh?.(),projectId,readUpload,reminderText};
  if(typeof module!=='undefined')module.exports={projectId,readUpload,reminderText};
})(typeof window!=='undefined'?window:globalThis);
