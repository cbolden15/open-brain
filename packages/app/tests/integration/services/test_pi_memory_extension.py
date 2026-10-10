"""Synthetic SDK-shape fixtures, not native harness acceptance evidence."""

import shutil
import subprocess
from pathlib import Path

import pytest

ASSET = Path(__file__).parents[3] / "src/open_brain/assets/pi-memory.mjs"


def test_pi_memory_extension_fixture() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for Pi extension fixtures")
    script = r"""
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const {createPiMemoryExtension} = await import(pathToFileURL(process.argv[1]));
const handlers = {}, tools = [], calls = [], warnings = [];
const pi = {on: (name, fn) => handlers[name] = fn, registerTool: t => tools.push(t)};
const ctx = {ui: {notify: message => warnings.push(message)}};
let enforced = false, deny = false, pendingCapture, response = {status:'ok',results:[]};
const host = {assertLocalOnly: () => enforced,
  requireLocalOnly: () => { if (!deny) enforced = true; }};
const messages = Array.from({length:8}, (_,i) =>
  ({role:i%2?'assistant':'user',content:`fact ${i}`}));
messages.splice(2,0,{role:'toolResult',content:'never capture'});
let session = 'synthetic-session';
const branch = messages.map((message,index) => ({type:'message',id:`entry-${index}`,message}));
const manager = {getSessionId: () => session,getBranch: () => branch};
const bridge = {recall: async r => {calls.push(r); return response;},
  search: async r => {calls.push(r); return response;},
  capture: r => {calls.push(r); return new Promise(resolve => pendingCapture = resolve);}};
const schema = {type:'object',properties:{query:{type:'string'}},required:['query']};
createPiMemoryExtension({bridge,sessionManager:manager,project:'/synthetic/project',
  provider:'synthetic-provider',localOnlyHost:host,searchSchema:schema})(pi);
assert.equal(tools[0].name,'brain_search_page');
assert.equal(tools[0].parameters,schema);
assert.equal(await handlers.before_agent_start({prompt:'question'},ctx),undefined);
assert.equal(warnings.length,0);
response = {status:'unavailable',results:[]};
await handlers.before_agent_start({prompt:'question'},ctx);
assert.equal(warnings.length,1);
response = {status:'ok',results:Array.from({length:9},()=>
  ({text:'é'.repeat(9000),citation:'fixture'}))};
const recall = await handlers.before_agent_start({prompt:'question'},ctx);
assert.equal(recall,undefined);
assert.ok(warnings.at(-1).includes('omitted'));
assert.equal(calls.at(-1).limit,5);
const search = await tools[0].execute('id',{query:'question'},undefined,undefined,ctx);
assert.ok(Buffer.byteLength(search.content[0].text)<=8192);
assert.equal(search.details.omitted,true);
assert.ok(!search.content[0].text.includes('No relevant'));
response = {status:'ok',results:[{text:'complete row',citation:'complete citation'}]};
const complete = await handlers.before_agent_start({prompt:'question'},ctx);
assert.ok(complete.message.content.includes('complete row\nSource: complete citation'));
let ended = false;
const capture = handlers.agent_end({},ctx).then(()=>ended=true);
await new Promise(resolve=>setTimeout(resolve,10));
assert.equal(ended,false);
const request = calls.at(-1);
assert.equal(request.client,'pi');
assert.equal(request.messages.length,8);
assert.deepEqual(request.messages.map(m=>m.ordinal),[0,1,2,3,4,5,6,7]);
assert.ok(request.messages.every(m=>m.authored===true));
assert.deepEqual(request.messages.map(m=>m.source_id),
  ['entry-0','entry-1','entry-3','entry-4','entry-5','entry-6','entry-7','entry-8']);
pendingCapture({status:'queued'}); await capture;
branch.splice(2,0,{type:'custom_message',customType:'memory',content:'generated reference'},
  {type:'compaction',summary:'generated summary'},
  {type:'message',message:{role:'custom',content:'generated context'}});
for (const status of ['acknowledged','saved']) {
  const warningsBefore = warnings.length;
  const nextCapture = handlers.agent_end({},ctx);
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.deepEqual(calls.at(-1).messages,request.messages);
  pendingCapture({status}); await nextCapture;
  assert.equal(warnings.length,warningsBefore);
}
const originalBranch = branch.slice();
// A fork keeps original entry IDs even when a shorter branch changes ordinals.
session='synthetic-fork';
branch.splice(0,branch.length,...originalBranch.filter(entry=>entry.id==='entry-7'
  || entry.id==='entry-8'));
const forkCapture = handlers.agent_end({},ctx);
await new Promise(resolve=>setTimeout(resolve,0));
assert.deepEqual(calls.at(-1).messages.map(m=>m.source_id),['entry-7','entry-8']);
assert.deepEqual(calls.at(-1).messages.map(m=>m.ordinal),[0,1]);
pendingCapture({status:'queued'}); await forkCapture;
session='synthetic-session';
branch.splice(0,branch.length,...originalBranch);
const previousId=branch[0].id, beforeInvalid=calls.length;
branch[0].id='invalid id';
await handlers.agent_end({},ctx);
assert.equal(calls.length,beforeInvalid);
branch[0].id=previousId;
const start = Date.now(), warningCount = warnings.length;
await handlers.agent_end({},ctx);
assert.ok(Date.now()-start>=1900 && Date.now()-start<2300);
assert.equal(warnings.length,warningCount+1);
assert.equal(calls.at(-1).signal.aborted,true);
response={status:'ok',requires_local_only:true,results:[{text:'local fixture'}]};
deny=true;
assert.equal(await handlers.before_agent_start({prompt:'question'},ctx),undefined);
const refused=await tools[0].execute('id',{query:'question'},undefined,undefined,ctx);
assert.equal(refused.isError,true);
assert.ok(!JSON.stringify(refused).includes('local fixture'));
deny=false;
await handlers.before_agent_start({prompt:'question'},ctx);
assert.equal(enforced,true);
assert.equal(calls.at(-1).local_only,true);
response={status:'ok',results:[]};
await handlers.before_agent_start({prompt:'question'},ctx);
assert.equal(calls.at(-1).provider,'local');
enforced=false;
const before=calls.length;
await handlers.before_agent_start({prompt:'question'},ctx);
assert.equal(calls.length,before);
assert.throws(()=>createPiMemoryExtension({bridge,sessionManager:manager,
  project:'/synthetic',provider:'local',localOnlyHost:host,searchSchema:schema})(pi));

// Shared recall deadline includes initial read, confinement and repeat read.
const slowHandlers = {}, slowCalls = [], slowWarnings = [];
let slowEnforced = false;
const pause = ms => new Promise(resolve=>setTimeout(resolve,ms));
createPiMemoryExtension({bridge:{...bridge,recall: async r => {
  slowCalls.push(r);
  if (slowCalls.length===1) { await pause(900); return {requires_local_only:true}; }
  return new Promise(()=>{});
}},sessionManager:manager,project:'/synthetic/project',provider:'synthetic-provider',
localOnlyHost:{assertLocalOnly:()=>slowEnforced,requireLocalOnly:async ({signal})=> {
  await pause(900); assert.equal(signal.aborted,false); slowEnforced=true;
}},searchSchema:schema})({on:(n,f)=>slowHandlers[n]=f,registerTool:()=>{}});
const deadlineStart = Date.now();
await slowHandlers.before_agent_start({prompt:'question'},
  {ui:{notify:m=>slowWarnings.push(m)}});
assert.ok(Date.now()-deadlineStart>=2900 && Date.now()-deadlineStart<3300);
assert.equal(slowCalls.length,2);
assert.equal(slowCalls[0].signal,slowCalls[1].signal);
assert.equal(slowCalls[1].signal.aborted,true);
assert.equal(slowWarnings.length,1);

// Caller cancellation settles promptly even if the bridge ignores cancellation.
let resolveRead;
const interruptHandlers = {}, interruptTools = [];
let activeLocal = true;
createPiMemoryExtension({bridge:{...bridge,recall:r=>new Promise(resolve=>resolveRead=resolve),
  search:r=>new Promise(resolve=>resolveRead=resolve)},sessionManager:manager,
project:'/synthetic/project',provider:'local',localOnlyHost:{assertLocalOnly:()=>activeLocal,
requireLocalOnly:()=>{}},searchSchema:schema})(
  {on:(n,f)=>interruptHandlers[n]=f,registerTool:t=>interruptTools.push(t)});
const controller = new AbortController(), abortStart = Date.now();
const aborted = interruptTools[0].execute('id',{query:'question'},controller.signal);
await pause(10); controller.abort();
assert.equal((await aborted).isError,true);
assert.ok(Date.now()-abortStart<200);
const regressed = interruptHandlers.before_agent_start({prompt:'question'},ctx);
await pause(0); activeLocal=false;
resolveRead({status:'ok',results:[{text:'never release regression'}]});
assert.equal(await regressed,undefined);
activeLocal=true;
const switched = interruptHandlers.before_agent_start({prompt:'question'},ctx);
await pause(0); session='new-synthetic-session';
resolveRead({status:'ok',results:[{text:'never release switched session'}]});
assert.equal(await switched,undefined);
console.log('synthetic fixture passed');
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script, str(ASSET)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
