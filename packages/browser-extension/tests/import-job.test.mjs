import test from 'node:test';
import assert from 'node:assert/strict';
import {organizer} from '../extension/organize.mjs';

function fixture(step) {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id,{
      handlers:new Map(), hidden:false, addEventListener(type,fn){ this.handlers.set(type,fn); },
      setAttribute(){}, removeAttribute(){}, replaceChildren(){}, append(){}, focus(){},
    });
    return elements.get(id);
  }
  globalThis.document = {getElementById:element,createElement:() => element('row')};
  const calls = [];
  const job = {id:'job',binding:'workflow'};
  let state = 'active';
  async function request(action) {
    calls.push(action);
    if (action === 'boundImport') return job;
    if (action === 'stepImport') return step();
    if (action === 'cancelImport') state = 'canceled';
    return {state,processed:0,total:10,rows:[],next_offset:null};
  }
  const open = organizer(request,()=>({}),()=>{});
  return {calls, open:() => open({expected:{binding:'workflow'}}), click(id) {
    return element(id).handlers.get('click')({preventDefault(){}});
  }, element};
}

test('an admitted import with no progress yields to Resume and Cancel',async () => {
  const f = fixture(async () => ({state:'active',processed:0,total:10}));
  await f.open();
  await f.click('import-resume');
  assert.equal(f.calls.filter(c => c === 'stepImport').length,2);
  assert.equal(f.element('import-resume').hidden,false);
  assert.match(f.element('import-status').textContent,/pending/);
});

test('cancel during processing takes effect after the in-flight step',async () => {
  const pending = Promise.withResolvers();
  const f = fixture(() => pending.promise);
  await f.open();
  const processing = f.click('import-resume');
  await f.click('import-cancel');
  pending.resolve({state:'active',processed:4,total:10});
  await processing;
  assert.equal(f.calls.filter(c => c === 'stepImport').length,1);
  assert.equal(f.calls.filter(c => c === 'cancelImport').length,1);
  assert.equal(f.element('import-done').hidden,false);
});
