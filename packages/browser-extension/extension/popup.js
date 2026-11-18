const el = id => document.getElementById(id);
const descriptions = {
  changed:'The page or selection changed. Reopen the extension to confirm it again.',
  unsupported:'Open an HTTPS page to save it.', restricted:'Chrome could not read this page.',
  selection:'Choose a passage of 4,096 characters or fewer.',
  reason:'Enter a reason of up to 280 characters.', unavailable:'The Open Brain helper is unavailable.',
  refused:'The helper refused this request. Check your local grant.',
  full:'Resolve or abandon an earlier save before starting another.',
  saved:'Saved to Open Brain.', pending:'Pending. The helper has received this save.',
  unknown:'Outcome unknown. Check status before starting another save.',
  cleared:'This save has already been resolved.'
};
let gesture;
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
      if (state === 'saved' || state === 'cleared') continue;
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
async function open() {
  const [tab] = await chrome.tabs.query({active:true,currentWindow:true});
  // Start the snapshot before status reconciliation to bind the opening gesture.
  const preview = request('begin',{tabId:tab.id, windowId:tab.windowId});
  preview.catch(() => {});
  await listPending(true);
  const page = await preview;
  gesture = {windowId:tab.windowId, nonce:page.nonce};
  for (const key of ['title','url','passage','destination']) el(key).textContent = page[key];
  if (el('status').textContent === 'Checking this page…')
    el('status').textContent = page.lookup === 'saved' ? 'Already saved in this source.' : 'No saved page in this scope.';
  el('preview').hidden = false;
}
document.querySelector('form').addEventListener('submit',async event => {
  event.preventDefault();
  el('save').disabled = true;
  try {
    const {state} = await request('save',{...gesture, reason:el('reason').value});
    status(state);
    await listPending(false);
  } catch (error) {
    status(error.message);
    if (error.message === 'reason') el('save').disabled = false;
  }
});
open().catch(error => status(error.message));
