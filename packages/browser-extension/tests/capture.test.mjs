import test from 'node:test';
import assert from 'node:assert/strict';
import {displayUrl, sameDocument, saveArgs, receiptState} from '../extension/capture.mjs';

const page = {tabId: 7, documentId: 'a', url: 'https://example.org/a#private', title: '<img>', passage: 'quote'};
test('the preview hides the fragment while the confirmed request preserves identity', () => {
  assert.equal(displayUrl(page.url), 'https://example.org/a');
  assert.deepEqual(saveArgs(page, ' why '), {url: page.url, title: '<img>', passage:'quote', reason:'why'});
  assert.throws(() => displayUrl('file:///private'));
  assert.throws(() => saveArgs(page, ' '));
});
test('tab, document, URL and selection must still match', () => {
  assert.ok(sameDocument(page, {...page}));
  for (const key of ['tabId','documentId','url','passage'])
    assert.equal(sameDocument(page, {...page, [key]: 'changed'}), false);
});
test('only acknowledged acceptance permits forgetting an uncertain ID', () => {
  assert.equal(receiptState({state:'accepted', capture_id:'id'}), 'saved');
  assert.equal(receiptState({state:'pending'}), 'pending');
  for (const result of [undefined, {state:'refused'}, {state:'absent'}, {state:'accepted'}])
    assert.equal(receiptState(result), 'unknown');
});

const binding = '1'.repeat(64);
const popup = 'chrome-extension://example/popup.html';
async function workerFixture() {
  let handler;
  const calls = [];
  const storage = {};
  const receipts = new Map();
  const env = {delivery:'accepted', documentId:'doc-one', active:true, online:true};
  let trusted = false;
  globalThis.chrome = {
    runtime: {id:'example', getURL:() => popup, onInstalled:{addListener(){}},
      onMessage:{addListener(fn){handler = fn;}},
      async sendNativeMessage(_host, message) {
        calls.push(message);
        if (!env.online) throw new Error('offline');
        const {operation, operation_id:id} = message;
        if (operation !== 'binding.describe') assert.equal(message.binding_id, binding);
        let result;
        if (operation === 'binding.describe') result = {binding_id:binding,destination:'This computer'};
        if (operation === 'page.lookup') result = {state:'absent'};
        if (operation === 'operation.status') result = receipts.get(id) ?? {state:'refused'};
        if (operation === 'page.save') {
          if (env.delivery === 'never') throw new Error('before admission');
          result = env.delivery === 'pending' ? {state:'pending'} : {state:'accepted',capture_id:'fixture'};
          receipts.set(id,result);
          if (env.delivery === 'lost') throw new Error('after admission');
        }
        return {version:'browser.v1',result};
      }},
    tabs:{async get(){return {url:page.url, windowId:1, active:env.active};}},
    windows:{async get(){return {focused:true};}},
    scripting:{async executeScript(){return [{documentId:env.documentId,
      result:{url:page.url,title:page.title,passage:page.passage}}];}},
    contextMenus:{onClicked:{addListener(){}}},
    storage:{local:{async setAccessLevel(value){trusted = value.accessLevel === 'TRUSTED_CONTEXTS';},
      async get(){assert.ok(trusted); return structuredClone(storage);},
      async set(value){assert.ok(trusted); Object.assign(storage,structuredClone(value));}}}
  };
  async function restart(){await import('../extension/worker.js?instance=' + crypto.randomUUID());}
  function request(action,fields={}){return new Promise(resolve => {
    handler({action,...fields},{id:'example',url:popup},resolve);
  });}
  async function begin(){return (await request('begin',{tabId:7,windowId:1})).value;}
  async function save(nonce){return request('save',{nonce,reason:'private reason',windowId:1});}
  await restart();
  return {env,storage,receipts,calls,restart,request,begin,save};
}
test('lost receipt survives worker restarts and outage, status never sends another mutation',async () => {
  const f = await workerFixture();
  f.env.delivery = 'lost';
  const preview = await f.begin();
  assert.equal(preview.lookup,'absent');
  assert.equal((await f.save(preview.nonce)).value.state,'unknown');
  const [{id}] = f.storage.pending;
  assert.deepEqual(f.storage.pending,[{id,binding}]);
  f.env.online = false;
  await f.restart();
  assert.equal((await f.request('status',{id})).value.state,'unknown');
  assert.deepEqual(f.storage.pending,[{id,binding}]);
  f.env.online = true;
  assert.equal((await f.request('status',{id})).value.state,'saved');
  assert.deepEqual(f.storage.pending,[]);
  assert.equal(f.calls.filter(x => x.operation === 'page.save').length,1);
  f.env.delivery = 'accepted';
  await f.save((await f.begin()).nonce);
  assert.equal(f.receipts.size,2);
});
test('never-delivered attempt requires owner action and scoped refusal retains the ID',async () => {
  const f = await workerFixture();
  f.env.delivery = 'never';
  const {nonce} = await f.begin();
  await f.save(nonce);
  const [{id}] = f.storage.pending;
  assert.equal((await f.request('status',{id})).value.state,'unknown');
  assert.deepEqual(f.storage.pending,[{id,binding}]);
  assert.equal((await f.save(nonce)).error,'changed');
  await f.request('abandon',{id});
  assert.deepEqual(f.storage.pending,[]);
  f.env.delivery = 'pending';
  assert.equal((await f.save((await f.begin()).nonce)).value.state,'pending');
  assert.notEqual(f.storage.pending[0].id,id);
});
test('changed document cancels before native save and missing helper surfaces separately',async () => {
  const f = await workerFixture();
  const {nonce} = await f.begin();
  f.env.documentId = 'doc-two';
  assert.equal((await f.save(nonce)).error,'changed');
  assert.deepEqual(f.storage.pending,[]);
  assert.equal(f.calls.filter(x => x.operation === 'page.save').length,0);
  f.env.online = false;
  assert.equal((await f.request('begin',{tabId:7,windowId:1})).error,'unavailable');
});
