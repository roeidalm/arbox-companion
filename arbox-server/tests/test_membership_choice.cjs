const test = require('node:test'), assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
class Element {
  constructor(tag) { this.tagName=tag; this.children=[]; this.disabled=false; this.checked=false; this.attributes={}; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children=items; }
  setAttribute(name,value) { this.attributes[name]=value; }
}
const find = (el,predicate) => predicate(el) ? el : el.children?.map(x=>find(x,predicate)).find(Boolean);
const source = fs.readFileSync(path.join(__dirname,'../frontend/membership-choice.js'),'utf8');
async function widget(args) {
  global.document={createElement:tag=>new Element(tag)};
  const {membershipChoice}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
  const root=membershipChoice(args); await root.ready; return root;
}
const inventory={category_name:'Flex',memberships:[],options:[
  {id:1,name:'Extra card',available:true,manual:true,token:'one-use'},
  {id:2,name:'Future plan',available:false,reason:'Starts after the workout'},
]};
test('unknown category requires explicit confirmation and invalid dates cannot be selected',async()=>{
  const calls=[];
  const root=await widget({load:async()=>inventory,save:async body=>{calls.push(body);return {ok:true};}});
  const radio=find(root,e=>e.tagName==='input'&&e.value==='1');
  const invalid=find(root,e=>e.tagName==='input'&&e.value==='2');
  const checkbox=find(root,e=>e.type==='checkbox');
  const save=find(root,e=>e.textContent==='שמירת המנוי לאימון');
  assert.equal(invalid.disabled,true); assert.equal(save.disabled,true);
  radio.onchange(); assert.equal(save.disabled,true);
  checkbox.checked=true; checkbox.onchange(); assert.equal(save.disabled,false);
  await save.onclick(); await save.onclick();
  assert.deepEqual(calls,[{token:'one-use',confirm_category:true}]);
});
test('lost response does not resend until an explicit refresh, which clears confirmation',async()=>{
  let calls=0,loads=0;
  const root=await widget({load:async()=>{loads++;return inventory;},save:async()=>{calls++;throw Error('Connection lost');}});
  const radio=find(root,e=>e.value==='1'), checkbox=find(root,e=>e.type==='checkbox');
  const save=find(root,e=>e.textContent==='שמירת המנוי לאימון');
  radio.onchange(); checkbox.checked=true;checkbox.onchange();
  await save.onclick();await save.onclick();assert.equal(calls,1);assert.equal(save.disabled,true);
  await find(root,e=>e.textContent==='רענון מנויים').onclick();
  assert.equal(loads,2);assert.equal(checkbox.checked,false);assert.equal(save.disabled,true);
});
test('a stale account cannot submit or overwrite the refreshed inventory',async()=>{
  let current=true,finish,saves=0,updates=0;
  const promise=new Promise(r=>finish=r);
  const {membershipChoice}=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
  const root=membershipChoice({load:()=>promise,save:async()=>saves++,isCurrent:()=>current,onInventory:()=>updates++});
  current=false;finish(inventory);await root.ready;
  assert.equal(updates,0);await find(root,e=>e.textContent==='שמירת המנוי לאימון').onclick();assert.equal(saves,0);
});
test('both frontends ship the same guarded picker',()=>{
  assert.equal(source,fs.readFileSync(path.join(__dirname,'../../custom_components/arbox/frontend/membership-choice.js'),'utf8'));
});
