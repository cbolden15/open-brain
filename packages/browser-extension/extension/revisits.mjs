// Revisit dates stay in the owner's chosen time zone as entered.
export function revisitControls(request, context, refresh) {
  const el = id => document.getElementById(id);
  let binding, next, history = [], offset = 0;
  const status = message => { el('revisit-status').textContent = message; };
  async function current() {
    const {revisit} = await request('revisitGet',context());
    el('revisit-date').value = revisit?.local_date ?? '';
    el('revisit-zone').value = revisit?.zone ?? Intl.DateTimeFormat().resolvedOptions().timeZone;
    el('revisit-dismiss').hidden = !revisit || revisit.state === 'dismissed';
    el('revisit-clear').hidden = !revisit;
    status(revisit ? (revisit.state === 'zone_unavailable' ? 'This stored time zone is unavailable. Choose a new time zone.' :
      revisit.state + ' · ' + revisit.local_date + ' · ' + revisit.zone) : 'No revisit is scheduled.');
  }
  async function mutate(action) {
    try {
      const args = action === 'set' ? {local_date:el('revisit-date').value,zone:el('revisit-zone').value} : {};
      const result = await request('revisitChange',{...context(),actionName:action,args});
      if (result.state !== 'rescheduled') {
        status('Outcome unknown. Reopen to check this change.'); return;
      }
      await refresh();
      await current();
      status(action === 'set' ? 'Revisit scheduled for ' + args.local_date + ' in ' + args.zone + '.' :
        action === 'dismiss' ? 'Revisit dismissed.' : 'Revisit cleared.');
      el('revisit-edit').open = true;
      el('revisit-date').focus();
    } catch { status('The change could not be confirmed. Reopen to check the saved date, time zone and grant.'); }
  }
  async function due() {
    const result = await request('revisitDue',{binding,offset});
    el('due-rows').replaceChildren();
    for (const row of result.revisits) {
      const item = document.createElement('li');
      item.textContent = row.local_date + ' · ' + row.zone + ' · ';
      let address;
      try { address = new URL(row.url); } catch { /* A redacted record can omit its address. */ }
      const label = row.title || row.capture_id;
      if (address?.protocol === 'https:' && !address.username && !address.password && !address.hash) {
        const link = document.createElement('a');
        link.textContent = label;
        link.href = address.href;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        item.append(link);
      } else item.append(document.createTextNode(label));
      el('due-rows').append(item);
    }
    next = result.next_offset;
    el('due-more').hidden = next == null;
    el('due-back').hidden = !history.length;
    el('due-status').textContent = result.revisits.length ? 'Due revisits loaded.' : 'No revisits are due in this scope.';
  }
  const dueError = () => { el('due-status').textContent = 'Due revisits are unavailable. Reopen to check the destination.'; };
  el('revisit-edit').addEventListener('toggle',() => {
    if (el('revisit-edit').open) current().catch(() => status('Revisit settings are unavailable.'));
  });
  el('revisit-form').addEventListener('submit',event => { event.preventDefault(); mutate('set'); });
  el('revisit-dismiss').addEventListener('click',() => mutate('dismiss'));
  el('revisit-clear').addEventListener('click',() => mutate('clear'));
  el('due-panel').addEventListener('toggle',() => {
    if (el('due-panel').open) { offset = 0; history = []; due().catch(dueError); }
  });
  el('due-more').addEventListener('click',() => {
    if (next != null) { history.push(offset); offset = next; due().catch(dueError); }
  });
  el('due-back').addEventListener('click',() => { offset = history.pop() ?? 0; due().catch(dueError); });
  return page => {
    binding = page.expected.binding;
    el('revisit-edit').hidden = !page.viewNonce;
    el('revisit-edit').open = false;
    el('due-panel').hidden = false;
    if (el('due-panel').open) due().catch(dueError);
  };
}
