const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), vm = require('node:vm');
const cancellation = require('../frontend/cancel-confirmation.js');
const source = fs.readFileSync(require.resolve('../frontend/app.js'), 'utf8');
const action = source.slice(source.indexOf('async function doAction('), source.indexOf('\nfunction navSchedule('));
const session = {schedule_id:77, category_name:'HS', date:'2026-11-12', start_time:'18:30',
  user_booked:123, user_in_standby:null, booking_option:'cancelScheduleUser'};
function harness({late = true, accept = true, fail = false} = {}) {
  const calls = [], confirmations = [], messages = [], button = {disabled:false};
  const context = {ArboxCancellation:cancellation, Map, String, JSON, Error,
    state:{apiKey:'key', selectedStudioId:73, membershipOverrides:new Map(), view:'mine'},
    chooseReason:async()=>({reason_code:'illness'}), confirm:message=>{confirmations.push(message);return accept;},
    toast:message=>messages.push(message), loadMine(){}, loadSchedule(){},
    $:()=>({open:false}), historyData:null,
    api:async(path, options)=>{
      calls.push({path,options});
      if (path.endsWith('/cancellation')) return {studio_id:73,schedule_id:77,booking_id:123,late_cancel:late,standby:false};
      if (fail) throw Error('late_cancel_required: conditions changed');
      return {ok:true};
    }};
  vm.createContext(context); vm.runInContext(action,context);
  return {context,calls,confirmations,messages,button,run:()=>context.doAction('cancel',session,button)};
}
for (const late of [false,true]) test(`one confirmation includes current fee state (${late}) before one cancellation`,async()=>{
  const h=harness({late}); await h.run();
  assert.equal(h.confirmations.length,1);assert.equal(h.calls.length,2);
  assert.equal(h.calls[0].path,'/api/schedule/77/cancellation');
  assert.equal(h.calls[1].path,'/api/cancel');
  assert.deepEqual(JSON.parse(h.calls[1].options.body),{schedule_id:77,late_cancel:late,booking_id:123,reason_code:'illness'});
  assert.equal(h.calls[1].options.headers['X-Arbox-Studio-Id'],'73');
  assert.match(h.confirmations[0],late ? /ביטול מאוחר/ : /חלון הביטול החופשי/);
});
test('declining confirmation never submits; changed conditions never auto-retry or add a popup',async()=>{
  const no=harness({accept:false});await no.run();assert.equal(no.calls.length,1);assert.equal(no.button.disabled,false);
  const changed=harness({fail:true});await changed.run();assert.equal(changed.calls.length,2);assert.equal(changed.confirmations.length,1);
  assert.match(changed.messages[0],/תנאי הביטול השתנו/);
});
test('late preview after a studio switch is ignored',async()=>{
  const h=harness();const api=h.context.api;
  h.context.api=async(...args)=>{const response=await api(...args);h.context.state.selectedStudioId=74;return response;};
  await h.run();assert.equal(h.confirmations.length,0);assert.equal(h.calls.length,1);
});
test('a preview for another booking cannot authorize this cancellation',()=>{
  assert.throws(()=>cancellation.validate({studio_id:73,schedule_id:77,booking_id:456,late_cancel:true},session,73),/ההרשמה השתנתה/);
});
test('shared web and HA confirmation copy is identical',()=>{
  assert.equal(fs.readFileSync(require.resolve('../frontend/cancel-confirmation.js'),'utf8'),
    fs.readFileSync(require.resolve('../../custom_components/arbox/frontend/cancel-confirmation.js'),'utf8'));
});
