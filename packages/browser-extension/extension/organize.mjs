import {prepareFile, chunksOf} from './import-file.mjs';

export function organizer(request, context, refresh) {
  const el = id => document.getElementById(id);
  const announce = text => { el('import-status').textContent = text; };
  let binding, job, digest, state, next, offset, history = [], busy = false;
  let nextSpace = null;
  const describeError = error => ({
    size:'Choose a file from 1 byte to 1 MiB. Split larger lists into text files with at most 1,000 URLs each and reuse the same reason.',
    reason:'Enter a reason of up to 280 characters.',
    import_preview_limit:'Four uploads or imports are already open. Finish one or wait for an unconfirmed preview to expire.',
    import_invalid_encoding:'Choose a UTF-8 bookmark HTML or URL-list file.',
    import_digest_mismatch:'The uploaded bytes did not match the selected file. Cancel and choose the file again.',
    import_too_large:'Split the list into files with at most 1,000 URLs and 1 MiB each. Use the same batch reason.',
    unavailable:'The helper is unavailable. Reopen to check this import.',
    refused:'This grant cannot perform the import.',
    changed:'The destination changed. Reopen with the original grant.'
  })[error.message] ?? 'The helper could not complete this import. Check its status before trying again.';

  const call = (action, args = {}) => request(action,{id:job.id, binding:job.binding,args});
  function mode(value) {
    state = value;
    el('import-form').hidden = Boolean(job);
    el('import-job').hidden = !job;
    el('import-confirm').hidden = state !== 'preview' || !digest;
    el('import-resume').hidden = state !== 'active';
    const terminal = ['complete','canceled','expired'].includes(state);
    el('import-done').hidden = !terminal;
    el('import-cancel').hidden = terminal;
  }
  function showRows(response) {
    const list = el('import-rows');
    list.replaceChildren();
    for (const row of response.rows) {
      const item = document.createElement('li');
      item.textContent = [row.url,row.state].filter(Boolean).join(' · ');
      list.append(item);
    }
    list.start = offset + 1;
    next = response.next_offset;
    el('import-previous').hidden = !history.length;
    el('import-next').hidden = next == null;
  }
  async function preview() {
    const result = await call('previewImport',{offset,limit:20});
    digest = result.digest;
    mode('preview');
    showRows(result);
    announce(result.total + ' rows in this preview. Confirm to import them.');
  }
  async function report() {
    let result;
    try { result = await call('statusImport',{offset,limit:20}); }
    catch (error) {
      if (error.message !== 'import_not_found') throw error;
      mode('expired');
      showRows({rows:[],next_offset:null});
      announce('This import is no longer staged. Close the results to choose another file.');
      return;
    }
    if (['ready','preview'].includes(result.state)) return preview();
    mode(result.state);
    showRows(result);
    announce(result.state === 'upload' ? 'The upload was interrupted. Cancel to select the file again.' :
      result.state + ' · ' + result.processed + ' of ' + result.total + ' rows processed.');
  }
  async function run() {
    let progress;
    do {
      progress = await call('stepImport');
      mode(progress.state);
      announce(progress.processed + ' of ' + progress.total + ' rows processed.');
      // Closing the popup stops requesting work. The canonical job can resume later.
    } while (progress.state === 'active');
    await report();
    el('import-done').focus();
  }
  function handle(id, type, action) {
    el(id).addEventListener(type, async event => {
      event.preventDefault();
      if (busy) return;
      busy = true;
      el('import-panel').setAttribute('aria-busy','true');
      try { await action(); }
      catch (error) { announce(describeError(error)); }
      finally {
        busy = false;
        el('import-panel').removeAttribute('aria-busy');
      }
    });
  }
  function resetPage() { offset = 0; history = []; next = null; }
  handle('import-form','submit',async () => {
    const input = el('bookmarks');
    const upload = await prepareFile(input.files[0], el('bookmark-format').value, el('import-reason').value);
    const result = await request('startImport',{binding,header:upload.header});
    job = {id:result.batch_id,binding};
    resetPage();
    digest = null;
    mode('uploading');
    announce('Uploading bookmarks for preview…');
    let index = 0;
    for (const data of chunksOf(upload.bytes)) await call('chunkImport',{index:index++,data});
    input.value = '';
    await preview();
    el('import-confirm').focus();
  });
  handle('import-confirm','click',async () => {
    if (state !== 'preview' || !digest) return;
    await call('confirmImport',{digest});
    resetPage();
    mode('active');
    await run();
  });
  handle('import-resume','click',run);
  handle('import-cancel','click',async () => {
    const result = await call('cancelImport');
    mode(result.state);
    await report();
    el('import-done').focus();
  });
  handle('import-done','click',async () => {
    await call('forgetImport');
    job = null;
    digest = null;
    resetPage();
    showRows({rows:[],next_offset:null});
    mode('empty');
    announce('Choose a bookmark file to start an import.');
    el('bookmarks').focus();
  });
  handle('import-next','click',async () => {
    if (next == null) return;
    history.push(offset);
    offset = next;
    await (state === 'preview' ? preview() : report());
  });
  handle('import-previous','click',async () => {
    offset = history.pop() ?? 0;
    await (state === 'preview' ? preview() : report());
  });

  async function listSpaces(start) {
    const result = await request('spaces',{...context(),offset:start});
    const select = el('space');
    if (!start) select.replaceChildren();
    for (const space of result.spaces) {
      select.add(new Option(space.name,space.space_id));
    }
    el('current-space').textContent = result.current_space ? 'Filed in ' + result.current_space.name :
      'No visible filing space.';
    if (result.current_space) select.value = result.current_space.space_id;
    nextSpace = result.next_offset;
    el('more-spaces').hidden = nextSpace == null;
    el('space-form').hidden = false;
    el('file-page').disabled = !select.options.length;
    if (!start && select.options.length) select.focus();
  }
  const filingFailure = () => { el('status').textContent = 'The page is saved. Reopen to retry filing under the current grant.'; };
  el('show-spaces').addEventListener('click', () => listSpaces(0).catch(filingFailure));
  el('more-spaces').addEventListener('click', () => {
    if (nextSpace != null) listSpaces(nextSpace).catch(filingFailure);
  });
  el('space-form').addEventListener('submit',async event => {
    event.preventDefault();
    el('file-page').disabled = true;
    try {
      const result = await request('file',{...context(),spaceId:el('space').value});
      if (result.state === 'filed' || result.state === 'saved') {
        await refresh();
        await listSpaces(0);
        el('status').textContent = 'Page filed.';
      } else el('status').textContent = 'Page is saved. Filing outcome is unknown. Check its status on reopening.';
    } catch { filingFailure(); }
  });
  return async page => {
    binding = page.expected.binding;
    el('filing').hidden = !page.viewNonce;
    el('space-form').hidden = true;
    el('current-space').textContent = '';
    el('import-panel').hidden = false;
    if (busy) return;
    job = await request('boundImport');
    resetPage();
    digest = null;
    mode(job ? 'loading' : 'empty');
    if (job) {
      if (binding !== job.binding) { el('import-cancel').hidden = true; announce('Reconnect the original destination to manage this import.'); return; }
      await report().catch(error => announce(describeError(error)));
    }
  };
}
