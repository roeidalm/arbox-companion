/* Run from the repository root with Playwright installed. Synthetic API only.
 * Optional PLAYWRIGHT_MODULE and CHROMIUM_PATH select existing local runtimes.
 * Screenshots go to ignored tmp/ui-review. No HA installation or Arbox access.
 */
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const http=require('node:http'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=process.cwd(),out=path.join(root,'tmp/ui-review');fs.mkdirSync(out,{recursive:true});
const members=[
 {id:1,plan:'מנוי בואו נזוז — 1 בשבוע (5 בחודש), הוראת קבע',active:true,recurring:true,period:'month',quota:5,used:2,reserved:2,planned:1,available_after_planned:0,period_start:'2026-09-01',period_end:'2026-09-30',policy:{state:'ready',fingerprint:'monthly',source:'shop',verified_at:1788864000,category_ids:[1,3],limits:[{period:'month',count:5}]}},
 {id:2,plan:'כרטיסיית מובמנט',active:true,sessions_on_purchase:5,period:'card',quota:5,used:0,reserved:1,planned:1,available_after_planned:3,period_start:'2026-09-02',period_end:'2026-10-01',policy:{state:'ready',fingerprint:'card',source:'rejection',verified_at:1788864000,category_ids:[2],limits:[{period:'card',count:5}]}}
];
const rows=[];for(let day=13;day<=19;day++)for(let j=0;j<6;j++)rows.push({schedule_id:day*100+j,date:`2026-09-${day}`,start_time:`${String(8+j*2).padStart(2,'0')}:00`,end_time:`${String(9+j*2).padStart(2,'0')}:15`,category_name:['HS, Mobility & Strength','Movement basics','Flex- Back\\Arches'][j%3],category_id:j%3+1,coach_name:['נועה ברומפמן','רוני גוזלי','ליאור מרפי'][j%3],registered:8,max_users:12,free:4,registration_open:true,booking_option:'insertScheduleUser',user_booked:null,user_in_standby:null});
Object.assign(rows[0],{user_booked:100,booking_option:'cancelScheduleUser',membership_user_id:1});
Object.assign(rows[7],{user_in_standby:101,booking_option:'cancelWaitList',membership_user_id:2});
Object.assign(rows[14],{watched:true,registration_open:false,planning:{state:'ready',membership_user_id:2,reason:'מכוסה במנוי ובמכסה'}});
Object.assign(rows[18],{watched:true,registration_open:false,planning:{state:'no_capacity',membership_user_id:1,reason:'המכסה של המנוי המתאים מלאה — ההרשמה מושהית'}});
Object.assign(rows[24],{planning_source:'autobook',autobook_match:true,registration_open:false,planning:{state:'ready',membership_user_id:1,reason:'מכוסה במנוי ובמכסה'}});
const mine=[rows[0],rows[7],rows[14],rows[18],rows[24]];
const quota={used:2,reserved:3,planned_total:4,planned_scheduled:3,planned_autobook:1,memberships:members,uncovered_plans:[rows[18].schedule_id],unresolved_plans:[],unattributed_sessions:[],overcommitted:true};
const context={studio_id:8,name:'סטודיו בדיקה',timezone:'Asia/Jerusalem',last_sync:'2026-09-08T20:00:00+03:00'};
const facets={coaches:['נועה ברומפמן','רוני גוזלי','ליאור מרפי'],categories:[{id:1,name:'HS, Mobility & Strength'},{id:2,name:'Movement basics'},{id:3,name:'Flex- Back\\Arches'}]};
const fixture={health:{configured:true,authenticated:true,timezone:'Asia/Jerusalem',version:'dev',last_sync:new Date().toISOString()},studios:{selected_studio_id:8,studios:[{id:8,name:'סטודיו בדיקה',selected:true}]},summary:{memberships:members,quota,next_class:rows[0]},quota,me:{sessions:mine,memberships:members,membership:members[0]},schedule:{sessions:rows,last_sync:new Date().toISOString()},facets,watchlist:{watchlist:[{schedule_id:rows[14].schedule_id,membership_user_id:2},{schedule_id:rows[18].schedule_id,membership_user_id:1}]},'membership-policies':{memberships:members,categories:facets.categories},settings:{available_memberships:members,telegram:{},ha:{},journal:{},notify:{},timezone:'Asia/Jerusalem'},profile:{profile:{full_name:'בדיקת ממשק',studio:{name:'סטודיו בדיקה'}},memberships:members,membership:members[0],quota},messages:{messages:[],items:[]},history:{sessions:[],stats:{}},journal:{entries:[],catalogue:[],settings:{}},rules:{rules:[]},vacations:{active:[],history:[]}};
const harness=`<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>body{margin:0}arbox-app-panel{display:block}</style><script type="module">import '/arbox_frontend/app-panel.js';const fixture=${JSON.stringify(fixture)},context=${JSON.stringify(context)};window.writes=[];window.fixture=fixture;const p=document.createElement('arbox-app-panel');window.panel=p;p.panel={config:{}};p.hass={connected:true,themes:{darkMode:true},callWS:async msg=>{if(msg.type==='arbox/panel/entries')return {entries:[{entry_id:'demo',title:'סטודיו בדיקה',can_write:true}]};if(msg.type==='arbox/panel/action'){window.writes.push(msg);return {data:{ok:true}};}const key=msg.resource==='membership_policies'?'membership-policies':msg.resource;return {data:fixture[key]||{},context,can_write:true};}};document.body.append(p);p._date='2026-09-13';</script>`;
const mutations=[];
const server=http.createServer((req,res)=>{
 const url=new URL(req.url,'http://local'),name=url.pathname;
 if(name.startsWith('/api/')){if(req.method!=='GET'){mutations.push({path:name,method:req.method});res.writeHead(200,{'Content-Type':'application/json'});return res.end('{"ok":true}');}const key=name.slice(5);res.writeHead(200,{'Content-Type':'application/json'});return res.end(JSON.stringify(fixture[key]||{}));}
 if(name==='/ha-test'){res.setHeader('Content-Type','text/html');return res.end(harness);}
 const file=name.startsWith('/static/')?path.join(root,'arbox-server/frontend',name.slice(8)):name.startsWith('/arbox_frontend/')?path.join(root,'custom_components/arbox/frontend',name.slice(16)):path.join(root,'arbox-server/frontend/index.html');
 try{res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html');res.end(fs.readFileSync(file));}catch(e){res.writeHead(404);res.end('missing');}
});
(async()=>{
 await new Promise(r=>server.listen(0,'127.0.0.1',r));const origin=`http://127.0.0.1:${server.address().port}`;
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_PATH});
 const page=await browser.newPage();const errors=[];page.on('pageerror',e=>{errors.push(e.message);console.error('PAGE',e.message);});page.on('console',m=>{if(m.type()==='error')console.error('CONSOLE',m.text());});
 await page.route('**/*',route=>route.request().url().startsWith(origin)?route.continue():route.abort());
 await page.addInitScript(()=>{localStorage.setItem('arbox_api_key','synthetic-only');localStorage.setItem('arbox_theme','dark');});
 await page.clock.install({time:new Date('2026-09-08T17:00:00Z')});
 for(const [mode,url] of [['web','/mine'],['ha','/ha-test#mine']]){
  for(const width of [1440,390,320]){
   await page.setViewportSize({width,height:900});await page.goto(origin+url);await page.reload();await page.locator('.mu-bar').first().waitFor({timeout:8000}).catch(async e=>{console.error(await page.locator('body').innerText());throw e;});
   const welcome=page.getByRole('button',{name:'הבנתי',exact:true}); if(await welcome.isVisible()) await welcome.click();
   assert.equal(await page.locator('.mu-quota-row').count(),2);
   const bars=await page.locator('.mu-bar').evaluateAll(ns=>ns.map(n=>({width:n.getBoundingClientRect().width,parts:[...n.children].reduce((v,x)=>v+x.getBoundingClientRect().width,0)})));
   assert.ok(bars.every(b=>Math.abs(b.width-b.parts)<2),'quota bar must fill entire track');
   const overflow=await page.evaluate(()=>{const p=document.querySelector('arbox-app-panel');const el=p?.shadowRoot.querySelector('.app')||document.documentElement;return el.scrollWidth-el.clientWidth;});if(overflow>=3) console.log('OVERFLOW',mode,width,await page.evaluate(()=>[...document.querySelectorAll('*')].filter(n=>n.getBoundingClientRect().right>innerWidth+1||n.getBoundingClientRect().left< -1).map(n=>({tag:n.tagName,cls:n.className,width:n.getBoundingClientRect().width,text:n.innerText?.slice(0,70)})).slice(0,15)));assert.ok(overflow<3,`${mode} ${width} overflows by ${overflow}`);
   await page.screenshot({path:path.join(out,`${mode}-mine-${width}.png`)});
   if(width===390){
     await page.evaluate(mode=>{if(mode==='ha')panel.hass={...panel._hass,themes:{darkMode:false}};else document.documentElement.dataset.theme='light';},mode);
     await page.screenshot({path:path.join(out,`${mode}-mine-light.png`)});
     await page.evaluate(mode=>{if(mode==='ha')panel.hass={...panel._hass,themes:{darkMode:true}};else document.documentElement.dataset.theme='dark';},mode);
   }
   await page.getByRole('button',{name:'ניהול מנויים בסטודיו',exact:true}).click();await page.locator('.mu-membership').first().waitFor();
   assert.equal(await page.locator('.mu-membership[open]').count(),0);
   await page.locator('.mu-membership>summary').first().click();
   await page.getByRole('button',{name:'סוגי שיעורים ומכסה',exact:true}).click();await page.locator('.mu-editor').waitFor();
   assert.equal(await page.locator('dialog[open]').count(),0,'studio edit stays inline');
   const checkbox=page.locator('.mu-editor input[type=checkbox]').first();await checkbox.uncheck();
   if(mode==='ha'){await page.evaluate(()=>{fixture.summary.quota.extraPoll=1;return panel.load();});assert.equal(await checkbox.isChecked(),false,'poll preserves draft');}
   const studioOverflow=await page.evaluate(()=>{const el=document.querySelector('arbox-app-panel')?.shadowRoot.querySelector('.app')||document.documentElement;return el.scrollWidth-el.clientWidth;});assert.ok(studioOverflow<3,`${mode} Studio ${width} overflows by ${studioOverflow}`);
   await page.screenshot({path:path.join(out,`${mode}-studio-${width}.png`)});
  }
  if(mode==='ha'){
   await page.goto(origin+'/ha-test#mine');await page.locator('.mine-card').first().waitFor();
   const scheduled=page.locator('.mine-card').filter({hasText:'בדיקה כאן'});await scheduled.getByRole('button',{name:'בדיקה כאן',exact:true}).click();
   const select=scheduled.getByRole('combobox',{name:'באיזה מנוי להשתמש?'});assert.equal(await select.inputValue(),'1');await select.selectOption('2');
   await scheduled.getByRole('button',{name:'עדכון המנוי לאימון'}).click();
   assert.deepEqual(await page.evaluate(()=>writes.map(w=>[w.action,w.data.membership_user_id,w.studio_id])),[['watch_membership',2,8]]);
   await page.goto(origin+'/ha-test#schedule');await page.getByRole('button',{name:'שבוע',exact:true}).click();
   await page.waitForFunction(()=>panel._view==='week');await page.locator('.pc-week').waitFor();assert.equal(await page.locator('.pc-week .session-card').count(),42);
   assert.equal(await page.locator('.pc-week .pc-automatic').count()>0,true);
   const filters=page.locator('.filter-picker').filter({has:page.locator('summary').filter({hasText:'מאמן/ת'})});
   await filters.locator('summary').click();await filters.getByRole('checkbox',{name:'נועה ברומפמן',exact:true}).check();await filters.getByRole('checkbox',{name:'רוני גוזלי',exact:true}).check();
   assert.equal(await page.locator('.pc-week .session-card').count(),28);
   await filters.getByRole('button',{name:'הכול',exact:true}).click();await filters.locator('summary').click();
  }else{
   await page.goto(origin+'/schedule?mode=week&d=2026-09-13');await page.locator('.session').first().waitFor();assert.equal(await page.locator('.session').count(),42);
   assert.equal(await page.locator('.session.schedule-autobook').count(),1);
   const filters=page.locator('#coachChips .filter-picker');await filters.locator('summary').click();await filters.getByRole('checkbox',{name:'נועה ברומפמן',exact:true}).check();await filters.getByRole('checkbox',{name:'רוני גוזלי',exact:true}).check();assert.equal(await page.locator('.session').count(),28);
   await filters.getByRole('button',{name:'הכול',exact:true}).click();await filters.locator('summary').click();
  }
  for(const view of ['יום','שבוע','חודש']) {
    await page.getByRole('button',{name:view,exact:true}).click();
    await page.setViewportSize({width:320,height:900});
    const overflow=await page.evaluate(()=>{const el=document.querySelector('arbox-app-panel')?.shadowRoot.querySelector('.app')||document.documentElement;return el.scrollWidth-el.clientWidth;});assert.ok(overflow<3,`${mode} ${view} calendar overflow ${overflow}`);
  }
  await page.getByRole('button',{name:'שבוע',exact:true}).click();
  await page.setViewportSize({width:1440,height:900});await page.screenshot({path:path.join(out,`${mode}-schedule-wide.png`)});
 }
 assert.deepEqual(errors,[]);assert.deepEqual(mutations,[]);
 console.log('PASS: web + HA component at 1440/390/320px, dark/light; full quota tracks, inline Studio editing, preserved draft, all 42 classes, correct scoped membership action; no real API calls.');
 await browser.close();server.close();
})().catch(e=>{console.error(e);process.exit(1)});
