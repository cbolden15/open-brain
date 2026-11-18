// Chrome loads this file in the extension's isolated world for the top document.
(() => {
  const highlightName = 'open-brain-saved-passages';

  function locateQuote(text, {quote, prefix, suffix}) {
    if (!quote || typeof prefix !== 'string' || typeof suffix !== 'string') return null;
    let found = null;
    let position = text.indexOf(quote);
    while (position !== -1) {
      const end = position + quote.length;
      if (text.slice(Math.max(0, position - prefix.length), position) === prefix &&
          text.startsWith(suffix, end)) {
        if (found) return null;
        found = {start: position, end};
      }
      position = text.indexOf(quote, position + 1);
    }
    return found;
  }

  function selectionAnchor(text, start, end, limit = 128) {
    // Slice a bounded UTF-16 window first. Keep whole code points at each end.
    const left = text.slice(Math.max(0, start - limit * 2 - 2), start);
    const right = text.slice(end, end + limit * 2 + 2);
    return {
      quote: text.slice(start, end),
      prefix: [...left].slice(-limit).join('').toWellFormed(),
      suffix: [...right].slice(0, limit).join('').toWellFormed(),
    };
  }

  function textIndex() {
    const root = document.body;
    if (!root) return null;
    const nodes = [];
    const chunks = [];
    let length = 0;
    let visited = 0;
    const excluded = 'script,style,noscript,template,input,textarea,select,[hidden],[inert],' +
                     '[aria-hidden="true"],[contenteditable]:not([contenteditable="false"])';
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, {
      acceptNode(node) {
        if (node.nodeType === Node.ELEMENT_NODE && node.matches(excluded)) {
          return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (++visited > 60000) return null;
      if (node.nodeType !== Node.TEXT_NODE || !node.length) continue;
      const start = length;
      length += node.length;
      if (length > 1024 * 1024) return null;
      nodes.push({node, start, end: length});
      chunks.push(node.data);
    }
    return {text: chunks.join(''), nodes};
  }

  function capture(quote) {
    const fallback = {quote, prefix: '', suffix: ''};
    if (!quote) return fallback;
    const selection = window.getSelection();
    if (selection?.rangeCount !== 1) return fallback;
    const range = selection.getRangeAt(0);
    const index = textIndex();
    if (!index || range.collapsed || !document.body.contains(range.commonAncestorContainer)) {
      return fallback;
    }
    let start;
    let end;
    for (const part of index.nodes) {
      if (!range.intersectsNode(part.node)) continue;
      if (start === undefined) {
        start = part.start + (part.node === range.startContainer ? range.startOffset : 0);
      }
      end = part.start + (part.node === range.endContainer ? range.endOffset : part.node.length);
    }
    if (start === undefined || index.text.slice(start, end) !== quote) return fallback;
    return selectionAnchor(index.text, start, end);
  }

  function restore(anchors) {
    CSS.highlights.delete(highlightName);
    const index = textIndex();
    const ranges = [];
    const resolved = anchors.map(anchor => {
      const position = index && locateQuote(index.text, anchor);
      if (!position) return false;
      const first = index.nodes.find(part => part.end > position.start);
      const last = index.nodes.find(part => part.end >= position.end);
      if (!first?.node.isConnected || !last?.node.isConnected) return false;
      const range = new Range();
      range.setStart(first.node, position.start - first.start);
      range.setEnd(last.node, position.end - last.start);
      if (range.toString() !== anchor.quote || !range.getClientRects().length) return false;
      ranges.push(range);
      return true;
    });
    if (ranges.length) {
      CSS.highlights.set(highlightName, new Highlight(...ranges));
      // Live ranges follow DOM edits. Discard them before they can paint new text.
      const observer = new MutationObserver(() => {
        CSS.highlights.delete(highlightName);
        observer.disconnect();
      });
      observer.observe(document.body, {subtree:true, childList:true, characterData:true});
      globalThis.openBrainHighlightObserver?.disconnect();
      globalThis.openBrainHighlightObserver = observer;
    }
    return resolved;
  }

  globalThis.openBrainAnchors = Object.freeze({locateQuote, selectionAnchor, capture, restore});
})();
