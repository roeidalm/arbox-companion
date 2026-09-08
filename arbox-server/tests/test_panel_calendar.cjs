const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const source=fs.readFileSync(path.resolve(__dirname,'../../custom_components/arbox/frontend/panel-calendar.js'),'utf8');
const modulePromise=import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
test('calendar ranges use Sunday weeks and exact leap-year month bounds',async()=>{
 const {calendarRange}=await modulePromise;
 assert.deepEqual(calendarRange('2028-02-29','month'),{date_from:'2028-02-01',date_to:'2028-02-29'});
 assert.deepEqual(calendarRange('2026-01-01','week'),{date_from:'2025-12-28',date_to:'2026-01-03'});
 assert.deepEqual(calendarRange('2026-09-06','week'),{date_from:'2026-09-06',date_to:'2026-09-12'});
 assert.deepEqual(calendarRange('2026-09-06','day'),{date_from:'2026-09-06',date_to:'2026-09-06'});
 assert.deepEqual(calendarRange('2026-09-06','all'),{date_from:null,date_to:null});
});
test('month prioritizes personal planning but preserves chronological order',async()=>{
 const {monthPreviewSessions}=await modulePromise;
 const rows=[{id:1},{id:2},{id:3},{id:4,watched:1},{id:5,autobook_match:true},{id:6,user_booked:44}];
 assert.deepEqual(monthPreviewSessions(rows).map(x=>x.id),[4,5,6]);
 assert.deepEqual(monthPreviewSessions(rows,5).map(x=>x.id),[1,2,4,5,6]);
 assert.deepEqual(monthPreviewSessions(rows,0),[]);
});
test('status handles null booking fields and distinguishes skipped and vacation automation',async()=>{
 const {calendarStatus}=await modulePromise;
 assert.equal(calendarStatus({user_booked:null,user_in_standby:null}),null);
 assert.equal(calendarStatus({user_booked:0}).key,'booked');
 assert.equal(calendarStatus({user_booked:null,user_in_standby:0}).key,'waiting');
 assert.equal(calendarStatus({automation_skipped:true,autobook_match:true}).key,'skipped');
 assert.equal(calendarStatus({autobook_blocked_by_vacation:true,autobook_match:true}).key,'vacation');
 assert.equal(calendarStatus({planning_source:'scheduled'}).key,'scheduled');
 assert.equal(calendarStatus({planning_source:'autobook'}).key,'automatic');
 assert.equal(calendarStatus({planning_source:'autobook',planning:{state:'no_capacity'}}).key,'review');
 assert.equal(calendarStatus({planning_source:'uncertain',planning:{state:'uncertain'}}).key,'review');
});
