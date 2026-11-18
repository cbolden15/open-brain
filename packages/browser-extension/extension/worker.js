import {displayUrl, sameDocument, saveArgs, receiptState} from './capture.mjs';

const popup = chrome.runtime.getURL('popup.html');
const gestures = new Map();
const menus = new Map();
const maxPending = 16;
const storageReady = chrome.storage.local.setAccessLevel({accessLevel: 'TRUSTED_CONTEXTS'});
let tail = Promise.resolve();
function serialize(task) {
  const result = tail.then(task);
  tail = result.catch(() => {});
  return result;
}

async function sendNative(operation, args, binding, operationId) {
  const message = {version:'browser.v1', operation, args};
  if (binding) message.binding_id = binding;
  if (operationId) message.operation_id = operationId;
  let reply;
  try { reply = await chrome.runtime.sendNativeMessage('com.open_brain.saved_items', message); }
  catch { throw new Error('unavailable'); }
  if (reply?.version !== 'browser.v1' || !reply.result) throw new Error('refused');
  return reply.result;
}

async function snapshotTab(tabId, windowId) {
  const tab = await chrome.tabs.get(tabId);
  if (!tab.active || tab.windowId !== windowId || !(await chrome.windows.get(windowId)).focused)
    throw new Error('changed');
  displayUrl(tab.url || '');
  const [frame] = await chrome.scripting.executeScript({
    target:{tabId, frameIds:[0]},
    func: () => ({url: location.href, title: document.title.slice(0,300),
      passage: String(window.getSelection() || '').slice(0,4097)})
  });
  if (!frame?.documentId || frame.result?.url !== tab.url) throw new Error('changed');
  if (frame.result.passage.length > 4096) throw new Error('selection');
  return Object.freeze({...frame.result, tabId, windowId, documentId: frame.documentId});
}

async function validateBeforeDispatch(snapshot) {
  const current = await snapshotTab(snapshot.tabId, snapshot.windowId);
  if (!sameDocument(current, snapshot)) throw new Error('changed');
}

async function begin(tabId, windowId) {
  const menu = menus.get(windowId);
  menus.delete(windowId);
  let page;
  if (menu) {
    if (menu.tabId !== tabId || Date.now() > menu.expires) throw new Error('changed');
    page = await menu.snapshot;
    await validateBeforeDispatch(page);
  } else page = await snapshotTab(tabId, windowId);
  const binding = await sendNative('binding.describe', {});
  if (!/^[0-9a-f]{64}$/.test(binding.binding_id) || binding.destination !== 'This computer')
    throw new Error('refused');
  const lookup = await sendNative('page.lookup', {url: page.url}, binding.binding_id);
  if (!['absent','saved'].includes(lookup.state)) throw new Error('refused');
  const nonce = crypto.randomUUID();
  gestures.set(windowId, {page, binding:binding.binding_id, nonce, expires:Date.now()+300000});
  return {url:displayUrl(page.url), title:page.title, passage:page.passage, lookup:lookup.state,
    destination:binding.destination, nonce};
}

async function ledger() {
  await storageReady;
  return (await chrome.storage.local.get('pending')).pending ?? [];
}
async function forget(id) {
  await chrome.storage.local.set({pending:(await ledger()).filter(row => row.id !== id)});
}
async function save({windowId, nonce, reason}) {
  const gesture = gestures.get(windowId);
  if (!gesture || gesture.nonce !== nonce || Date.now() > gesture.expires)
    throw new Error('changed');
  const args = saveArgs(gesture.page, reason);
  gestures.delete(windowId);
  const pending = await ledger();
  if (pending.length >= maxPending) throw new Error('full');
  const row = {id:crypto.randomUUID(), binding:gesture.binding};
  await chrome.storage.local.set({pending:[...pending, row]});
  try { await validateBeforeDispatch(gesture.page); }
  catch { await forget(row.id); throw new Error('changed'); }
  const result = await sendNative('page.save', args, row.binding, row.id).catch(() => null);
  const state = receiptState(result);
  if (state === 'saved') await forget(row.id);
  return {state:result?.state === 'refused' ? 'refused' : state};
}
async function reconcile(id) {
  const row = (await ledger()).find(entry => entry.id === id);
  if (!row) return {state:'cleared'};
  const result = await sendNative('operation.status', {}, row.binding, row.id).catch(() => null);
  const state = receiptState(result);
  if (state === 'saved') await forget(id);
  return {state};
}
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id || sender.url !== popup) return;
  async function handle() {
    switch (message.action) {
      case 'begin': return begin(message.tabId, message.windowId);
      case 'save': return serialize(() => save(message));
      case 'list': return {ids:(await ledger()).map(row => row.id)};
      case 'status': return serialize(() => reconcile(message.id));
      case 'abandon': return serialize(() => forget(message.id));
      default: throw new Error('refused');
    }
  }
  handle().then(value => reply({value}), error => reply({error:
    ['changed','unsupported','reason','unavailable','refused','full','selection'].includes(error.message)
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
  const menu = {tabId:tab.id, snapshot, expires:Date.now()+10000};
  menus.set(tab.windowId, menu);
  chrome.action.openPopup({windowId:tab.windowId}).catch(() => {
    if (menus.get(tab.windowId) === menu) menus.delete(tab.windowId);
  });
});
