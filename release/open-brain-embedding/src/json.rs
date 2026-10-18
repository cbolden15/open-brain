//! Strict JSON reader: duplicate keys, nesting depth, lone surrogates and
//! boolean-versus-integer confusion are all rejected here.

#[derive(Debug, Clone, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
    Array(Vec<Value>),
    Object(Vec<(String, Value)>),
}

impl Value {
    pub fn get(&self, key: &str) -> Option<&Value> {
        match self {
            Value::Object(m) => m.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::Str(s) => Some(s),
            _ => None,
        }
    }
    pub fn as_int(&self) -> Option<i64> {
        match self {
            Value::Int(i) => Some(*i),
            _ => None,
        }
    }
    pub fn as_object(&self) -> Option<&[(String, Value)]> {
        match self {
            Value::Object(m) => Some(m),
            _ => None,
        }
    }
}

pub const MAX_DEPTH: usize = 8;

struct P<'a> {
    s: &'a str,
    b: &'a [u8],
    i: usize,
}

/// Parses exactly one value; returns it and the offset just past it.
pub fn parse_prefix(text: &str) -> Result<(Value, usize), ()> {
    let mut p = P { s: text, b: text.as_bytes(), i: 0 };
    let v = p.value(0)?;
    Ok((v, p.i))
}

/// Parses a document that must contain exactly one value and nothing else.
pub fn parse_exact(text: &str) -> Result<Value, ()> {
    let (v, n) = parse_prefix(text)?;
    if n == text.len() { Ok(v) } else { Err(()) }
}

impl P<'_> {
    fn ws(&mut self) {
        while matches!(self.b.get(self.i), Some(b' ' | b'\t' | b'\r' | b'\n')) {
            self.i += 1;
        }
    }
    fn eat(&mut self, lit: &[u8]) -> Result<(), ()> {
        if self.b[self.i..].starts_with(lit) {
            self.i += lit.len();
            Ok(())
        } else {
            Err(())
        }
    }
    fn value(&mut self, depth: usize) -> Result<Value, ()> {
        self.ws();
        match self.b.get(self.i).ok_or(())? {
            b'{' => {
                if depth + 1 > MAX_DEPTH {
                    return Err(());
                }
                self.i += 1;
                let mut m: Vec<(String, Value)> = Vec::new();
                self.ws();
                if self.b.get(self.i) == Some(&b'}') {
                    self.i += 1;
                    return Ok(Value::Object(m));
                }
                loop {
                    self.ws();
                    let k = self.string()?;
                    if m.iter().any(|(e, _)| *e == k) {
                        return Err(());
                    }
                    self.ws();
                    self.eat(b":")?;
                    let v = self.value(depth + 1)?;
                    m.push((k, v));
                    self.ws();
                    match self.b.get(self.i) {
                        Some(b',') => self.i += 1,
                        Some(b'}') => {
                            self.i += 1;
                            return Ok(Value::Object(m));
                        }
                        _ => return Err(()),
                    }
                }
            }
            b'[' => {
                if depth + 1 > MAX_DEPTH {
                    return Err(());
                }
                self.i += 1;
                let mut a = Vec::new();
                self.ws();
                if self.b.get(self.i) == Some(&b']') {
                    self.i += 1;
                    return Ok(Value::Array(a));
                }
                loop {
                    a.push(self.value(depth + 1)?);
                    self.ws();
                    match self.b.get(self.i) {
                        Some(b',') => self.i += 1,
                        Some(b']') => {
                            self.i += 1;
                            return Ok(Value::Array(a));
                        }
                        _ => return Err(()),
                    }
                }
            }
            b'"' => Ok(Value::Str(self.string()?)),
            b't' => self.eat(b"true").map(|_| Value::Bool(true)),
            b'f' => self.eat(b"false").map(|_| Value::Bool(false)),
            b'n' => self.eat(b"null").map(|_| Value::Null),
            b'-' | b'0'..=b'9' => self.number(),
            _ => Err(()),
        }
    }
    fn number(&mut self) -> Result<Value, ()> {
        let s = self.i;
        if self.b[self.i] == b'-' {
            self.i += 1;
        }
        match self.b.get(self.i) {
            Some(b'0') => self.i += 1,
            Some(b'1'..=b'9') => self.digits(),
            _ => return Err(()),
        }
        let mut float = false;
        if self.b.get(self.i) == Some(&b'.') {
            float = true;
            self.i += 1;
            if !matches!(self.b.get(self.i), Some(b'0'..=b'9')) {
                return Err(());
            }
            self.digits();
        }
        if matches!(self.b.get(self.i), Some(b'e' | b'E')) {
            float = true;
            self.i += 1;
            if matches!(self.b.get(self.i), Some(b'+' | b'-')) {
                self.i += 1;
            }
            if !matches!(self.b.get(self.i), Some(b'0'..=b'9')) {
                return Err(());
            }
            self.digits();
        }
        let t = std::str::from_utf8(&self.b[s..self.i]).map_err(|_| ())?;
        if !float {
            if let Ok(i) = t.parse::<i64>() {
                return Ok(Value::Int(i));
            }
        }
        let f: f64 = t.parse().map_err(|_| ())?;
        if f.is_finite() { Ok(Value::Float(f)) } else { Err(()) }
    }
    fn digits(&mut self) {
        while matches!(self.b.get(self.i), Some(b'0'..=b'9')) {
            self.i += 1;
        }
    }
    fn hex4(&mut self) -> Result<u32, ()> {
        let h = self.b.get(self.i..self.i + 4).ok_or(())?;
        let mut n = 0u32;
        for c in h {
            n = n * 16 + (*c as char).to_digit(16).ok_or(())?;
        }
        self.i += 4;
        Ok(n)
    }
    fn string(&mut self) -> Result<String, ()> {
        self.eat(b"\"")?;
        let mut out = String::new();
        loop {
            let c = *self.b.get(self.i).ok_or(())?;
            match c {
                b'"' => {
                    self.i += 1;
                    return Ok(out);
                }
                b'\\' => {
                    self.i += 1;
                    let e = *self.b.get(self.i).ok_or(())?;
                    self.i += 1;
                    match e {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{8}'),
                        b'f' => out.push('\u{c}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let hi = self.hex4()?;
                            let cp = if (0xD800..0xDC00).contains(&hi) {
                                self.eat(b"\\u")?;
                                let lo = self.hex4()?;
                                if !(0xDC00..0xE000).contains(&lo) {
                                    return Err(());
                                }
                                0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00)
                            } else {
                                hi
                            };
                            out.push(char::from_u32(cp).ok_or(())?);
                        }
                        _ => return Err(()),
                    }
                }
                0..=0x1f => return Err(()),
                _ => {
                    // Input is a validated &str, so copy one whole char.
                    let ch = self.s[self.i..].chars().next().ok_or(())?;
                    out.push(ch);
                    self.i += ch.len_utf8();
                }
            }
        }
    }
}

/// Escapes a string for output (ASCII-safe controls only; other chars verbatim).
pub fn quote(s: &str) -> String {
    let mut o = String::with_capacity(s.len() + 2);
    o.push('"');
    for c in s.chars() {
        match c {
            '"' => o.push_str("\\\""),
            '\\' => o.push_str("\\\\"),
            c if (c as u32) < 0x20 => o.push_str(&format!("\\u{:04x}", c as u32)),
            c => o.push(c),
        }
    }
    o.push('"');
    o
}
