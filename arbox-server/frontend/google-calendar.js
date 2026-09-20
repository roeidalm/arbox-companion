/* Optional Google connection. Secrets are uploaded once and never rendered. */
(function (root) {
  'use strict';
  const kinds = {scheduled:'מתוזמן', automation:'אוטומציה', booked:'רשום', standby:'רשימת המתנה', review:'דורש בדיקה'};
  const colors = {
    1:['לבנדר','#7986cb'],2:['מרווה','#33b679'],3:['סגול','#8e24aa'],4:['ורוד','#e67c73'],
    5:['צהוב','#f6bf26'],6:['כתום','#f4511e'],7:['טורקיז','#039be5'],8:['אפור','#616161'],
    9:['כחול','#3f51b5'],10:['ירוק','#0b8043'],11:['אדום','#d50000']
  };
  // Google's current event palette; custom calendar labels keep their own names.
  const paletteNames = {
    '#ad1457':'סלק', '#d81b60':'פריחת דובדבן', '#e67c73':'פלמינגו', '#d50000':'עגבנייה',
    '#f4511e':'מנדרינה', '#ef6c00':'דלעת', '#f09300':'מנגו', '#f6bf26':'בננה',
    '#e4c441':'אתרוג', '#c0ca33':'אבוקדו', '#7cb342':'פיסטוק', '#0b8043':'בזיליקום',
    '#33b679':'מרווה', '#009688':'אקליפטוס', '#039be5':'טווס', '#4285f4':'קובלט',
    '#7986cb':'לבנדר', '#3f51b5':'אוכמניות', '#b39ddb':'ויסטריה', '#9e69af':'אחלמה',
    '#8e24aa':'ענבים', '#795548':'קקאו', '#616161':'גרפיט', '#a79b8e':'ליבנה'
  };
  function paletteOptions(palette) {
    if (!palette?.length) return Object.entries(colors).map(([id,[name,color]])=>({id,name,color}));
    const order=Object.keys(paletteNames);
    return palette.map(c=>({...c,name:c.name||paletteNames[c.color]||c.color})).sort((a,b)=>{
      const rank=c=>order.includes(c.color)?order.indexOf(c.color):order.length;
      return rank(a)-rank(b);
    });
  }
  function projectId(value) {
    let id = String(value || '').trim();
    try { if (id.startsWith('https://')) id = new URL(id).searchParams.get('project') || ''; } catch (_) { return ''; }
    return /^[a-z][a-z0-9-]{4,28}[a-z0-9]$/.test(id) ? id : '';
  }
  function setupGuide(project, callback) {
    const id=projectId(project), q=id?'?project='+encodeURIComponent(id):'';
    let validCallback=false;
    try { const u=new URL(callback);validCallback=u.protocol==='https:'&&u.pathname==='/api/calendar/google/callback'&&!u.search&&!u.hash&&!u.username&&!u.password; } catch (_) {}
    return [
      {key:'project',title:'הפרויקט האישי שלכם',url:'https://console.cloud.google.com/projectcreate',text:'צרו פרויקט בשם Arbox Calendar. אם כבר יש לכם פרויקט לחיבור הזה, השתמשו בו. הדביקו בשדה למעלה את הקישור מלוח הבקרה או את Project ID.',copy:'Arbox Calendar',copyLabel:'שם הפרויקט'},
      {key:'api',title:'הפעלת היומן',url:'https://console.cloud.google.com/apis/library/calendar-json.googleapis.com'+q,text:'לחצו Enable. אם מופיע Manage, השירות כבר פעיל ואין צורך בשינוי.'},
      {key:'branding',title:'פרטי החיבור האישי',url:'https://console.cloud.google.com/auth/branding'+q,text:'לחצו Get started אם זו ההגדרה הראשונה. שם האפליקציה: Arbox Calendar. בחרו את המייל שלכם לתמיכה ולפרטי קשר ואת הקהל External לחשבון Gmail אישי. השלימו ושמרו. כל משתמש מגדיר פרויקט בחשבון שלו.'},
      {key:'scope',title:'הרשאה ליומן הייעודי',url:'https://console.cloud.google.com/auth/scopes'+q,text:'לחצו Add or remove scopes, הוסיפו את ההרשאה הבאה ושמרו. היא מאפשרת לנהל יומנים שהאפליקציה יוצרת, בלי גישה ליומנים האחרים שלכם.',copy:'https://www.googleapis.com/auth/calendar.app.created',copyLabel:'הרשאת היומן'},
      {key:'client',title:'קובץ החיבור לשרת שלכם',url:'https://console.cloud.google.com/auth/clients'+q,text:'Create client → Web application. שם: Arbox Calendar Connection. השאירו JavaScript origins ריק. תחת Authorized redirect URIs הדביקו את הכתובת הבאה, צרו את החיבור והורידו את קובץ ה־JSON.',copy:validCallback?callback:'',copyLabel:'כתובת החזרה',blocked:!validCallback},
      {key:'production',title:'חיבור קבוע',url:'https://console.cloud.google.com/auth/audience'+q,text:'ב־Audience לחצו Publish app. ודאו שכתוב In production. כך מוסרת מגבלת 7 הימים של Testing. אם כבר התחברתם במצב Testing, חברו מחדש אחרי המעבר; אותו קובץ ואותו יומן נשארים.'}
    ].map((item,i)=>({...item,blocked:!!item.blocked||(i>0&&!id)}));
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
    if (minutes === 0) return 'בזמן האימון';
    if (minutes % 1440 === 0) return minutes === 1440 ? 'יום לפני' : `${minutes / 1440} ימים לפני`;
    if (minutes % 60 === 0) return minutes === 60 ? 'שעה לפני' : `${minutes / 60} שעות לפני`;
    return `${minutes} דקות לפני`;
  }
  function reminderMinutes(amount, unit) {
    const factor = {minutes:1,hours:60,days:1440}[unit], n = Number(amount);
    if (String(amount).trim() === '' || !factor || !Number.isInteger(n) || n < 0 || n * factor > 40320) return null;
    return n * factor;
  }
  async function saveAndSync(api, preferences) {
    await api('/api/calendar/google/preferences',{method:'POST',body:JSON.stringify(preferences)});
    return await api('/api/calendar/google/sync',{method:'POST'});
  }
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent=text; if(cls)n.className=cls; return n; };
  let refresh;
  function mount(host, api) {
    let savedPrefs = '';
    let status = null, prefs = null, palette = [], selected = 'booked', step = 1, busy = false, uploaded = null;
    host.innerHTML = `<details class="gc-disclosure" id="gcDisclosure"><summary class="gc-heading"><span><strong>סנכרון עם Google Calendar</strong><span class="gc-subtitle">צבעים ותזכורות לכל מצב · חיבור אופציונלי</span></span><span class="gc-badge" id="gcBadge">הגדרת חיבור</span></summary><div class="gc-content">
      <ol class="gc-steps" aria-label="שלבי חיבור"><li><button type="button" data-step="1">1 · הכנה</button></li><li><button type="button" data-step="2">2 · קובץ</button></li><li><button type="button" data-step="3">3 · העדפות</button></li></ol>
      <p id="gcMessage" role="status" aria-live="polite" hidden></p>
      <section data-gc-step="1"><h3>נכין את החיבור ל־Google</h3><p>הגדרה חד־פעמית בחשבון Google שלכם, עבור השרת שלכם. הקובץ וההרשאות נשמרים אצלכם. נדריך אתכם שלב־שלב; את האישור בחשבון Google מבצעים בעצמכם.</p>
      <label for="gcProject">קישור לפרויקט Google או Project ID</label><input id="gcProject" dir="ltr" placeholder="הדביקו קישור מהדפדפן של Google Cloud">
      <p id="gcProjectHint" class="hint" role="status"></p><p id="gcGuideProgress" class="hint"></p><div id="gcGuide"></div><button type="button" class="primary" id="gcPrepared">כבר יש לי קובץ JSON — להעלאה</button></section>
      <section data-gc-step="2" hidden><h3>מעלים את הקובץ ש־Google נתן לך</h3><p>אין צורך לפתוח אותו או להעתיק מתוכו פרטים. הקובץ נשמר בשרת שלך בלבד. <a href="/static/google-privacy.html" target="_blank" rel="noopener noreferrer">מידע על פרטיות החיבור</a></p>
      <label class="gc-upload" for="gcFile"><strong>בחירת קובץ JSON</strong><span>אפשר גם לגרור את הקובץ לכאן</span><input id="gcFile" type="file" accept=".json,application/json"></label>
      <p id="gcFileInfo" role="status"></p><label id="gcRedirectLabel" hidden>כתובת החזרה מתוך הקובץ<select id="gcRedirect" dir="ltr"></select></label>
      <div class="gc-actions"><button type="button" class="primary" id="gcUpload" disabled>שמירת הקובץ</button><button type="button" id="gcConnect" hidden>חיבור ל־Google</button></div>
      <p><a id="gcContinueLink" hidden rel="noreferrer">פתיחת Google ידנית</a></p><p class="hint">לאחר השמירה נפתח את Google לבחירת חשבון ולאישור גישה ליומן הייעודי.</p></section>
      <section data-gc-step="3" hidden><div class="gc-connected"><div><h3 id="gcConnectionTitle">צבעים ותזכורות</h3><p id="gcConnectionInfo">בחרו צבע ותזכורות לכל מצב. ניתן לשנות הכול גם בהמשך.</p></div></div>
      <div class="gc-personalize"><div><div id="gcKindList" class="gc-kind-list" role="group" aria-label="מצבי האימון"></div>
      <div class="gc-editor"><h4 id="gcEditorTitle"></h4><label class="gc-check"><input type="checkbox" id="gcVisible">הצגת המצב הזה ביומן</label>
      <fieldset class="gc-colors"><legend>צבע האירוע <span id="gcColorName"></span></legend><div id="gcColor" class="gc-color-grid"></div></fieldset>
      <label class="gc-check"><input type="checkbox" id="gcBusy">סימון הזמן כ״עסוק״</label>
      <h4>תזכורות לפני האימון</h4><div class="gc-reminders" id="gcReminders"></div>
      <div class="gc-reminder-add"><label for="gcMinutes">כמה זמן לפני?</label><div class="gc-reminder-fields"><input type="number" id="gcMinutes" min="0" max="40320" step="1" value="30" aria-label="מספר יחידות זמן"><select id="gcReminderUnit" aria-label="יחידת זמן"><option value="minutes">דקות</option><option value="hours">שעות</option><option value="days">ימים</option></select></div><button type="button" id="gcAddReminder">הוסף תזכורת</button></div><p class="hint">עד 5 תזכורות לכל אירוע. ללא תזכורות? הסירו את כולן.</p></div></div>
      <details class="gc-preview"><summary>תצוגה מקדימה · דוגמה</summary><div class="gc-day"><strong>יום רביעי</strong><span>האימונים שלי</span></div><div class="gc-timegrid"><span>08:00</span><article id="gcPreviewEvent"><strong>Movement basics</strong><span>08:00–09:00 · רוני גוזלי</span><b id="gcPreviewStatus"></b><div id="gcPreviewAlarms"></div></article><span>09:00</span></div><p class="hint">כשמצב האימון משתנה, אותו אירוע מתעדכן ביומן.</p></details></div>
      <p id="gcUnsaved" class="hint" hidden>יש שינויים שטרם נשמרו</p><div class="gc-actions"><button type="button" class="primary" id="gcSave">שמירת העדפות</button><button type="button" class="primary" id="gcEnable" hidden>יצירת יומן והפעלת הסנכרון</button><button type="button" id="gcSync" hidden>שמירה וסנכרון עכשיו</button><button type="button" id="gcPause" hidden>השהיית הסנכרון</button><button type="button" id="gcDisconnect" hidden>ניתוק Google</button></div><div id="gcFeedback"></div>
      <details class="gc-explanation"><summary>איך הסנכרון עובד?</summary><p class="hint">הסנכרון מציג את 30 הימים הקרובים ומתעדכן בכל דקה. ביטול או דילוג מסירים אירוע עתידי. השהיה וניתוק משאירים את האירועים שכבר נוצרו. שינויים בהרשמות עושים ב־Arbox Companion. עריכות ידניות באירועים המנוהלים ב־Google נדרסות בבדיקה תקופתית.</p></details>
      <details class="gc-explanation"><summary>החיבור מתנתק בכל שבוע?</summary><p class="hint">בפרויקט External במצב Testing הרשאת היומן פגה אחרי 7 ימים. לשימוש קבוע עברו ב־Google ל־Production, ואז לחצו על אימות החיבור מחדש. אין צורך ביומן חדש או בקובץ JSON חדש.</p><a id="gcAudienceLink" target="_blank" rel="noopener noreferrer">פתיחת הגדרות הקהל ב־Google</a><p class="hint">Production אינו הגבלת גישה אישית. הגבילו את הכניסה לאתר לחשבונות המורשים בלבד. אין לאשף גישה לבדוק את מצב הפרסום ב־Google.</p></details>
      <details class="gc-explanation"><summary>כתובת החזרה של Google</summary><p id="gcCallback" class="hint" dir="ltr"></p><div id="gcMigration" hidden><p class="hint">לאחר שהוספתם את הכתובת החדשה ל־Authorized redirect URIs ב־Google, ניתן לעדכן כאן. היומן והאירועים הקיימים נשמרים.</p><p id="gcNewCallback" dir="ltr"></p><button id="gcMigrate" type="button">הכתובת נוספה בגוגל — עדכון החיבור</button></div><button id="gcReconnect" type="button">אימות החיבור מחדש עם Google</button></details>
      <div id="gcRecovery" hidden><p>אם היומן כבר נוצר ב־Google, אפשר לחבר אותו בלי ליצור עותק נוסף. בהגדרות היומן ב־Google, תחת ״שילוב היומן״, העתיקו את מזהה היומן.</p><input id="gcRecoverId" aria-label="מזהה היומן שנוצר" dir="ltr"><button id="gcRecover" type="button">חיבור ליומן שנוצר</button></div></section></div></details>`;
    const $ = s => host.querySelector(s);
    function message(text, error=false) { const n=$('#gcMessage');n.hidden=!text;n.textContent=text;n.className=error?'gc-error':'gc-success'; }
    function show(n) { step=n;if(n===3)$('#gcFeedback').append($('#gcMessage'));else $('.gc-steps').after($('#gcMessage'));host.querySelectorAll('[data-gc-step]').forEach(x=>x.hidden=Number(x.dataset.gcStep)!==n);host.querySelectorAll('[data-step]').forEach(b=>{b.classList.toggle('active',Number(b.dataset.step)===n);b.setAttribute('aria-current',Number(b.dataset.step)===n?'step':'false');}); }
    async function action(fn, progress='') { if(busy)return;busy=true;host.setAttribute('aria-busy','true');message(progress);try{await fn();}catch(e){message(e.message,true);}finally{busy=false;host.removeAttribute('aria-busy');} }
    let guideIndex=0, guideContext='';
    function remembered(key, value) {
      try { if(value!==undefined){localStorage.setItem(key,value);return value;}return localStorage.getItem(key)||''; } catch(_){return '';}
    }
    function guide() {
      const raw=$('#gcProject').value.trim(), id=projectId(raw);
      const suggested=status?.suggested_redirect||status?.redirect_uri||'';
      const items=setupGuide(id,suggested), context=id+'|'+suggested;
      if(context!==guideContext){guideContext=context;const saved=Number(remembered('arbox-google-guide:'+context));guideIndex=Number.isInteger(saved)&&saved>=0&&saved<items.length?saved:0;}
      if(id)remembered('arbox-google-project',id);
      $('#gcProjectHint').textContent=id?'הפרויקט בקישורים: '+id:raw?'לא זוהה Project ID תקין. העתיקו את הקישור מלוח הבקרה של הפרויקט.':'אחרי יצירת הפרויקט, הדביקו כאן את הקישור כדי שכל הכפתורים יפתחו את הפרויקט שלכם.';
      $('#gcProject').setAttribute('aria-invalid',String(!!raw&&!id));
      $('#gcGuideProgress').textContent=`שלב ${guideIndex+1} מתוך ${items.length} · ההתקדמות נשמרת בדפדפן, לפי הסימון שלכם`;
      const out=$('#gcGuide');out.replaceChildren();
      items.forEach((item,i)=>{
        const d=el('details');d.open=i===guideIndex;
        const summary=el('summary',`${i+1}. ${item.title}`);d.append(summary,el('p',item.text));
        summary.onclick=e=>{e.preventDefault();guideIndex=i;remembered('arbox-google-guide:'+context,String(i));guide();out.querySelectorAll('summary')[i].focus();};
        if(item.copy){const code=el('code',item.copy);code.dir='ltr';d.append(code);const copy=el('button','העתקת '+item.copyLabel);copy.type='button';copy.onclick=()=>action(async()=>{try{await navigator.clipboard.writeText(item.copy);message(item.copyLabel+' הועתקה');}catch(_){message('סמנו והעתיקו את הטקסט המוצג. הדפדפן לא אפשר העתקה אוטומטית.');}});d.append(copy);}
        if(item.blocked)d.append(el('p',!id&&i>0?'הדביקו למעלה את קישור הפרויקט כדי להמשיך.':'נדרשת כתובת HTTPS לשרת. הגדירו אותה בהגדרות → מתקדם. כתובת HTTP פנימית אינה מתאימה לחזרה מ־Google.','gc-error'));
        if(item.key==='production'){
          const help=el('details',undefined,'gc-setup-help');help.append(el('summary','Publish app חסום, או רוצים לבדוק קודם?'));
          help.append(el('p','אם Google מפנה ל־Branding, בדקו שם אפליקציה, מייל תמיכה, כתובת בית וקישור פרטיות. בהתקנה רגילה אפשר להשתמש בדף המידע שלהלן, אחרי שבדקתם שהוא מתאר נכון את ההתקנה שלכם. אין צורך להגיש בקשת אימות רק כדי לנסות להסיר את מגבלת השבוע.'));
          try {const privacy=new URL('/static/google-privacy.html',suggested).href;const link=el('a','פתיחת דף הפרטיות של ההתקנה');link.href=privacy;link.target='_blank';link.rel='noopener noreferrer';help.append(link);const value=el('code',privacy);value.dir='ltr';help.append(value);}catch(_){}
          const branding=el('a','פתיחת Branding בפרויקט שלכם');branding.href='https://console.cloud.google.com/auth/branding'+(id?'?project='+encodeURIComponent(id):'');branding.target='_blank';branding.rel='noopener noreferrer';help.append(branding);
          help.append(el('p','אפשר להתחיל ניסיון: ב־Audience → Test users הוסיפו את חשבון היומן, ואז המשיכו להעלאת הקובץ. במצב Testing החיבור פג אחרי 7 ימים; האירועים נשארים, אבל נדרש חיבור מחדש כדי להמשיך לסנכרן.'));
          help.append(el('p','Production אינו הופך את היומן לציבורי, אך מבטל את רשימת משתמשי הבדיקה. הגבילו את הגישה לשרת שלכם. שימוש אישי עשוי להיות פטור מאימות; פעלו לפי הדרישות שמוצגות בפרויקט.'));
          const ref=el('a','הנחיות Google לשימוש אישי');ref.href='https://support.google.com/cloud/answer/13464323?hl=en';ref.target='_blank';ref.rel='noopener noreferrer';help.append(ref);d.append(help);
        }
        const actions=el('div',undefined,'gc-actions');
        if(!item.blocked){const a=el('a','פתיחת השלב ב־Google');a.href=item.url;a.target='_blank';a.rel='noopener noreferrer';actions.append(a);}
        const next=el('button',i===items.length-1?'להעלאת קובץ החיבור':'השלמתי — הבא');next.type='button';next.disabled=item.blocked||(i===0&&!id);
        next.onclick=()=>{if(i===items.length-1){show(2);return;}guideIndex=i+1;remembered('arbox-google-guide:'+context,String(guideIndex));guide();out.querySelectorAll(':scope > details > summary')[guideIndex].focus();};actions.append(next);d.append(actions);out.append(d);
      });
    }
    function currentColor(id) { return palette.find(c=>c.id===id)||{name:colors[id]?.[0]||'צבע שהוסר',color:colors[id]?.[1]||'#616161'}; }
    function renderPrefs() {
      if(!prefs)return;
      $('#gcUnsaved').hidden=JSON.stringify(prefs)===savedPrefs;
      const list=$('#gcKindList');list.replaceChildren();
      Object.entries(kinds).forEach(([k,label])=>{const b=el('button',label);b.type='button';b.className=k===selected?'active':'';b.setAttribute('aria-pressed',String(k===selected));const dot=el('span');dot.className='gc-dot';dot.style.background=currentColor(prefs[k].color).color;b.prepend(dot);b.onclick=()=>{selected=k;renderPrefs();};list.append(b);});
      const p=prefs[selected];$('#gcEditorTitle').textContent=kinds[selected];$('#gcVisible').checked=p.enabled;$('#gcBusy').checked=p.busy;$('#gcColorName').textContent='· '+currentColor(p.color).name;
      $('#gcColor').querySelectorAll('input').forEach(input=>{input.checked=input.value===p.color;});
      const chips=$('#gcReminders');chips.replaceChildren();p.reminders.forEach(m=>{const b=el('button',reminderText(m)+' ×');b.type='button';b.setAttribute('aria-label','הסרת תזכורת '+reminderText(m));b.onclick=()=>{p.reminders=p.reminders.filter(x=>x!==m);renderPrefs();};chips.append(b);});if(!p.reminders.length)chips.append(el('span','ללא תזכורות','hint'));
      $('#gcPreviewEvent').style.borderInlineStartColor=currentColor(p.color).color;$('#gcPreviewEvent').style.opacity=p.enabled?'1':'.4';$('#gcPreviewStatus').textContent=kinds[selected]+(p.enabled?'':' · לא מוצג ביומן');
      const alarms=$('#gcPreviewAlarms');alarms.replaceChildren();p.reminders.forEach(m=>alarms.append(el('span','◷ '+reminderText(m))));
    }
    function renderStatus() {
      if(status.connected&&!status.enabled||status.error)$('#gcDisclosure').open=true;
      $('#gcCallback').textContent=status.redirect_uri||'';
      $('#gcAudienceLink').href='https://console.cloud.google.com/auth/audience'+(projectId(status.project_id)?'?project='+encodeURIComponent(status.project_id):'');
      $('#gcNewCallback').textContent=status.suggested_redirect||'';
      $('#gcMigration').hidden=!status.uploaded||!status.suggested_redirect||status.suggested_redirect===status.redirect_uri;
      $('#gcReconnect').hidden=!status.uploaded;
      $('#gcBadge').textContent=status.enabled?'סנכרון פעיל':status.connected?'Google מחובר':status.uploaded?'הקובץ מוכן':'הגדרת חיבור';
      $('#gcConnect').hidden=!status.uploaded;$('#gcConnectionTitle').textContent='צבעים ותזכורות';
      $('#gcConnectionInfo').textContent=status.connected?`${status.email||'חשבון Google מחובר'}${status.last_sync?' · עודכן '+new Date(status.last_sync).toLocaleTimeString('he-IL',{hour:'2-digit',minute:'2-digit'}):''}`:'בחרו צבע ותזכורות לכל מצב. ניתן לשנות הכול גם בהמשך.';
      $('#gcEnable').hidden=!status.connected||status.enabled||status.creation_pending;$('#gcEnable').textContent=status.calendar_id?'הפעלת הסנכרון':'יצירת יומן והפעלת הסנכרון';
      $('#gcSync').hidden=!status.enabled;$('#gcPause').hidden=!status.enabled;$('#gcDisconnect').hidden=!status.connected;$('#gcRecovery').hidden=!status.creation_pending;
      if(status.error)message(status.error,true);
      if(status.uploaded)$('#gcFileInfo').textContent='✓ קובץ החיבור שמור בשרת';
    }
    async function load() {await action(async()=>{status=await api('/api/calendar/google/status');prefs=structuredClone(status.preferences);palette=paletteOptions(status.palette);
      Object.values(prefs).forEach(p=>{const match=palette.find(c=>c.color===colors[p.color]?.[1]);if(match)p.color=match.id;});
      savedPrefs=JSON.stringify(prefs);
      const grid=$('#gcColor');grid.replaceChildren();palette.forEach(c=>{
        const label=el('label',undefined,'gc-swatch');label.title=c.name;
        const input=el('input');input.type='radio';input.name='gcEventColor';input.value=c.id;input.setAttribute('aria-label',c.name);
        const dot=el('span',undefined,'gc-color-dot');dot.style.background=c.color;dot.setAttribute('aria-hidden','true');
        label.append(input,dot);grid.append(label);
      });if(!$('#gcProject').value)$('#gcProject').value=projectId(status.project_id)||projectId(remembered('arbox-google-project'));guide();renderPrefs();renderStatus();if(!status.available){message('יש להתחבר ל־Arbox ולבחור סטודיו לפני חיבור היומן',true);return;}show(status.connected?3:status.uploaded?2:step);});}
    async function receive(file){await action(async()=>{uploaded=null;$('#gcUpload').disabled=true;if(!file)return;if(file.size>32768)throw Error('הקובץ גדול מדי. בחרו את קובץ ה־JSON שהורד מ־Google.');let d;try{d=JSON.parse(await file.text());}catch(_){throw Error('הקובץ אינו JSON תקין');}const hostname=new URL(status?.suggested_redirect||window.location.href).hostname;const options=readUpload(d,hostname);uploaded=d;if(projectId(d.web.project_id)){$('#gcProject').value=projectId(d.web.project_id);guide();}const select=$('#gcRedirect');select.replaceChildren();options.forEach(uri=>{const o=el('option',uri);o.value=uri;select.append(o);});$('#gcRedirectLabel').hidden=options.length===1;$('#gcFileInfo').textContent=`${file.name} · ${d.web.project_id||'פרויקט Google'} · הקובץ תקין`;$('#gcUpload').disabled=false;});}
    host.querySelectorAll('[data-step]').forEach(b=>b.onclick=()=>show(Number(b.dataset.step)));
    $('#gcProject').oninput=guide;$('#gcPrepared').onclick=()=>show(2);
    $('#gcFile').onchange=e=>receive(e.target.files[0]);const drop=$('.gc-upload');drop.ondragover=e=>{e.preventDefault();drop.classList.add('dragover');};drop.ondragleave=()=>drop.classList.remove('dragover');drop.ondrop=e=>{e.preventDefault();drop.classList.remove('dragover');receive(e.dataTransfer.files[0]);};
    $('#gcUpload').onclick=()=>action(async()=>{status=await api('/api/calendar/google/credentials',{method:'POST',body:JSON.stringify({credentials:uploaded,redirect_uri:$('#gcRedirect').value})});uploaded=null;$('#gcFile').value='';$('#gcUpload').disabled=true;renderStatus();message('הקובץ נשמר. עכשיו אפשר להתחבר ל־Google.');});
    $('#gcConnect').onclick=()=>action(async()=>{const d=await api('/api/calendar/google/connect',{method:'POST'});const link=$('#gcContinueLink');link.href=d.url;link.hidden=false;message('מעבירים אותך ל־Google. אם הדף לא נפתח, לחצו על הקישור שמתחת לכפתורים.');window.location.assign(d.url);});
    $('#gcReconnect').onclick=()=>$('#gcConnect').onclick();
    $('#gcMigrate').onclick=()=>action(async()=>{status=await api('/api/calendar/google/redirect',{method:'POST',body:JSON.stringify({redirect_uri:status.suggested_redirect})});renderStatus();guide();message('כתובת החזרה עודכנה. היומן הקיים נשמר. אפשר לאמת את החיבור מחדש.');});
    $('#gcVisible').onchange=e=>{prefs[selected].enabled=e.target.checked;renderPrefs();};$('#gcBusy').onchange=e=>{prefs[selected].busy=e.target.checked;renderPrefs();};$('#gcColor').onchange=e=>{prefs[selected].color=e.target.value;renderPrefs();};
    $('#gcReminderUnit').onchange=()=>{const max={minutes:40320,hours:672,days:28}[$('#gcReminderUnit').value];$('#gcMinutes').max=String(max);};
    $('#gcAddReminder').onclick=()=>{const n=reminderMinutes($('#gcMinutes').value,$('#gcReminderUnit').value),p=prefs[selected];if(n===null)return message('בחרו מספר שלם ולא שלילי. אפשר להגדיר תזכורת עד 28 ימים לפני האימון.',true);if(p.reminders.includes(n))return message('התזכורת הזאת כבר נוספה');if(p.reminders.length>=5)return message('אפשר עד חמש תזכורות',true);p.reminders=[...p.reminders,n].sort((a,b)=>b-a);message('');renderPrefs();};
    async function save(){const snapshot=JSON.stringify(prefs);status=await api('/api/calendar/google/preferences',{method:'POST',body:snapshot});savedPrefs=snapshot;renderPrefs();renderStatus();}
    $('#gcSave').onclick=()=>action(async()=>{await save();message('ההעדפות נשמרו. אירועים עתידיים יתעדכנו בסנכרון הבא.');});
    $('#gcEnable').onclick=()=>action(async()=>{await save();status=await api('/api/calendar/google/enable',{method:'POST'});renderStatus();message(status.error||'הסנכרון פעיל. אפשר לפתוח את Google Calendar ולראות את האימונים.',!!status.error);});
    $('#gcSync').onclick=()=>action(async()=>{const snapshot=structuredClone(prefs);status=await saveAndSync(api,snapshot);savedPrefs=JSON.stringify(snapshot);renderPrefs();renderStatus();message(status.error||`נשמר וסונכרן בהצלחה · ${status.event_count} אירועים מנוהלים ביומן`,!!status.error);},'שומר העדפות ומסנכרן עם Google…');
    ['Pause','Disconnect'].forEach(name=>{$('#gc'+name).onclick=()=>action(async()=>{status=await api('/api/calendar/google/'+name.toLowerCase(),{method:'POST'});renderStatus();message(status.error||(name==='Disconnect'?'החיבור נותק. האירועים הקיימים נשארו ביומן.':name==='Pause'?'הסנכרון הושהה. האירועים הקיימים נשארו ביומן.':'הסנכרון הושלם'),!!status.error);});});
    $('#gcRecover').onclick=()=>action(async()=>{status=await api('/api/calendar/google/recover',{method:'POST',body:JSON.stringify({calendar_id:$('#gcRecoverId').value.trim()})});renderStatus();message('היומן חובר');});
    refresh=load;load();
  }
  root.GoogleCalendarUI={mount,refresh:()=>refresh?.(),projectId,readUpload,reminderText};
  if(typeof module!=='undefined')module.exports={projectId,readUpload,reminderText,paletteOptions,reminderMinutes,saveAndSync,setupGuide};
})(typeof window!=='undefined'?window:globalThis);
