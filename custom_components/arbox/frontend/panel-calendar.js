import {filterPicker, matchesFilters} from './filter-picker.js?v=1';
const iso = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
const dateOf = value => new Date(`${value}T12:00:00`);
const add = (value, days) => {const d=dateOf(value);d.setDate(d.getDate()+days);return iso(d);};
const label = value => dateOf(value).toLocaleDateString('he-IL',{weekday:'long',day:'numeric',month:'numeric'});
const el = (tag,text,cls) => {const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;};
const btn = (text,run,cls) => {const b=el('button',text,cls);b.type='button';b.onclick=()=>Promise.resolve(run()).catch(()=>{});return b;};

export function calendarRange(date, view) {
  if(view==='all')return {date_from:null,date_to:null};
  const d=dateOf(date);
  if(view==='week'){const start=add(date,-d.getDay());return {date_from:start,date_to:add(start,6)};}
  if(view==='month')return {date_from:iso(new Date(d.getFullYear(),d.getMonth(),1,12)),date_to:iso(new Date(d.getFullYear(),d.getMonth()+1,0,12))};
  return {date_from:date,date_to:date};
}
export function calendarStatus(s) {
  if(s.user_booked!=null)return {key:'booked',label:'מוזמן',icon:'✓'};
  if(s.user_in_standby!=null)return {key:'waiting',label:'בהמתנה',icon:'◷'};
  if(s.planning && s.planning.state !== 'ready')return {key:'review',label:'דורש בדיקה',icon:'⚠'};
  if(s.watched||s.planning_source==='scheduled')return {key:'scheduled',label:'מתוזמן',icon:'⌛'};
  if(s.automation_skipped)return {key:'skipped',label:'דולג הפעם',icon:'↷'};
  if(s.autobook_blocked_by_vacation)return {key:'vacation',label:'חופשה',icon:'☀'};
  if(s.autobook_match||s.planning_source==='autobook')return {key:'automatic',label:'אוטומטי',icon:'↻'};
  return null;
}
export function monthPreviewSessions(sessions,limit=3) {
  const indexed=sessions.map((session,index)=>({session,index,status:calendarStatus(session)}));
  const chosen=indexed.filter(x=>x.status).slice(0,limit);
  chosen.push(...indexed.filter(x=>!x.status).slice(0,Math.max(0,limit-chosen.length)));
  return chosen.sort((a,b)=>a.index-b.index).map(x=>x.session);
}
export function calendarSignature(panel){return JSON.stringify([panel._date,panel._view,panel._mineView,panel._selectedDay,panel._filters]);}

function counts(rows) {
  const result={};for(const s of rows){const status=calendarStatus(s);if(status)result[status.key]=(result[status.key]||0)+1;}return result;
}
const STATES=[['booked','מוזמנים','✓'],['waiting','בהמתנה','◷'],['review','דורשים בדיקה','⚠'],['scheduled','מתוזמנים','⌛'],['automatic','אוטומטיים','↻'],['skipped','דולגו','↷'],['vacation','בחופשה','☀']];
function badges(rows,compact=false){
  const n=el('div',null,'pc-counts'),c=counts(rows);
  for(const [key,text,icon]of STATES)if(c[key]){const b=el('span',`${icon} ${c[key]}${compact?'':` ${text}`}`,`pc-state pc-${key}`);b.title=`${c[key]} ${text}`;b.setAttribute('aria-label',b.title);n.append(b);}return n;
}
function list(panel,rows,target){
  if(!rows.length){target.append(el('p','אין אימונים בתצוגה הזו','pc-empty'));return;}
  panel.list(rows,target);
  for(const [i,card]of [...target.querySelectorAll('.session-card')].entries()){
    const status=calendarStatus(rows[i]);if(status)card.classList.add('pc-personal',`pc-${status.key}`);
  }
}
export function renderCalendar(panel,{mine=false}={}) {
  if(!panel.shadowRoot.querySelector('link[data-calendar-style]')){const css=el('link');css.rel='stylesheet';css.href=new URL('./panel-calendar.css?v=3.3.0',import.meta.url).href;css.dataset.calendarStyle='';panel.shadowRoot.append(css);}
  const view=(mine?panel._mineView:panel._view)||(mine?'all':'day');
  const today=panel.studioNow().slice(0,10),anchor=panel._date||today;
  const setView=v=>{if(mine)panel._mineView=v;else panel._view=v;panel._selectedDay=anchor;panel.load();};
  const navigate=date=>{panel._date=date;panel._selectedDay=date;panel.load();};
  const showDay=date=>{panel._date=date;panel._selectedDay=date;if(mine)panel._mineView='day';else panel._view='day';panel.load();};
  const range=calendarRange(anchor,view),root=el('section',null,'pc-calendar');
  root.setAttribute('aria-label',mine?'לוח האימונים שלי':'לוח האימונים');
  const controls=el('div',null,'pc-controls');
  const views=el('div',null,'pc-views');views.setAttribute('aria-label','תצוגת לוח');
  for(const [value,text]of [...(mine?[['all','הכול']]:[]),['day','יום'],['week','שבוע'],['month','חודש']]){
    const b=btn(text,()=>setView(value));b.setAttribute('aria-pressed',String(view===value));views.append(b);
  }
  controls.append(views);
  if(view!=='all'){
    const arrows=el('div',null,'pc-arrows');
    const move=direction=>{if(view==='month'){const d=dateOf(anchor);navigate(iso(new Date(d.getFullYear(),d.getMonth()+direction,1,12)));}else navigate(add(anchor,direction*(view==='week'?7:1)));};
    const previous=btn('→',()=>move(-1));previous.setAttribute('aria-label','לתקופה הקודמת');
    const next=btn('←',()=>move(1));next.setAttribute('aria-label','לתקופה הבאה');
    arrows.append(previous,btn('היום',()=>navigate(today)),next);controls.append(arrows);
    const wrap=el('label',null,'pc-date'),input=el('input');input.type='date';input.value=anchor;input.setAttribute('aria-label','בחירת תאריך');input.dataset.focusKey='calendar-date';input.onchange=()=>{if(input.value)navigate(input.value);};wrap.append(input);controls.append(wrap);
  }
  const tools = mine ? el('details',null,'pc-mine-tools') : root;
  if(mine) {
    tools.open = !!panel._mineToolsOpen;
    tools.ontoggle = () => {panel._mineToolsOpen = tools.open;};
    tools.append(el('summary', `סינון ותצוגת לוח${view !== 'all' ? ' · '+({day:'יום',week:'שבוע',month:'חודש'}[view]) : ''}`));
    root.append(tools);
  }
  tools.append(controls);
  if(view!=='all')root.append(el('h2',view==='month'?dateOf(anchor).toLocaleDateString('he-IL',{month:'long',year:'numeric'}):view==='week'?`${label(range.date_from)} – ${label(range.date_to)}`:label(anchor),'pc-title'));
  const all=panel.rows().slice().sort((a,b)=>`${a.date}${a.start_time||''}`.localeCompare(`${b.date}${b.start_time||''}`));
  const filters=el('div',null,'pc-filter-fields');
  panel._calendarFilterOpen ||= {};
  for(const [key,text] of [['category_name','סוג שיעור'],['coach_name','מאמן/ת']]) {
    filters.append(filterPicker({label:text, key, values:[...new Set(all.map(s=>s[key]).filter(Boolean))].sort(), selected:panel._filters?.[key],
      open:!!panel._calendarFilterOpen[key], toggle:open=>{panel._calendarFilterOpen[key]=open;},
      change:values=>{panel._filters={...panel._filters,[key]:values};panel.render();}}));
  }
  const parts=el('div',null,'pc-dayparts');
  for(const [value,text] of [['','כל השעות'],['morning','בוקר'],['noon','צהריים'],['afternoon','אחה״צ'],['evening','ערב']]) {
    const b=btn(text,()=>{panel._filters={...panel._filters,daypart:value};panel.render();});
    b.setAttribute('aria-pressed',String((panel._filters?.daypart||'')===value));parts.append(b);
  }
  tools.append(filters,parts);
  const rows=all.filter(s=>matchesFilters(s,panel._filters)).filter(s=>!range.date_from||(s.date>=range.date_from&&s.date<=range.date_to));
  const legend=el('div',null,'pc-legend');legend.setAttribute('aria-label','מקרא מצבי האימונים');
  for(const[key,text,icon]of STATES)legend.append(el('span',`${icon} ${text}`,`pc-state pc-${key}`));tools.append(legend);

  if(!mine) root.append(badges(rows));
  if(view==='month'){
    const selected=panel._selectedDay&&panel._selectedDay>=range.date_from&&panel._selectedDay<=range.date_to?panel._selectedDay:anchor;
    const selectDay=date=>{panel._selectedDay=date;panel.render();};
    const grid=el('div',null,'pc-month');
    for(const day of ['א׳','ב׳','ג׳','ד׳','ה׳','ו׳','ש׳'])grid.append(el('div',day,'pc-weekday'));
    for(let i=0;i<dateOf(range.date_from).getDay();i++){const pad=el('div',null,'pc-pad');pad.setAttribute('aria-hidden','true');grid.append(pad);}
    for(let day=range.date_from;day<=range.date_to;day=add(day,1)){
      const date=day,sessions=rows.filter(s=>s.date===date),cell=el('section',null,`pc-cell${date===today?' pc-today':''}${date===selected?' pc-selected':''}`);
      const dayButton=btn(String(dateOf(date).getDate()),()=>selectDay(date),'pc-day-number');
      const states=counts(sessions),description=STATES.filter(([key])=>states[key]).map(([key,text])=>`${states[key]} ${text}`).join(' · ');
      dayButton.setAttribute('aria-label',`${label(date)} · ${sessions.length} אימונים${description?' · '+description:''}`);
      dayButton.setAttribute('aria-pressed',String(date===selected));
      dayButton.append(el('span',sessions.length?String(sessions.length):'–','pc-cell-total'));
      cell.append(dayButton,badges(sessions,true));
      cell.onclick=event=>{if(!event.target.closest('button'))selectDay(date);};
      for(const s of monthPreviewSessions(sessions)){
        const status=calendarStatus(s),b=btn(`${status?.icon||''} ${(s.start_time||'').slice(0,5)} ${s.category_name||'אימון'}`,()=>panel.openSession(s),`pc-event ${status?`pc-${status.key}`:''}`);
        b.title=[s.category_name,s.coach_name,status?.label].filter(Boolean).join(' · ');cell.append(b);
      }
      if(sessions.length>3)cell.append(btn(`עוד ${sessions.length-3}`,()=>selectDay(date),'pc-more'));
      grid.append(cell);
    }
    root.append(grid);
    const detail=el('section',null,'pc-selected-day');detail.setAttribute('aria-label',`אימונים ביום ${label(selected)}`);
    detail.append(el('h2',label(selected),'pc-title'));
    list(panel,rows.filter(s=>s.date===selected),detail);root.append(detail);
  }else if(view==='week'){
    const grid=el('div',null,'pc-week');
    for(let date=range.date_from;date<=range.date_to;date=add(date,1)){
      const day=date,col=el('section',null,`pc-week-day${day===today?' pc-today':''}`),sessions=rows.filter(s=>s.date===day);
      col.append(btn(label(day),()=>showDay(day),'pc-week-title'),badges(sessions,true));list(panel,sessions,col);grid.append(col);
    }
    root.append(grid);
  }else{
    const active=rows.filter(s=>!s.automation_skipped),skipped=rows.filter(s=>s.automation_skipped);
    list(panel,active,root);
    if(skipped.length){const details=el('details',null,'pc-skipped-list');details.append(el('summary',`${skipped.length} מועדים שדילגת עליהם · אפשר להחזיר`));list(panel,skipped,details);root.append(details);}
  }
  panel._content.append(root);
}
