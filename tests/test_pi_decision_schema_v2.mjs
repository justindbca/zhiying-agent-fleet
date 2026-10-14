// Task-mode Pi supervisor: the actual fleet_decision TypeBox schema (advisor plan §13 / design §20).
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import { Value } from 'typebox/value';
const {default: register}=await import(pathToFileURL(process.argv[2]).href);
const tools=new Map();
register({on(){},registerTool:(t)=>tools.set(t.name,t),registerEntryRenderer(){},registerMessageRenderer(){},appendEntry(){},sendMessage(){}});
const schema=tools.get('fleet_decision').parameters;
const base={status:'RUNNING',completion:10,reason:'r'};
assert.ok(Value.Check(schema,{...base,dispatch:[{to:'worker-1',instruction:'Write worker-1/a.md please'}]}));
assert.ok(Value.Check(schema,{...base,dispatch:[{to:'worker-1',instruction:'Write worker-1/a.md please',id:'M0',
  advise_note:'needs web research, no code'}]}),'dispatch[].id and advise_note must be accepted');
assert.ok(!Value.Check(schema,{...base,dispatch:[{to:'worker-1',instruction:'Write worker-1/a.md please',advise_note:3}]}));
assert.ok(!Value.Check(schema,{...base,dispatch:[{to:'worker-1',instruction:'Write worker-1/a.md please',id:5}]}),'id is typed text');
const one={...base,dispatch:[{to:'worker-1',instruction:'Write worker-1/a.md please'}]};
assert.ok(Value.Check(schema,{...one,advisor:'on'})&&Value.Check(schema,{...one,advisor:'off'}),'advisor on/off must be accepted');
assert.ok(!Value.Check(schema,{...one,advisor:'yes'})&&!Value.Check(schema,{...one,advisor:true}),'advisor is on or off only');
const ask={worker:'worker-1',task:'M0',finding:'两轮都 403',want:'补抓取工具'};
assert.ok(Value.Check(schema,{...base,dispatch:[],consult:[ask]}),'consult[] must be accepted (advisor plan §3.2)');
assert.ok(Value.Check(schema,{...base,dispatch:[],consult:[{worker:'worker-1',task:'M0',finding:'x'}]}),'want is optional');
assert.ok(!Value.Check(schema,{...base,dispatch:[],consult:[{...ask,worker:5}]}),'consult.worker is text');
assert.ok(!Value.Check(schema,{...base,dispatch:[],consult:[{worker:'worker-1',task:'M0'}]}),'consult.finding is required');
assert.ok(!Value.Check(schema,{...base,dispatch:[],consult:{...ask}}),'consult is a list');
assert.ok(Value.Check(schema,{...base,dispatch:[],advisor:'on',consult:[ask]}),'advisor on and consult in one reply');
// no-typing batch (7b14068f): withdraw[] of worker names, next to the advisor keys
const done={status:'COMPLETED',completion:100,reason:'r',dispatch:[]};
assert.ok(Value.Check(schema,{...done,withdraw:['worker-1']}),'withdraw[] must be accepted');
assert.ok(!Value.Check(schema,{...done,withdraw:'worker-1'})&&!Value.Check(schema,{...done,withdraw:[1]}),'withdraw is a list of names');
assert.ok(Value.Check(schema,{...done,withdraw:['worker-1'],advisor:'off'}),'withdraw and advisor in one reply');
assert.ok(Value.Check(schema,{...base,dispatch:[{to:'worker-1',instruction:'Write worker-1/a.md please',id:'M1',advise_note:'n'}],
  consult:[ask],advisor:'on'}),'every advisor key together');
// plan F' 8.3 / 8.4: escalations[] answers [escalate], [dialog] and [blocked]
const run={...base,dispatch:[]};
assert.ok(Value.Check(schema,{...run,escalations:[{task:'dialog:worker-1:g0',decision:'approve'}]}),'dialog approve');
assert.ok(Value.Check(schema,{...run,escalations:[{task:'blocked:worker-1',decision:'revise',note:'other way'}]}),'blocked revise');
assert.ok(Value.Check(schema,{...run,escalations:[{task:'M0',decision:'approve',workers:2,note:'n'}]}),'split approve');
assert.ok(!Value.Check(schema,{...run,escalations:[{task:'dialog:worker-1:g0',decision:'yes'}]}),'decision is an enum');
assert.ok(!Value.Check(schema,{...run,escalations:[{decision:'approve'}]}),'task is required');
assert.ok(!Value.Check(schema,{...run,escalations:{task:'M0',decision:'approve'}}),'escalations is a list');
console.log('decision schema contract passed');
