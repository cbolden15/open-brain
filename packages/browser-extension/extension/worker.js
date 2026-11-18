import {displayUrl, sameDocument, saveArgs, receiptState} from './capture.mjs';

const popup = chrome.runtime.getURL('popup.html');
const gestures = new Map();
const menus = new Map();
const activeIntents = new Map();
function startIntent(tabId, windowId) {
  const intent = {tabId, windowId};
  activeIntents.set(windowId, intent);
  return intent;
}
function requireIntent(intent) {
  if (activeIntents.get(intent.windowId) !== intent) throw new Error('changed');
}
chrome.tabs.onActivated.addListener(({windowId, tabId}) => {
  if (activeIntents.get(windowId)?.tabId !== tabId) activeIntents.delete(windowId);
});
chrome.tabs.onUpdated.addListener((tabId, change, tab) => {
  if ((change.status === 'loading' || change.url) && activeIntents.get(tab.windowId)?.tabId === tabId)
    activeIntents.delete(tab.windowId);
});
chrome.windows.onRemoved.addListener(windowId => {
  activeIntents.delete(windowId);
  gestures.delete(windowId);
  menus.delete(windowId);
});
const maxPending = 16;
const storageReady = chrome.storage.local.setAccessLevel({accessLevel: 'TRUSTED_CONTEXTS'});
let tail = Promise.resolve();
function serialize(task) {
  const result = tail.then(task);
  tail = result.catch(() => {});
  return result;
}

// Only these native errors are known to precede admission for a fresh save.
const preAdmissionErrors = new Map([
  ...['invalid_request','invalid_frame','frame_too_large','invalid_url','url_secret','invalid_why',
    'invalid_field','file_too_large','payload_too_large'].map(code => [code,'invalid']),
  ...['binding_changed','operation_denied','origin_denied','unsupported_version'].map(code => [code,'refused'])
]);
async function sendNative(operation, args, binding, operationId) {
  const message = {version:'browser.v1', operation, args};
  if (binding) message.binding_id = binding;
  if (operationId) message.operation_id = operationId;
  let reply;
  try { reply = await chrome.runtime.sendNativeMessage('com.open_brain.saved_items', message); }
  catch { throw new Error('unavailable'); }
  if (reply?.version !== 'browser.v1') throw new Error('unavailable');
  if (!reply.result) {
    const code = reply.error?.code;
    const known = preAdmissionErrors.get(code);
    const error = new Error(known ?? (code === 'browser_unbound' ? 'refused' : 'unavailable'));
    error.beforeAdmission = Boolean(known);
    throw error;
  }
  return reply.result;
}

async function snapshotTab(tabId, windowId) {
  const tab = await chrome.tabs.get(tabId);
  if (!tab.active || tab.windowId !== windowId || !(await chrome.windows.get(windowId)).focused)
    throw new Error('changed');
  displayUrl(tab.url || '');
  const [frame] = await chrome.scripting.executeScript({
    target:{tabId, frameIds:[0]},
    func: () => {
      function prefix(value, limit) {
        let text = '';
        let count = 0;
        for (const character of value) {
          if (++count > limit) break;
          text += character;
        }
        return text.toWellFormed();
      }
      return {url:location.href, title:prefix(document.title,300),
        passage:prefix(String(window.getSelection() || ''),4097)};
    }
  });
  if (!frame?.documentId || frame.result?.url !== tab.url) throw new Error('changed');
  if ([...frame.result.passage].length > 4096) throw new Error('selection');
  return Object.freeze({...frame.result, tabId, windowId, documentId: frame.documentId});
}

async function validateBeforeDispatch(snapshot) {
  const current = await snapshotTab(snapshot.tabId, snapshot.windowId);
  if (!sameDocument(current, snapshot)) throw new Error('changed');
  const [[active], window] = await Promise.all([
    chrome.tabs.query({active:true, windowId:snapshot.windowId}), chrome.windows.get(snapshot.windowId)
  ]);
  if (!window.focused || active?.id !== snapshot.tabId || active.url !== snapshot.url)
    throw new Error('changed');
}

async function begin(tabId, windowId, expected) {
  const menu = menus.get(windowId);
  menus.delete(windowId);
  const intent = menu?.intent ?? startIntent(tabId, windowId);
  requireIntent(intent);
  let page;
  if (menu) {
    if (menu.tabId !== tabId || Date.now() > menu.expires) throw new Error('changed');
    page = await menu.snapshot;
    await validateBeforeDispatch(page);
  } else page = await snapshotTab(tabId, windowId);
  if (expected && expected.documentId !== page.documentId) throw new Error('changed');
  const binding = await sendNative('binding.describe', {});
  if (expected && expected.binding !== binding.binding_id) throw new Error('changed');
  if (!/^[0-9a-f]{64}$/.test(binding.binding_id) || typeof binding.destination !== 'string' ||
      !binding.destination.trim() || binding.destination.length > 120 ||
      /[\u0000-\u001f\u007f]/u.test(binding.destination)) throw new Error('refused');
  let lookup;
  try { lookup = await sendNative('page.lookup', {url:page.url}, binding.binding_id); }
  catch (error) {
    if (error.message !== 'unavailable') throw error;
    lookup = {state:'unavailable'};
  }
  if (!['absent','saved','unavailable'].includes(lookup.state)) throw new Error('refused');
  requireIntent(intent);
  const nonce = lookup.state === 'absent' ? crypto.randomUUID() : null;
  const viewNonce = lookup.state === 'saved' && lookup.capture_id ? crypto.randomUUID() : null;
  if (nonce || viewNonce) {
    gestures.set(windowId, {page, intent, binding:binding.binding_id, nonce:nonce ?? viewNonce,
      kind:nonce ? 'save' : 'view', captureId:lookup.capture_id, expires:Date.now()+300000});
  } else gestures.delete(windowId);
  return {url:displayUrl(page.url), title:page.title, passage:page.passage, lookup:lookup.state,
    destination:binding.destination, nonce, viewNonce,
    expected:{documentId:page.documentId, binding:binding.binding_id}};
}

async function ledger() {
  await storageReady;
  return (await chrome.storage.local.get('pending')).pending ?? [];
}
async function forget(id) {
  await chrome.storage.local.set({pending:(await ledger()).filter(row => row.id !== id)});
}
function currentGesture(windowId, nonce, kind) {
  const gesture = gestures.get(windowId);
  if (!gesture || gesture.nonce !== nonce || gesture.kind !== kind || Date.now() > gesture.expires)
    throw new Error('changed');
  requireIntent(gesture.intent);
  return gesture;
}

async function mutation(gesture, operation, args) {
  const pending = await ledger();
  if (pending.length >= maxPending) throw new Error('full');
  const row = {id:crypto.randomUUID(), binding:gesture.binding};
  await chrome.storage.local.set({pending:[...pending, row]});
  try { await validateBeforeDispatch(gesture.page); requireIntent(gesture.intent); }
  catch { await forget(row.id); throw new Error('changed'); }
  let result;
  try { result = await sendNative(operation, args, row.binding, row.id); }
  catch (error) {
    if (error.beforeAdmission) {
      await forget(row.id);
      throw error;
    }
  }
  const state = receiptState(result);
  if (state === 'saved' || state === 'highlighted') await forget(row.id);
  return {state:result?.state === 'refused' ? 'refused' : state};
}

async function save({windowId, nonce, reason}) {
  const gesture = currentGesture(windowId, nonce, 'save');
  const args = saveArgs(gesture.page, reason);
  gestures.delete(windowId);
  return mutation(gesture, 'page.save', args);
}

function readAnchor() {
  const quote = String(window.getSelection() || '');
  if (!quote || [...quote].length > 4096) return null;
  return {url:location.href, ...globalThis.openBrainAnchors.capture(quote)};
}

async function annotate({windowId, nonce, note = ''}) {
  const gesture = currentGesture(windowId, nonce, 'view');
  if (!gesture.page.passage.trim() || typeof note !== 'string' || !note.isWellFormed() ||
      [...note].length > 1024 || note.includes('\0')) throw new Error('invalid');
  const target = {tabId:gesture.page.tabId, documentIds:[gesture.page.documentId]};
  await chrome.scripting.executeScript({target, files:['anchors.js']});
  const [frame] = await chrome.scripting.executeScript({target, func:readAnchor});
  const anchor = frame?.result;
  if (frame?.documentId !== gesture.page.documentId || anchor?.url !== gesture.page.url ||
      anchor?.quote !== gesture.page.passage) throw new Error('changed');
  gestures.delete(windowId);
  return mutation(gesture, 'annotation.add', {url:gesture.page.url,capture_id:gesture.captureId,
    annotation_id:crypto.randomUUID(),quote:anchor.quote,prefix:anchor.prefix,suffix:anchor.suffix,
    ...(note.trim() && {note:note.trim()})});
}

async function highlights({windowId, nonce, offset = 0}, paint) {
  const gesture = currentGesture(windowId, nonce, 'view');
  if (!Number.isSafeInteger(offset) || offset < 0) throw new Error('invalid');
  const rows = await sendNative('annotation.list', {url:gesture.page.url,
    capture_id:gesture.captureId,limit:10,offset}, gesture.binding);
  if (!Array.isArray(rows.annotations) || rows.annotations.length > 10) throw new Error('refused');
  await validateBeforeDispatch(gesture.page);
  requireIntent(gesture.intent);
  if (!paint) return rows;
  const target = {tabId:gesture.page.tabId, documentIds:[gesture.page.documentId]};
  // Only anchor text reaches the isolated document. Owner notes stay in the popup.
  const anchors = rows.annotations.map(({quote,prefix,suffix}) => ({quote,prefix,suffix}));
  await chrome.scripting.executeScript({target, files:['anchors.js']});
  await chrome.scripting.insertCSS({target, css:
    '::highlight(open-brain-saved-passages) { background-color: #ffe589; color: #292315; }'});
  requireIntent(gesture.intent);
  const [frame] = await chrome.scripting.executeScript({target,
    func:(items) => globalThis.openBrainAnchors.restore(items), args:[anchors]});
  if (frame?.documentId !== gesture.page.documentId) throw new Error('changed');
  return {...rows, resolved:frame.result};
}
async function reconcile(id) {
  const row = (await ledger()).find(entry => entry.id === id);
  if (!row) return {state:'cleared'};
  const result = await sendNative('operation.status', {}, row.binding, row.id).catch(() => null);
  const state = receiptState(result);
  if (state === 'saved' || state === 'highlighted') await forget(id);
  return {state};
}
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id || sender.url !== popup) return;
  async function handle() {
    switch (message.action) {
      case 'begin': return begin(message.tabId, message.windowId, message.expected);
      case 'save': return serialize(() => save(message));
      case 'annotate': return serialize(() => annotate(message));
      case 'highlights': return highlights(message, false);
      case 'restore': return highlights(message, true);
      case 'list': return {ids:(await ledger()).map(row => row.id)};
      case 'status': return serialize(() => reconcile(message.id));
      case 'abandon': return serialize(() => forget(message.id));
      default: throw new Error('refused');
    }
  }
  handle().then(value => reply({value}), error => reply({error:
    ['changed','unsupported','reason','unavailable','refused','full','selection','invalid'].includes(error.message)
      ? error.message : 'restricted'}));
  return true;
});
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({id:'save-selection', title:'Save selection to Open Brain',
    contexts:['selection'], documentUrlPatterns:['https://*/*']});
});
chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId !== 'save-selection' || (info.frameId ?? 0) !== 0 || !tab?.active) return;
  const snapshot = snapshotTab(tab.id, tab.windowId).then(page => {
    if (page.url !== info.pageUrl || page.passage !== info.selectionText) throw new Error('changed');
    return page;
  });
  snapshot.catch(() => {});
  const menu = {tabId:tab.id, intent:startIntent(tab.id, tab.windowId), snapshot, expires:Date.now()+10000};
  menus.set(tab.windowId, menu);
  chrome.action.openPopup({windowId:tab.windowId}).catch(() => {
    if (menus.get(tab.windowId) === menu) menus.delete(tab.windowId);
  });
});
