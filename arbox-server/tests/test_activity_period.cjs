const {test}=require('node:test');
const assert=require('node:assert/strict');
const {range,move}=require('../frontend/activity-period.js');
test('week begins Sunday and spans a year boundary',()=>assert.deepEqual(range('week','2027-01-01'),{date_from:'2026-12-27',date_to:'2027-01-02'}));
test('month and leap day',()=>assert.deepEqual(range('month','2028-02-29'),{date_from:'2028-02-01',date_to:'2028-02-29'}));
test('month navigation never skips February from the 31st',()=>assert.equal(move('month','2026-01-31',null,1).anchor,'2026-02-01'));
test('custom navigation preserves inclusive length across DST',()=>assert.deepEqual(move('custom','2026-03-26','2026-03-28',1),{anchor:'2026-03-29',end:'2026-03-31'}));
test('all and invalid ranges',()=>{assert.deepEqual(range('all'),{});assert.throws(()=>range('custom','2026-10-02','2026-10-01'));assert.throws(()=>range('day','2026-02-30'));});
test('period label distinguishes this week from another week',()=>{
  const {label}=require('../frontend/activity-period.js');
  assert.equal(label('week','2026-09-20',null,'2026-09-25').title,'השבוע');
  assert.equal(label('week','2026-09-13',null,'2026-09-25').current,false);
  assert.equal(label('all',null,null,'2026-09-25').title,'כל התקופות');
});
