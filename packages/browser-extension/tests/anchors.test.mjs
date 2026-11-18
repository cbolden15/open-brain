import assert from 'node:assert/strict';
import test from 'node:test';
import '../extension/anchors.js';

const {locateQuote, selectionAnchor} = globalThis.openBrainAnchors;

test('context distinguishes repeated quotes across adjacent text nodes', () => {
  const text = 'red book / blue book / unread';
  const anchor = selectionAnchor(text, 16, 20, 5);
  assert.deepEqual(anchor, {quote: 'book', prefix: 'blue ', suffix: ' / un'});
  assert.deepEqual(locateQuote(text, anchor), {start: 16, end: 20});
});

test('ambiguous and changed anchors stay unresolved, including overlapping matches', () => {
  assert.equal(locateQuote('abc abc', {quote:'abc', prefix:'', suffix:''}), null);
  assert.equal(locateQuote('aaaa', {quote:'aaa', prefix:'', suffix:''}), null);
  assert.equal(locateQuote('new quote', {quote:'quote', prefix:'old ', suffix:''}), null);
  assert.equal(locateQuote('x', {quote:'', prefix:'', suffix:''}), null);
});

test('DOM offsets are UTF-16 while context is bounded in complete code points', () => {
  const text = '😀 cafe\u0301 🐦';
  const anchor = selectionAnchor(text, 3, 8, 2);
  assert.deepEqual(anchor, {quote:'cafe\u0301', prefix:'😀 ', suffix:' 🐦'});
  assert.deepEqual(locateQuote(text, anchor), {start:3, end:8});
  assert.equal(locateQuote(text.normalize('NFC'), anchor), null);
});
