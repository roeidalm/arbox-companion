/* Registration policy and observational history; no booking actions here. */
let registrationReport;
function timingNode(tag, text) { const n=document.createElement(tag); n.textContent=text; return n; }
async function loadRegistrationLearning() {
  try {
    registrationReport=await api('/api/registration-learning');
    const c=registrationReport.config;
    $('#registrationLearning').checked=c.learning_enabled;
    $('#registrationWaiting').checked=c.enabled;
    $('#registrationPercent').value=c.threshold_percent;
    $('#registrationTimeout').value=c.timeout_minutes;
    $('#registrationWarning').textContent=registrationReport.warning;
    const last=registrationReport.recent_runs[0];
    $('#registrationHealth').textContent=last ? `דגימה אחרונה: ${last.observed_at.replace('T',' ').slice(0,19)} · ${last.found}/${last.expected} שיעורים · ${last.status==='ok'?'הושלמה':last.status==='partial'?'חסרים נתונים':'נכשלה'}` : 'עדיין לא בוצעה דגימה. האיסוף מתחיל בפתיחות ההרשמה הבאות, ואינו משחזר היסטוריה.';
    const select=$('#registrationTarget'), selected=select.value; select.replaceChildren();
    const box=registrationReport.box_id;
    for(const [kind,rows] of [['rule',registrationReport.rules],['session',registrationReport.sessions]]) {
      const group=document.createElement('optgroup');group.label=kind==='rule'?'אוטומציות':'שיעורים';
      for(const row of rows) { const o=timingNode('option',row.label||row.name); o.value=`${kind}:${box}:${row.id}`;group.append(o); }
      select.append(group);
    }
    if ([...select.options].some(o=>o.value===selected)) select.value=selected;
    loadTimingOverride();
    const host=$('#registrationStats');host.replaceChildren();
    if(!registrationReport.courses.length) host.append(timingNode('p','אין היסטוריה עדיין. מומלץ להפעיל למידה למשך שבועיים ולהשאיר הרשמה מיידית.'));
    for(const course of registrationReport.courses) {
      const details=document.createElement('details');details.append(timingNode('summary',`${course.label} — ${course.openings} פתיחות נמדדו, ${course.complete_openings} בכיסוי מלא`));
      details.append(timingNode('p','דגימות מעטות אינן תחזית. פתיחות חלקיות, שינוי בקיבולת ושינויים בשיעור עשויים להשפיע.'));
      for(const w of course.observations.slice(-12).reverse()) {
        const entry=document.createElement('details');
        entry.append(timingNode('summary',`${w.opens_at.replace('T',' ')} · ${w.samples} דגימות · ${w.complete?'כיסוי מלא':'כיסוי חלקי'}`));
        const f=x=>x==null?'לא נצפה':`${Math.floor(x/60)}:${String(x%60).padStart(2,'0')} דק׳`;
        entry.append(timingNode('p',`זיהוי ראשון: 30% — ${f(w.first_observed_threshold_seconds['30'])}; 70% — ${f(w.first_observed_threshold_seconds['70'])}; מלא — ${f(w.first_observed_threshold_seconds['100'])}. הפער הגדול בין דגימות: ${w.max_gap_seconds} שניות.`));
        entry.append(timingNode('p',`ההרשמה שלנו זוהתה: ${f(w.own_booking_first_observed_seconds)}. התפוסה כוללת אותנו.`));
        for(const point of w.points) entry.append(timingNode('div',`${f(point.seconds)}: ${point.registered}/${point.capacity}${point.own_booking?' · כולל אותנו':''}`));
        details.append(entry);
      }
      host.append(details);
    }
  } catch(e) { $('#registrationStats').textContent='לא ניתן לטעון: '+e.message; }
}
function loadTimingOverride() {
  if(!registrationReport)return;
  const c=registrationReport.config;
  const o=c.overrides[$('#registrationTarget').value];
  $('#registrationMode').value=o?.mode||'inherit';
  $('#registrationOverridePercent').value=o?.threshold_percent??c.threshold_percent;
  $('#registrationOverrideTimeout').value=o?.timeout_minutes??c.timeout_minutes;
}
$('#registrationTarget').addEventListener('change',loadTimingOverride);
$('#registrationRefresh').addEventListener('click',loadRegistrationLearning);

$('#registrationSave').addEventListener('click',async()=>{
  try { await api('/api/settings',{method:'POST',body:JSON.stringify({registration_timing:{
    learning_enabled:$('#registrationLearning').checked,enabled:$('#registrationWaiting').checked,
    threshold_percent:Number($('#registrationPercent').value),timeout_minutes:Number($('#registrationTimeout').value)}})});
    toast('נשמר ✓');await loadRegistrationLearning();
  } catch(e){toast(e.message);}
});
$('#registrationOverrideSave').addEventListener('click',async()=>{
  if(!registrationReport||!$('#registrationTarget').value)return;
  const overrides={...registrationReport.config.overrides},key=$('#registrationTarget').value,mode=$('#registrationMode').value;
  if(mode==='inherit')delete overrides[key];else overrides[key]={mode,threshold_percent:Number($('#registrationOverridePercent').value),timeout_minutes:Number($('#registrationOverrideTimeout').value)};
  try {await api('/api/settings',{method:'POST',body:JSON.stringify({registration_timing:{overrides}})});toast('נשמר ✓');await loadRegistrationLearning();}catch(e){toast(e.message);}
});
