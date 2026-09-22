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
