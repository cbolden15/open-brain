const chunkSize = 48 * 1024;

export async function prepareFile(file, format, input) {
  if (!file || !file.size || file.size > 1024 * 1024) throw new Error('size');
  if (!['html','urls'].includes(format)) throw new Error('format');
  const reason = input.trim();
  if (!reason || [...reason].length > 280 || reason.includes('\0') || !reason.isWellFormed())
    throw new Error('reason');
  const bytes = new Uint8Array(await file.arrayBuffer());
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
  return {bytes, header:{format,reason,bytes:bytes.length,chunks:Math.ceil(bytes.length / chunkSize),
    digest:Array.from(digest, b => b.toString(16).padStart(2,'0')).join('')}};
}
export function* chunksOf(bytes) {
  for (let start = 0; start < bytes.length; start += chunkSize) {
    const chunk = bytes.subarray(start, start + chunkSize);
    yield btoa(String.fromCharCode(...chunk));
  }
}
