import test from 'node:test';
import assert from 'node:assert/strict';
import {prepareFile, chunksOf} from '../extension/import-file.mjs';

test('preview hashes the selected bytes and chunks split Unicode without changing bytes', async () => {
  const file = new File(['a'.repeat(49151) + '🪴'], 'bookmarks.txt');
  const upload = await prepareFile(file, 'urls', ' spring ');
  assert.deepEqual(upload.header, {bytes:49155, chunks:2, format:'urls', reason:'spring',
    digest:Buffer.from(await crypto.subtle.digest('SHA-256', await file.arrayBuffer())).toString('hex')});
  assert.deepEqual(Buffer.concat([...chunksOf(upload.bytes)].map(s => Buffer.from(s,'base64'))),
    Buffer.from(await file.arrayBuffer()));
});
test('reject oversized or empty files before reading, require a bounded reason', async () => {
  for (const size of [0,1048577]) await assert.rejects(prepareFile({size},'html','why'), /size/);
  await assert.rejects(prepareFile(new File(['x'],'x'),'other','why'), /format/);
  await assert.rejects(prepareFile(new File(['x'],'x'),'urls',''), /reason/);
});
