const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const load=name=>import('data:text/javascript;base64,'+Buffer.from(fs.readFileSync(path.join(__dirname,'../frontend',name),'utf8')).toString('base64'));
test('quota track includes the server-provided free remainder across the whole capacity',async()=>{
 const {quotaSegments}=await load('membership-ui.js');
 const parts=quotaSegments({quota:5,used:0,reserved:1,planned:1,available_after_planned:3});
 assert.deepEqual(parts.map(p=>[p.kind,p.value,p.percent]),[['reserved',1,20],['planned',1,20],['free',3,60]]);
 assert.equal(parts.reduce((sum,p)=>sum+p.percent,0),100);
 const empty=quotaSegments({quota:5,available_after_planned:5});assert.equal(empty[0].percent,100);assert.equal(empty[0].kind,'free');
});
test('unknown or zero capacity never becomes a claim of free places',async()=>{
 const {quotaSegments}=await load('membership-ui.js');
 assert.deepEqual(quotaSegments({quota:null,used:1}),[]);
 assert.deepEqual(quotaSegments({quota:0}),[]);
 const unknown=quotaSegments({quota:5,used:1});
 assert.equal(unknown.some(p=>p.kind==='free'),false);assert.equal(unknown.find(p=>p.kind==='unknown').value,4);
});
test('waiting and uncertain commitments remain visible without inventing capacity',async()=>{
 const {quotaSegments}=await load('membership-ui.js');
 const parts=quotaSegments({quota:5,used:1,reserved:1,pending_standby:1,planned:1,uncertain:1,available_after_planned:0});
 assert.deepEqual(parts.map(p=>p.kind),['used','reserved','standby','planned','uncertain']);
 const overflow=quotaSegments({quota:5,used:5,reserved:2});
 assert.equal(overflow.reduce((sum,p)=>sum+p.value,0),7);assert.equal(overflow.some(p=>p.kind==='free'),false);
 assert.ok(Math.abs(overflow.reduce((sum,p)=>sum+p.percent,0)-100)<.001);
});
test('multiple coaches/categories use OR inside a filter and AND across filters',async()=>{
 const {matchesFilters}=await load('filter-picker.js');
 const row={coach_name:'Dana',category_name:'Movement',start_time:'09:30'};
 assert.equal(matchesFilters(row,{}),true);
 assert.equal(matchesFilters(row,{coach_name:['Dana','Noa'],category_name:['Movement','Flex']}),true);
 assert.equal(matchesFilters(row,{coach_name:['Noa'],category_name:['Movement']}),false);
 assert.equal(matchesFilters(row,{coach_name:[],daypart:'morning'}),true);
 assert.equal(matchesFilters(row,{coach_name:'Dana',daypart:'evening'}),false);
});
test('HACS bundles identical quota and filter modules',()=>{
 for(const name of ['membership-ui.js','membership-ui.css','filter-picker.js'])
  assert.equal(fs.readFileSync(path.join(__dirname,'../frontend',name),'utf8'),fs.readFileSync(path.join(__dirname,'../../custom_components/arbox/frontend',name),'utf8'));
});
