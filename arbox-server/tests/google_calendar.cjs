const {test} = require('node:test');
const assert = require('node:assert/strict');
const {projectId,readUpload,reminderText}=require('../frontend/google-calendar.js');
test('wizard extracts the project from the console link',()=>{
 assert.equal(projectId('https://console.cloud.google.com/home/dashboard?project=arbox-calendar-508719'),'arbox-calendar-508719');
 assert.equal(projectId('<script>'),'');
});
test('upload accepts only the local HTTPS callback',()=>{
 const uri='https://server.example:8446/api/calendar/google/callback';
 const json={web:{client_id:'test',client_secret:'test',redirect_uris:[uri,'https://evil.example/api/calendar/google/callback']}};
 assert.deepEqual(readUpload(json,'server.example'),[uri]);
 assert.throws(()=>readUpload(json,'other.example'));
 assert.throws(()=>readUpload({installed:{}},'server.example'));
});
test('multiple reminders retain individual labels',()=>{
 assert.equal(reminderText(60),'שעה לפני');assert.equal(reminderText(30),'30 דקות לפני');assert.equal(reminderText(0),'בזמן האימון');
});


test('calendar palette preserves label ids, colors and custom names', () => {
  const {paletteOptions}=require('../frontend/google-calendar.js');
  const options=paletteOptions([{id:'custom-id',color:'#009688',name:'My color'}, {id:'red-id',color:'#d50000',name:''}]);
  assert.equal(options.length,2);
  assert.equal(options[0].id,'red-id');
  assert.equal(options[1].name,'My color');
  assert.equal(options[1].color,'#009688');
  assert.equal(paletteOptions([]).length,11);
});


test('reminder units convert with bounds and readable day and hour labels',()=>{
 const {reminderMinutes}=require('../frontend/google-calendar.js');
 assert.equal(reminderMinutes('4','hours'),240);
 assert.equal(reminderMinutes('2','days'),2880);
 assert.equal(reminderMinutes('28','days'),40320);
 assert.equal(reminderMinutes('0','minutes'),0);
 for(const [amount,unit] of [['29','days'],['-1','hours'],['','minutes'],['1.5','hours'],['4','unknown']]) assert.equal(reminderMinutes(amount,unit),null);
 assert.equal(reminderText(240),'4 שעות לפני');
 assert.equal(reminderText(1440),'יום לפני');
 assert.equal(reminderText(2880),'2 ימים לפני');
});


test('sync saves the current draft first and stops if saving fails',async()=>{
 const {saveAndSync}=require('../frontend/google-calendar.js');
 const calls=[],prefs={booked:{busy:true,reminders:[240,30]}};
 const api=async(path,options)=>{calls.push([path,options]);return {event_count:9};};
 assert.deepEqual(await saveAndSync(api,prefs),{event_count:9});
 assert.deepEqual(calls.map(c=>c[0]),['/api/calendar/google/preferences','/api/calendar/google/sync']);
 assert.deepEqual(JSON.parse(calls[0][1].body),prefs);
 let attempts=0;
 await assert.rejects(saveAndSync(async()=>{attempts++;throw Error('save failed');},prefs),/save failed/);
 assert.equal(attempts,1);
});
