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
        if (operation === 'binding.describe') result = {binding_id:binding,destination:env.destination ?? 'This computer'};
        if (operation === 'page.lookup') result = {state:env.lookup ?? 'absent', ...(env.lookup === 'saved' && {capture_id:'fixture'})};
        if (operation === 'page.lookup' && env.lookupOffline) throw new Error('offline');
        if (operation === 'annotation.add') {
          result = {state:'accepted',capture_id:'fixture',annotation_id:message.args.annotation_id};
          receipts.set(id, result);
          if (env.delivery === 'lost') throw new Error('reply lost');
        }
        if (operation === 'annotation.list') result = {annotations:[], next_offset:null};
        if (operation === 'operation.status') result = receipts.get(id) ?? {state:'refused'};
        if (operation === 'page.save') {
          if (env.nativeError) return {version:'browser.v1',error:{code:env.nativeError}};
          if (env.delivery === 'never') throw new Error('before admission');
          result = env.delivery === 'pending' ? {state:'pending'} : {state:'accepted',capture_id:'fixture'};
          receipts.set(id,result);
          if (env.delivery === 'lost') throw new Error('after admission');
        }
        return {version:'browser.v1',result};
      }},
    tabs:{onActivated:{addListener(fn){env.activate = fn;}},onUpdated:{addListener(fn){env.update = fn;}},async query(){return env.active ? [{id:7,url:page.url}] : [{id:8}];},async get(){return {url:page.url, windowId:1, active:env.active};}},
    windows:{onRemoved:{addListener(){}},async get(){return {focused:true};}},
    scripting:{async executeScript({func, files}){if (files) return [];
      if (func.name === 'readAnchor') return [{documentId:env.documentId,
        result:{url:page.url, quote:page.passage,prefix:'Start ',suffix:' end'}}];
      env.readPage = func; await env.injection?.(); return [{documentId:env.documentId,
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
  assert.deepEqual(f.storage.pending ?? [],[]);
  assert.equal(f.calls.filter(x => x.operation === 'page.save').length,0);
  f.env.online = false;
  assert.equal((await f.request('begin',{tabId:7,windowId:1})).error,'unavailable');
});

test('a known saved page grants no new save gesture', async () => {
  const f = await workerFixture();
  f.env.lookup = 'saved';
  const preview = await f.begin();
  assert.equal(preview.lookup,'saved');
  assert.equal(preview.nonce,null);
  assert.equal((await f.save(preview.nonce)).error,'changed');
  assert.equal(f.calls.filter(message => message.operation === 'page.save').length,0);
});

test('switching tabs during the final script read cancels dispatch', async () => {
  const f = await workerFixture();
  const {nonce} = await f.begin();
  const entered = Promise.withResolvers();
  const resume = Promise.withResolvers();
  f.env.injection = () => { entered.resolve(); return resume.promise; };
  const saving = f.save(nonce);
  await entered.promise;
  f.env.active = false;
  resume.resolve();
  assert.equal((await saving).error,'changed');
  assert.deepEqual(f.storage.pending,[]);
  assert.equal(f.calls.filter(message => message.operation === 'page.save').length,0);
});

test('returning to the original tab cannot revive an invalidated confirmation', async () => {
  const f = await workerFixture();
  const {nonce} = await f.begin();
  f.env.activate({windowId:1,tabId:8});
  f.env.activate({windowId:1,tabId:7});
  assert.equal((await f.save(nonce)).error,'changed');
  assert.deepEqual(f.storage.pending ?? [],[]);
  assert.equal(f.calls.filter(message => message.operation === 'page.save').length,0);
});

test('reason bounds count Unicode code points', () => {
  assert.equal(saveArgs(page,'😀'.repeat(280)).reason,'😀'.repeat(280));
  assert.throws(() => saveArgs(page,'😀'.repeat(281)));
});
test('malformed requests clear their new ID, deadline failures retain it', async () => {
  const f = await workerFixture();
  f.env.nativeError = 'invalid_request';
  assert.equal((await f.save((await f.begin()).nonce)).error,'invalid');
  assert.deepEqual(f.storage.pending,[]);
  f.env.nativeError = 'deadline_exceeded';
  assert.equal((await f.save((await f.begin()).nonce)).value.state,'unknown');
  assert.equal(f.storage.pending.length,1);
});

test('top-frame reader preserves astral title and selection at the native limits', async () => {
  const f = await workerFixture();
  await f.begin();
  globalThis.document = {title:'a'.repeat(299) + '😀b'};
  globalThis.location = {href:page.url};
  globalThis.window = {getSelection:() => '😀'.repeat(4096)};
  try {
    const result = f.env.readPage();
    assert.equal(result.title,'a'.repeat(299) + '😀');
    assert.equal(result.passage,'😀'.repeat(4096));
  } finally {
    delete globalThis.document;
    delete globalThis.location;
    delete globalThis.window;
  }
});

test('a lost annotation has a distinct child receipt and an intentional repeat gets a new ID', async () => {
  const f = await workerFixture();
  f.env.lookup = 'saved';
  f.env.delivery = 'lost';
  async function annotate(note) {
    const preview = await f.begin();
    return f.request('annotate', {windowId:1, nonce:preview.viewNonce, note});
  }
  assert.equal((await annotate('owner note')).value.state, 'unknown');
  const [row] = f.storage.pending;
  assert.deepEqual(Object.keys(row).sort(), ['binding','id']);
  await f.restart();
  assert.equal((await f.request('status', {id:row.id})).value.state, 'highlighted');
  assert.deepEqual(f.storage.pending, []);
  f.env.delivery = 'accepted';
  assert.equal((await annotate('second note')).value.state, 'highlighted');
  const writes = f.calls.filter(x => x.operation === 'annotation.add');
  assert.equal(writes.length, 2);
  assert.notEqual(writes[0].args.annotation_id, writes[1].args.annotation_id);
  assert.notEqual(writes[0].args.annotation_id, writes[0].operation_id);
  assert.deepEqual(writes[0].args, {url:page.url,capture_id:'fixture',quote:'quote',
    prefix:'Start ',suffix:' end',annotation_id:writes[0].args.annotation_id,note:'owner note'});
  assert.equal(f.calls.filter(x => x.operation === 'page.save').length, 0);
});

test('a disconnected configured destination stays visible and cannot issue a save', async () => {
  const f = await workerFixture();
  f.env.destination = 'Remote research Brain';
  f.env.lookupOffline = true;
  const page = await f.begin();
  assert.equal(page.destination, 'Remote research Brain');
  assert.equal(page.lookup, 'unavailable');
  assert.equal(page.nonce, null);
  assert.equal((await f.save(page.nonce)).error, 'changed');
});

test('refresh after a save never looks up a replacement document', async () => {
  const f = await workerFixture();
  await f.begin();
  f.env.documentId = 'replacement';
  const before = f.calls.length;
  const result = await f.request('begin', {tabId:7,windowId:1,
    expected:{documentId:'doc-one',binding}});
  assert.equal(result.error, 'changed');
  assert.equal(f.calls.length, before);
});

test('an import preserves only its opaque destination and job ID across restart',async () => {
  const f = await workerFixture();
  const batch = crypto.randomUUID();
  const original = chrome.runtime.sendNativeMessage;
  chrome.runtime.sendNativeMessage = async (host, message) => {
    if (message.operation.startsWith('import.')) {
      assert.equal(message.binding_id,binding);
      f.calls.push(message);
      if (message.operation === 'import.confirm')
        return {version:'browser.v1',error:{code:'outcome_unknown'}};
      return {version:'browser.v1',result:message.operation === 'import.begin' ? {batch_id:batch} : {state:'active'}};
    }
    return original(host,message);
  };
  await f.request('startImport',{binding,header:{reason:'example',digest:'fake'}});
  const confirmation = await f.request('confirmImport',{id:batch,binding,args:{digest:'fake'}});
  assert.equal(confirmation.error,'outcome_unknown');
  assert.deepEqual(f.storage.bookmarkJob,{id:batch,binding});
  await f.restart();
  assert.deepEqual((await f.request('boundImport')).value,{id:batch,binding});
  assert.equal((await f.request('statusImport',{id:batch,binding,args:{offset:0,limit:1}})).value.state,'active');
  assert.equal(f.calls.filter(c => c.operation === 'import.confirm').length,1);
  assert.equal((await f.request('stepImport',{id:batch,binding:'wrong'})).error,'changed');
  assert.equal((await f.request('stepImport',{id:batch,binding,args:{}})).value.state,'active');
  assert.equal(f.calls.filter(c => c.operation === 'import.step').length,1);
  assert.deepEqual(f.storage.bookmarkJob,{id:batch,binding});
});
test('filing needs a current saved-page gesture and has a separate receipt',async () => {
  const f = await workerFixture();
  f.env.lookup = 'saved';
  const {viewNonce} = await f.begin();
  const original = chrome.runtime.sendNativeMessage;
  chrome.runtime.sendNativeMessage = async (host,m) => {
    if (m.operation !== 'page.file') return original(host,m);
    f.calls.push(m);
    return {version:'browser.v1',result:{state:'accepted',capture_id:'fixture',space_id:m.args.space_id}};
  };
  assert.equal((await f.request('file',{windowId:1,nonce:viewNonce,spaceId:'space'})).value.state,'filed');
  assert.equal((await f.request('file',{windowId:1,nonce:viewNonce,spaceId:'space'})).error,'changed');
  assert.deepEqual(f.calls.filter(c => c.operation === 'page.file').map(c => c.args),
    [{url:page.url,capture_id:'fixture',space_id:'space'}]);
});
