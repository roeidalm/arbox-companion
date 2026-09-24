/* Contextual learning guidance; all text is rendered with textContent. */
let ruleTimingContext = null;
let registrationFocusCohorts = null;
const timingSeconds = seconds => `${Math.floor(seconds/60)}:${String(Math.round(seconds)%60).padStart(2,'0')}`;
async function fetchTimingContext(studio, asPin=false) {
  try {
    const data=await api(`/api/registration-guidance${asPin?'?as_pin=true':''}`,{headers:{'X-Arbox-Studio-Id':String(studio)}});
    return studio===state.selectedStudioId?data:null;
  } catch(e) { return null; }
}
function timingHistoryText(history) {
  if(!history?.openings) return 'אין עדיין היסטוריה לשיעורים האלה. מומלץ להישאר בהרשמה מיידית ולצבור כשבועיים של למידה.';
  const full=history.fastest_full_seconds==null?'טרם נצפה שיעור מלא.':`הזיהוי המוקדם ביותר של שיעור מלא: ${timingSeconds(history.fastest_full_seconds)} דקות מהפתיחה.`;
  return `${history.openings} פתיחות נמדדו. ${full} ${history.risky_openings?`⚠️ ב־${history.risky_openings} מהן נצפתה התמלאות מהירה ביחס לזמן הבדיקה — מומלצת הרשמה מיידית.`:'אין כרגע אינדיקציה להתמלאות מהירה; זו אינה הבטחה למקום.'}`;
}
function openTimingHistory(history) {
  registrationFocusCohorts=history?.cohorts||[];
  state.settingsPane='registration';showView('settings');
}
function timingModeText(entry,session) {
  if(session?.automation_skipped)return 'מדלגים על המועד הזה — לא תבוצע הרשמה אוטומטית';
  if(session?.planning && session.planning.state!=='ready') return 'התכנון מושהה: '+session.planning.reason;
  if(entry.status==='booked') return 'כבר רשום — אין המתנה להרשמה';
  if(entry.status==='standby') return 'ברשימת המתנה למקום שהתפנה';
  if(entry.status==='disabled') return 'האוטומציה כבויה';
  if(entry.status==='notify') return 'התראה בלבד — האוטומציה אינה נרשמת';
  const policy=entry.policy;
  const mode=policy.mode==='immediate'?'הרשמה מיידית עם פתיחת ההרשמה':`המתנה עד ${policy.threshold_percent}% תפוסה או ${policy.timeout_minutes} דקות מהפתיחה`;
  if(entry.status==='before_open') return `${mode} · פתיחה: ${entry.opens_at.replace('T',' ').slice(0,16)}`;
  if(entry.status==='waiting_occupancy') return `ממתין ל־${policy.threshold_percent}% תפוסה · סיום ההמתנה לכל המאוחר ב־${entry.deadline_at.slice(11,16)} · בדיקה כל 20–40 שניות`;
  if(entry.status==='ready_for_check') return 'אין כרגע המתנה לפי תפוסה — ההרשמה תיבחן בבדיקה הבאה, בכפוף לזכאות ולמקום פנוי';
  return mode;
}
function timingCard(entry,{kind,id,session,refresh,actions=true,studio=state.selectedStudioId}={}) {
  const card=timingNode(actions?'details':'div','');card.className='registration-context';
  if(actions) {
    const summary=timingNode('summary','פרטי הרשמה ולמידה');
    if(entry?.status==='waiting_occupancy')summary.textContent=timingModeText(entry,session);
    else if(entry?.history?.risky_openings && !['booked','standby','disabled','notify'].includes(entry.status)) {
      summary.textContent='⚠️ נצפתה התמלאות מהירה — מומלצת הרשמה מיידית';
      summary.className='planning-warning';
    } else if(entry?.override)summary.textContent='פרטי הרשמה ולמידה · העדפה אישית';
    card.append(summary);
  }
  if(!entry){card.append(timingNode('p','לא ניתן לטעון כרגע את המלצת ההרשמה. ההגדרות הקיימות ממשיכות לחול.'));return card;}
  card.append(timingNode('strong',timingModeText(entry,session)));
  const history=timingNode('p',timingHistoryText(entry.history));
  if(entry.history.risky_openings)history.className='planning-warning';card.append(history);
  const controls=timingNode('div','');controls.className='registration-context-actions';
  const detail=timingNode('button','לנתוני הלמידה');detail.type='button';detail.onclick=()=>openTimingHistory(entry.history);controls.append(detail);
  if(actions && !['booked','standby','notify'].includes(entry.status)) {
    for(const [mode,label] of [['immediate',kind==='rule'?'מיידי לאוטומציה הזו':'מיידי לשיעור הזה'],['inherit','חזרה לברירת המחדל']]) {
      if(mode==='inherit'&&!entry.override)continue;
      if(mode==='immediate'&&entry.override?.mode==='immediate')continue;
      const button=timingNode('button',label);button.type='button';
      button.onclick=async()=>{
        button.disabled=true;
        try {await api(`/api/registration-policy/${kind}/${id}`,{method:'PUT',headers:{'X-Arbox-Studio-Id':String(studio)},body:JSON.stringify({mode})});toast('העדפת ההרשמה נשמרה');if(studio===state.selectedStudioId)await refresh?.();}
        catch(e){toast(e.message);button.disabled=false;}
      };controls.append(button);
    }
  }
  card.append(controls);return card;
}
async function chooseTimingForSchedule(session,studio) {
  const context=await fetchTimingContext(studio,true);
  if(studio!==state.selectedStudioId)return null;
  const dialog=$('#timingScheduleDialog'),host=$('#timingScheduleContent');
  host.replaceChildren(timingCard(context?.sessions[String(session.schedule_id)],{actions:false,session}));
  const detailsButton=host.querySelector('.registration-context-actions button');
  if(detailsButton)detailsButton.onclick=()=>{dialog.close('cancel');openTimingHistory(context?.sessions[String(session.schedule_id)]?.history);};
  $('#timingScheduleTitle').textContent=`תזמון ${session.category_name||'שיעור'} · ${session.date} ${session.start_time}`;
  $('#timingScheduleMode').value='keep';dialog.returnValue='';dialog.showModal();
  return new Promise(resolve=>dialog.addEventListener('close',()=>resolve(dialog.returnValue==='confirm'?$('#timingScheduleMode').value:null),{once:true}));
}
function refreshTimingRuleDraft() {
  const host=$('#ruleTimingPreview');if(!host)return;
  const active=$('#ruleMode').value==='autobook';$('#ruleTimingFields').hidden=!active;
  if(!active)return;
  if(!ruleTimingContext||ruleTimingContext.box_id!==state.selectedStudioId){host.replaceChildren(timingCard(null));return;}
  const draft=currentRuleDraft(),context=ruleTimingContext;
  const entries=Object.values(context.sessions).filter(e=>{
    const s=e.session,wd=(new Date(s.date+'T00:00').getDay()+6)%7;
    return (!draft.coaches.length||draft.coaches.includes(s.coach_name))&&(!draft.categories.length||draft.categories.includes(s.category_name))&&(!draft.weekdays.length||draft.weekdays.includes(wd))&&(!draft.time_from||s.start_time>=draft.time_from)&&(!draft.time_to||s.start_time<=draft.time_to);
  });
  const histories=new Map(entries.filter(e=>e.history.cohorts.length).map(e=>[e.history.cohorts[0],e.history]));
  const hs=[...histories.values()],full=hs.map(h=>h.fastest_full_seconds).filter(x=>x!=null);
  const history={openings:hs.reduce((n,h)=>n+h.openings,0),risky_openings:hs.reduce((n,h)=>n+h.risky_openings,0),fastest_full_seconds:full.length?Math.min(...full):null,cohorts:[...histories.keys()]};
  const existing=context.rules[String(editingRuleId)],selection=$('#ruleTimingMode').value;
  const policy=selection==='keep'&&existing?existing.policy:{...context.config,mode:context.config.enabled&&selection!=='immediate'?'wait':'immediate'};
  host.replaceChildren(timingCard({policy,history,status:'rule'},{actions:false}));
}
$('#ruleTimingMode').addEventListener('change',refreshTimingRuleDraft);

$('#ruleTimingMode').value='inherit';
