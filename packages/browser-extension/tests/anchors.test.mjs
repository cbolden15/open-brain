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
  assert.deepEqual(locateQuote(text.normalize('NFC'), anchor), {start:3, end:7});
});

test('canonical Unicode maps back to original DOM offsets and identical normalized candidates stay ambiguous', () => {
  const {locateQuote} = globalThis.openBrainAnchors;
  const anchor = {quote:'café',prefix:'(',suffix:')'};
  assert.deepEqual(locateQuote('🍏 (cafe\u0301)',anchor),{start:4,end:9});
  assert.equal(locateQuote('(café) or (cafe\u0301)',anchor),null);
  assert.deepEqual(locateQuote('(café)',{...anchor,quote:'cafe\u0301'}),{start:1,end:5});
});

test('rejected sibling nodes count toward the foreground traversal limit', () => {
  let visited = 0;
  globalThis.NodeFilter = {SHOW_ELEMENT:1, SHOW_TEXT:4, FILTER_REJECT:2, FILTER_ACCEPT:1};
  globalThis.Node = {ELEMENT_NODE:1,TEXT_NODE:3};
  globalThis.CSS = {highlights:new Map()};
  globalThis.document = {body:{},createTreeWalker(_root,_flags,filter) {
    return {nextNode() {
      while (++visited <= 65000)
        filter.acceptNode({nodeType:1,matches:() => true});
      return null;
    }};
  }};
  assert.deepEqual(globalThis.openBrainAnchors.restore([{quote:'example',prefix:'',suffix:''}]),[false]);
  assert.equal(visited,60001);
});
