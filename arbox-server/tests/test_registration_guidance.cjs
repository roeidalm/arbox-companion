const test=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs');const vm=require('node:vm');const path=require('node:path');
const context=vm.createContext({$:()=>({addEventListener(){}})});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../frontend/registration-guidance.js'),'utf8'),context);
test('unknown history never claims a safe waiting period',()=>{
  assert.match(context.timingHistoryText({openings:0}),/אין עדיין היסטוריה/);
  assert.match(context.timingHistoryText({openings:2,risky_openings:0,fastest_full_seconds:null}),/אינה הבטחה/);
});
test('fast-fill guidance shows measured evidence and recommendation',()=>{
  const text=context.timingHistoryText({openings:3,risky_openings:2,fastest_full_seconds:50});
  assert.match(text,/0:50/);assert.match(text,/ב־2/);assert.match(text,/מיידית/);
});
test('eligibility and cancellation statuses outrank a waiting policy',()=>{
  const entry={status:'waiting_occupancy',policy:{mode:'wait',threshold_percent:30},deadline_at:'2026-09-23T08:10:00'};
  assert.match(context.timingModeText(entry,{planning:{state:'needs_action',reason:'בחרו מנוי'}}),/מושהה.*בחרו מנוי/);
  assert.match(context.timingModeText(entry,{automation_skipped:true}),/לא תבוצע/);
  assert.match(context.timingModeText(entry,{}),/08:10/);
  assert.match(context.timingModeText({...entry,status:'booked'},{}),/כבר רשום/);
});
function renderCard(entry,options={}) {
  context.state={selectedStudioId:1};
  context.timingNode=(tag,text)=>({tag,textContent:text,children:[],append(...nodes){this.children.push(...nodes);}});
  return context.timingCard(entry,options);
}
const routine={status:'booked',policy:{mode:'immediate'},history:{openings:0}};
test('routine guidance is collapsed while intentional previews remain expanded',()=>{
  const card=renderCard(routine);
  assert.equal(card.tag,'details');assert.notEqual(card.open,true);
  assert.equal(card.children[0].tag,'summary');
  assert.equal(card.children[0].textContent,'פרטי הרשמה ולמידה');
  assert.equal(renderCard(routine,{actions:false}).tag,'div');
});
test('active waiting and relevant fast-fill warnings remain visible in summary',()=>{
  const waiting={...routine,status:'waiting_occupancy',policy:{mode:'wait',threshold_percent:30},deadline_at:'2026-09-24T10:10:00'};
  assert.match(renderCard(waiting).children[0].textContent,/10:10/);
  const risky={...routine,status:'rule',history:{openings:2,risky_openings:1}};
  assert.match(renderCard(risky).children[0].textContent,/התמלאות מהירה/);
  assert.equal(renderCard({...risky,status:'booked'}).children[0].textContent,'פרטי הרשמה ולמידה');
  assert.equal(renderCard(null).children[0].tag,'summary');
});
