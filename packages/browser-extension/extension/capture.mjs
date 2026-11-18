export function displayUrl(raw) {
  const url = new URL(raw);
  if (url.protocol !== 'https:' || url.username || url.password || raw.length > 8192)
    throw new Error('unsupported');
  url.hash = '';
  return url.href;
}

export function sameDocument(a, b) {
  return ['tabId', 'documentId', 'url', 'passage'].every(key => a[key] === b[key]);
}

export function saveArgs(page, input) {
  const reason = input.trim();
  if (!reason || reason.length > 280) throw new Error('reason');
  return {url: page.url, reason, ...(page.title.trim() && {title: page.title}),
    ...(page.passage.trim() && {passage: page.passage})};
}

export function receiptState(result) {
  if (result?.state === 'accepted' && typeof result.capture_id === 'string' && result.capture_id)
    return 'saved';
  return result?.state === 'pending' ? 'pending' : 'unknown';
}
