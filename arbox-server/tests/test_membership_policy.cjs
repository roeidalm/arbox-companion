const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
class Node {
  constructor(tag) {this.tag = tag; this.children = []; this.style = {}; this.attrs = {}; this.value = '';}
  append(...children) {for(const child of children){this.children.push(child);if(child && typeof child === 'object')child.parent=this;}}
  setAttribute(key,value){this.attrs[key]=value;}
  showModal(){this.open=true;}
  close(){this.open=false;this.onclose?.();}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(x=>x!==this);}
}
function setup(save=async()=>{}) {
  const file = path.join(__dirname,'../frontend/membership-policy.js');
  const body = new Node('body');
  const context = {document:{body,createElement:t=>new Node(t)}, Option:class extends Node{constructor(text,value){super('option');this.textContent=text;this.value=value;}}, Date};
  vm.createContext(context); vm.runInContext(fs.readFileSync(file,'utf8').replaceAll('export ',''),context);
  const member={id:7,plan:'<img src=x onerror=alert(1)>',sessions_on_purchase:5,policy:{fingerprint:'revision',category_ids:[1],limits:[],source:'manual'}};
  const dialog=context.policyDialog({member,categories:[{id:1,name:'Movement'}],save});
  return {context,dialog,body,member};
}
function find(root,tag){return [root,...root.children.flatMap(x=>x instanceof Node?find(x,tag):[])].filter(x=>x.tag===tag);}
test('the policy editor ships identically in HACS and standalone',()=>{
  assert.equal(fs.readFileSync(path.join(__dirname,'../frontend/membership-policy.js'),'utf8'),fs.readFileSync(path.join(__dirname,'../../custom_components/arbox/frontend/membership-policy.js'),'utf8'));
});
test('save sends explicit membership revision and category ids once',async()=>{
  let finish, count=0, saved;
  const {dialog}=setup(async data=>{count++;saved=data;await new Promise(r=>finish=r);});
  const form=find(dialog,'form')[0];
  const pending=form.onsubmit({preventDefault(){}});
  await form.onsubmit({preventDefault(){}});
  assert.equal(count,1);assert.deepEqual(JSON.parse(JSON.stringify(saved)),{category_ids:[1],fingerprint:'revision',limits:[]});
  finish();await pending;assert.equal(dialog.open,false);
});
test('failed save retains checked categories and re-enables submit',async()=>{
  const {dialog}=setup(async()=>{throw Error('offline');});
  const form=find(dialog,'form')[0];await form.onsubmit({preventDefault(){}});
  assert.equal(dialog.open,true);assert.equal(find(dialog,'input')[0].checked,true);
  assert.equal(find(dialog,'button').find(x=>x.type==='submit').disabled,false);
  assert.equal(find(dialog,'p').find(x=>x.attrs.role==='alert').textContent,'offline');
});
test('upstream names are plain text and read-only summaries have no edit action',()=>{
  const {dialog,context,member}=setup();
  assert.equal(find(dialog,'img').length,0);assert.equal(find(dialog,'h2')[0].textContent,member.plan);
  assert.equal(find(context.policySummary(member),'button').length,0);
  assert.equal(dialog.dir,'rtl');assert.ok(dialog.attrs['aria-label']);
});
test('weekly limits require the user to choose the studio week start', async()=>{
  let saved;
  const {dialog}=setup(async data=>{saved=data;});
  find(dialog,'button').find(x=>x.textContent==='הוספת מגבלה נוספת').onclick();
  const [period,weekStart]=find(dialog,'select');
  period.value='week';period.onchange();
  find(dialog,'input').find(x=>x.type==='number').value=1;
  const form=find(dialog,'form')[0];
  await form.onsubmit({preventDefault(){}});
  assert.equal(saved,undefined);
  assert.equal(weekStart.required,true);
  weekStart.value='6';
  await form.onsubmit({preventDefault(){}});
  assert.equal(saved.limits[0].week_start,6);
});
