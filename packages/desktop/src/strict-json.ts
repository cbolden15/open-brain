/** Decode transport JSON before duplicate keys or invalid Unicode can be erased. */
export function parseStrictJson(raw: string | Uint8Array, integersOnly = false): unknown {
  const text = typeof raw === "string" ? raw : new TextDecoder("utf-8", { fatal: true }).decode(raw);
  let offset = 0;
  function whitespace(): void { while (/\s/.test(text[offset] ?? "") && offset < text.length) offset++; }
  function quoted(): string {
    whitespace();
    const match = /^"(?:[^"\\\u0000-\u001f]|\\(?:["\\/bfnrt]|u[\da-fA-F]{4}))*"/.exec(text.slice(offset));
    if (!match) throw new Error("invalid_json");
    offset += match[0].length;
    const value: string = JSON.parse(match[0]);
    for (const scalar of value) {
      const point = scalar.codePointAt(0)!;
      if (point === 0 || (point >= 0xd800 && point <= 0xdfff)) throw new Error("invalid_json");
    }
    return value;
  }
  function value(depth: number): void {
    if (depth > 64) throw new Error("invalid_json");
    whitespace();
    const token = text[offset];
    if (token === '"') { quoted(); return; }
    if (token === "{" || token === "[") {
      const close = token === "{" ? "}" : "]";
      offset++; whitespace();
      const keys = new Set<string>();
      if (text[offset] === close) { offset++; return; }
      while (true) {
        if (token === "{") {
          const key = quoted();
          if (keys.has(key)) throw new Error("invalid_json");
          keys.add(key); whitespace();
          if (text[offset++] !== ":") throw new Error("invalid_json");
        }
        value(depth + 1); whitespace();
        const separator = text[offset++];
        if (separator === close) return;
        if (separator !== ",") throw new Error("invalid_json");
      }
    }
    const match = /^(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/.exec(text.slice(offset));
    if (!match) throw new Error("invalid_json");
    offset += match[0].length;
    if (integersOnly && /[.eE]/.test(match[0]) && !["true", "false", "null"].includes(match[0])) throw new Error("invalid_json");
    if (!["true", "false", "null"].includes(match[0]) && !Number.isFinite(Number(match[0]))) throw new Error("invalid_json");
  }
  value(0); whitespace();
  if (offset !== text.length) throw new Error("invalid_json");
  return JSON.parse(text);
}
