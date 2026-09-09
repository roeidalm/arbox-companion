const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
class Element {
  constructor(tag){this.tag=tag;this.children=[];this.dataset={};this.attrs={};this.style={setProperty(){}};this.value='';}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;}
  setAttribute(k,v){this.attrs[k]=v;}
  focus(){this.focused=true;}
  get lastChild(){return this.children.at(-1);}
  querySelectorAll(tag){return descendants(this).filter(n=>n.tag===tag);}
}
const descendants=root=>root.children.flatMap(n=>typeof n==='object'?[n,...descendants(n)]:[]);
function setup(){
 const context={document:{createElement:tag=>new Element(tag)}};
 vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../frontend/filter-picker.js'),'utf8').replaceAll('export ',''),context);
 return context;
}
test('search keeps prior selection visible first, adds a second coach, and Everything clears only the group',()=>{
 const {filterPicker}=setup(); let chosen;
 const root=filterPicker({label:'מאמנים',values:['ליאור','ניר','נועה'],selected:[],change:v=>{chosen=Array.from(v)}});
 const input=descendants(root).find(n=>n.tag==='input');
 const select=value=>descendants(root).find(n=>n.className==='filter-color-chip'&&n.children[0].textContent===value).onclick();
 input.value='ליאור';input.oninput();select('ליאור');
 input.value='ניר';input.oninput();
 const options=()=>descendants(root).filter(n=>n.className==='filter-color-chip');
 assert.deepEqual(options().map(n=>n.children[0].textContent),['ליאור','ניר']);
 assert.equal(options()[0].attrs['aria-pressed'],'true');
 select('ניר');assert.deepEqual(chosen,['ליאור','ניר']);
 descendants(root).find(n=>n.textContent==='הכל').onclick();
 assert.deepEqual(chosen,[]);assert.equal(input.value,'');
});
test('compact summary renders current workout counts and mobile expansion without changing filters',()=>{
 const {workoutSummary}=setup();let changed=0,open;
 const summary=workoutSummary([{category_name:'Movement',coach_name:'Dana'},{category_name:'Movement',coach_name:'Noa'}],{change:()=>changed++,toggle:value=>{open=value}});
 assert.equal(summary.dataset.open,'false');
 assert.ok(descendants(summary).some(n=>n.textContent==='×2'));
 descendants(summary).find(n=>n.className==='workout-summary-toggle').onclick();
 assert.equal(summary.dataset.open,'true');assert.equal(open,true);assert.equal(changed,0);
});
