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
