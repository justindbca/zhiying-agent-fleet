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
console.log('decision schema contract passed');
