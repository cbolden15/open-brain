import {organizer} from './organize.mjs';

const el = id => document.getElementById(id);
const descriptions = {
  changed:'The page or selection changed. Reopen the extension to confirm it again.',
  unsupported:'Open an HTTPS page to save it.', restricted:'Chrome could not read this page.',
  selection:'Choose a passage of 4,096 characters or fewer.',
  invalid:'The helper rejected the page details. Check the URL and text before trying again.',
  reason:'Enter a reason of up to 280 characters.', unavailable:'The Open Brain helper is unavailable.',
  refused:'The helper refused this request. Check your local grant.',
  full:'Resolve or abandon an earlier save before starting another.',
  saved:'Page is saved. Repeat saves keep the original reason.', pending:'Pending. The helper has received this save.',
  unknown:'Outcome unknown. Check status before starting another save.',
  filed:'Page filed.', highlighted:'Highlight saved.', cleared:'This save has already been resolved.'
};
let gesture;
let currentOffset = 0;
let nextOffset = null;
let previousOffsets = [];
async function request(action, fields = {}) {
  const reply = await chrome.runtime.sendMessage({action, ...fields});
  if (reply.error) throw new Error(reply.error);
  return reply.value;
}
function status(code) { el('status').textContent = descriptions[code] ?? descriptions.unavailable; }
function button(label, name, fn) {
  const node = document.createElement('button');
  node.type = 'button'; node.className = name; node.textContent = label;
  node.addEventListener('click', async () => {
    node.disabled = true;
    try { await fn(); } catch (error) { status(error.message); }
    finally { node.disabled = false; }
  });
  return node;
}
async function listPending(check) {
  const {ids} = await request('list');
  const list = el('pending').querySelector('ul');
  list.replaceChildren();
  for (const id of ids) {
    if (check) {
      const {state} = await request('status', {id});
      status(state);
      if (['saved', 'highlighted', 'filed', 'cleared'].includes(state)) continue;
    }
    const item = document.createElement('li');
    const name = document.createElement('span');
    name.textContent = 'Save ' + id.slice(0,8) + ' ';
    item.append(name, button('Check status','retry',async () => {
      status((await request('status',{id})).state);
      await listPending(false);
    }), button('Abandon','abandon',async () => {
      if (!confirm('Forget this attempt? It may already be saved.')) return;
      await request('abandon',{id});
      await listPending(false);
      el('status').textContent = 'Attempt forgotten. Reopen the extension to start a new save.';
    }));
    list.append(item);
  }
  el('pending').hidden = !list.children.length;
}
const organization = organizer(request, () => ({windowId:gesture.windowId,nonce:gesture.viewNonce}), () => open(false));
async function open(checkPending = true) {
  const tab = !checkPending && gesture ? {id:gesture.tabId, windowId:gesture.windowId} :
    (await chrome.tabs.query({active:true,currentWindow:true}))[0];
  // Start the snapshot before status reconciliation to bind the opening gesture.
  const preview = request('begin',{tabId:tab.id, windowId:tab.windowId,
    expected:checkPending ? undefined : gesture?.expected});
  preview.catch(() => {});
  await listPending(checkPending);
  const page = await preview;
  gesture = {tabId:tab.id, windowId:tab.windowId, expected:page.expected,
    nonce:page.nonce, viewNonce:page.viewNonce};
  for (const key of ['title','url','passage','destination']) el(key).textContent = page[key];
  el('page-form').hidden = page.lookup !== 'absent';
  el('highlight-form').hidden = !page.viewNonce || !page.passage.trim();
  el('highlights').hidden = !page.viewNonce;
  el('highlight-list').hidden = true;
  currentOffset = 0;
  nextOffset = null;
  previousOffsets = [];
  if (page.lookup === 'unavailable') {
    el('status').textContent = 'Cannot reach ' + page.destination + '. Reopen to retry.';
  }
  el('existing').hidden = page.lookup !== 'saved';
  if (el('status').textContent === 'Checking this page…')
    el('status').textContent = page.lookup === 'saved' ? 'Already saved in this source.' : 'No saved page in this scope.';
  el('preview').hidden = false;
  await organization(page);
}
el('page-form').addEventListener('submit',async event => {
  event.preventDefault();
  el('save').disabled = true;
  try {
    const {state} = await request('save',{...gesture, reason:el('reason').value});
    status(state);
    if (state === 'saved') {
      await open(false).catch(() => { el('preview').hidden = true; });
      if (!el('preview').hidden && !el('highlight-form').hidden) el('add-highlight').focus();
    } else await listPending(false);
  } catch (error) {
    status(error.message);
    if (error.message === 'reason') el('save').disabled = false;
  }
});
el('highlight-form').addEventListener('submit', async event => {
  event.preventDefault();
  el('add-highlight').disabled = true;
  try {
    const {state} = await request('annotate', {windowId:gesture.windowId,
      nonce:gesture.viewNonce, note:el('note').value});
    status(state);
    if (state === 'highlighted') {
      el('note').value = '';
      await open(false).catch(() => { el('preview').hidden = true; });
      if (!el('preview').hidden) el('show-highlights').focus();
    } else await listPending(false);
  } catch (error) { status(error.message); }
  finally { el('add-highlight').disabled = false; }
});

async function showHighlights(offset, paint = false) {
  const response = await request(paint ? 'restore' : 'highlights',
    {windowId:gesture.windowId, nonce:gesture.viewNonce, offset});
  const list = el('quotes');
  list.replaceChildren();
  for (const [i, annotation] of response.annotations.entries()) {
    const item = document.createElement('li');
    const quote = document.createElement('blockquote');
    quote.textContent = annotation.quote;
    item.append(quote);
    if (annotation.note) {
      const note = document.createElement('p');
      note.textContent = annotation.note;
      item.append(note);
    }
    if (paint && !response.resolved[i]) {
      const detail = document.createElement('p');
      detail.textContent = 'Could not locate this passage uniquely.';
      item.append(detail);
    }
    list.append(item);
  }
  currentOffset = offset;
  nextOffset = response.next_offset;
  el('highlight-list').hidden = false;
  el('restore-highlights').hidden = !response.annotations.length;
  el('previous-highlights').hidden = !previousOffsets.length;
  el('next-highlights').hidden = nextOffset == null;
  if (paint) {
    el('status').textContent = 'Located ' + response.resolved.filter(Boolean).length +
      ' of ' + response.annotations.length + ' passages on this page.';
  } else {
    el('status').textContent = response.annotations.length ? 'Saved highlights loaded.' :
      'This page has no saved highlights.';
  }
}
el('show-highlights').addEventListener('click', () => {
  previousOffsets = [];
  showHighlights(0).catch(error => status(error.message));
});
el('restore-highlights').addEventListener('click', () => {
  showHighlights(currentOffset, true).catch(error => status(error.message));
});
el('next-highlights').addEventListener('click', () => {
  if (nextOffset == null) return;
  previousOffsets.push(currentOffset);
  showHighlights(nextOffset).catch(error => status(error.message));
});
el('previous-highlights').addEventListener('click', () => {
  if (previousOffsets.length) showHighlights(previousOffsets.pop()).catch(error => status(error.message));
});
open().catch(error => status(error.message));
