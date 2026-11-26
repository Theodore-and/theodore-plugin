//! A small JSON value: what the front and the relay read and write (MCP messages, C1 frames, `service.json`,
//! `launch.json`). Numbers keep the text they were written with, so a JSON-RPC id or a count goes back exactly.

use std::fmt::Write as _;

#[derive(Clone, Debug, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    Num(String),
    Str(String),
    Arr(Vec<Json>),
    Obj(Vec<(String, Json)>),
}

/// Nesting deeper than this is refused (a message is never this deep; a hostile one would exhaust the stack).
const MAX_DEPTH: usize = 256;

impl Json {
    pub fn obj() -> Json {
        Json::Obj(Vec::new())
    }

    pub fn str<S: Into<String>>(s: S) -> Json {
        Json::Str(s.into())
    }

    pub fn int(n: i64) -> Json {
        Json::Num(n.to_string())
    }

    /// The member `key` of an object (the last one, as Python's json reads a repeated name).
    pub fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Obj(members) => members.iter().rev().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }

    pub fn as_bool(&self) -> Option<bool> {
        match self {
            Json::Bool(b) => Some(*b),
            _ => None,
        }
    }

    pub fn as_i64(&self) -> Option<i64> {
        match self {
            Json::Num(text) => text.parse::<i64>().ok().or_else(|| {
                text.parse::<f64>().ok().filter(|f| f.fract() == 0.0 && f.abs() < 9.0e15).map(|f| f as i64)
            }),
            _ => None,
        }
    }

    pub fn as_array(&self) -> Option<&Vec<Json>> {
        match self {
            Json::Arr(items) => Some(items),
            _ => None,
        }
    }

    /// Sets `key` on an object, replacing an earlier value. A value that is not an object becomes one.
    pub fn set(&mut self, key: &str, value: Json) {
        if !matches!(self, Json::Obj(_)) {
            *self = Json::obj();
        }
        if let Json::Obj(members) = self {
            members.retain(|(k, _)| k != key);
            members.push((key.to_string(), value));
        }
    }

    /// `set`, as a builder.
    pub fn with(mut self, key: &str, value: Json) -> Json {
        self.set(key, value);
        self
    }

    pub fn remove(&mut self, key: &str) -> Option<Json> {
        if let Json::Obj(members) = self {
            let at = members.iter().rposition(|(k, _)| k == key)?;
            let (_, value) = members.remove(at);
            members.retain(|(k, _)| k != key);
            return Some(value);
        }
        None
    }

    pub fn dump(&self) -> String {
        let mut out = String::new();
        self.write_to(&mut out);
        out
    }

    pub fn write_to(&self, out: &mut String) {
        match self {
            Json::Null => out.push_str("null"),
            Json::Bool(true) => out.push_str("true"),
            Json::Bool(false) => out.push_str("false"),
            Json::Num(text) => out.push_str(text),
            Json::Str(s) => write_str(out, s),
            Json::Arr(items) => {
                out.push('[');
                for (i, item) in items.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    item.write_to(out);
                }
                out.push(']');
            }
            Json::Obj(members) => {
                out.push('{');
                for (i, (key, value)) in members.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    write_str(out, key);
                    out.push(':');
                    value.write_to(out);
                }
                out.push('}');
            }
        }
    }
}

impl From<&str> for Json {
    fn from(s: &str) -> Json {
        Json::Str(s.to_string())
    }
}

impl From<String> for Json {
    fn from(s: String) -> Json {
        Json::Str(s)
    }
}

impl From<bool> for Json {
    fn from(b: bool) -> Json {
        Json::Bool(b)
    }
}

/// A string as JSON: quotes, backslashes and control characters escaped, everything else as UTF-8 (Python's
/// `ensure_ascii=False`).
pub fn write_str(out: &mut String, s: &str) {
    out.push('"');
    let bytes = s.as_bytes();
    let mut start = 0;
    for (i, &b) in bytes.iter().enumerate() {
        let short = match b {
            b'"' => "\\\"",
            b'\\' => "\\\\",
            b'\n' => "\\n",
            b'\r' => "\\r",
            b'\t' => "\\t",
            0x08 => "\\b",
            0x0c => "\\f",
            0x00..=0x1f => "",
            _ => continue,
        };
        out.push_str(&s[start..i]);
        if short.is_empty() {
            let _ = write!(out, "\\u{:04x}", b);
        } else {
            out.push_str(short);
        }
        start = i + 1;
    }
    out.push_str(&s[start..]);
    out.push('"');
}

pub fn parse(text: &str) -> Result<Json, String> {
    let mut p = Parser { s: text.as_bytes(), text, i: 0, depth: 0 };
    p.ws();
    let value = p.value()?;
    p.ws();
    if p.i != p.s.len() {
        return Err(format!("unexpected text at {}", p.i));
    }
    Ok(value)
}

struct Parser<'a> {
    s: &'a [u8],
    text: &'a str,
    i: usize,
    depth: usize,
}

impl<'a> Parser<'a> {
    fn ws(&mut self) {
        while self.i < self.s.len() && matches!(self.s[self.i], b' ' | b'\t' | b'\n' | b'\r') {
            self.i += 1;
        }
    }

    fn peek(&self) -> Option<u8> {
        self.s.get(self.i).copied()
    }

    fn expect(&mut self, word: &str) -> Result<(), String> {
        if self.s[self.i..].starts_with(word.as_bytes()) {
            self.i += word.len();
            Ok(())
        } else {
            Err(format!("expected {} at {}", word, self.i))
        }
    }

    fn value(&mut self) -> Result<Json, String> {
        match self.peek() {
            None => Err("unexpected end".into()),
            Some(b'{') => self.object(),
            Some(b'[') => self.array(),
            Some(b'"') => Ok(Json::Str(self.string()?)),
            Some(b't') => self.expect("true").map(|_| Json::Bool(true)),
            Some(b'f') => self.expect("false").map(|_| Json::Bool(false)),
            Some(b'n') => self.expect("null").map(|_| Json::Null),
            Some(b'-' | b'0'..=b'9') => self.number(),
            Some(c) => Err(format!("unexpected {:?} at {}", c as char, self.i)),
        }
    }

    fn enter(&mut self) -> Result<(), String> {
        self.depth += 1;
        if self.depth > MAX_DEPTH {
            return Err("nested too deeply".into());
        }
        self.i += 1;
        Ok(())
    }

    fn object(&mut self) -> Result<Json, String> {
        self.enter()?;
        let mut members = Vec::new();
        self.ws();
        if self.peek() == Some(b'}') {
            self.i += 1;
            self.depth -= 1;
            return Ok(Json::Obj(members));
        }
        loop {
            self.ws();
            if self.peek() != Some(b'"') {
                return Err(format!("expected a name at {}", self.i));
            }
            let key = self.string()?;
            self.ws();
            if self.peek() != Some(b':') {
                return Err(format!("expected ':' at {}", self.i));
            }
            self.i += 1;
            self.ws();
            let value = self.value()?;
            members.push((key, value));
            self.ws();
            match self.peek() {
                Some(b',') => self.i += 1,
                Some(b'}') => {
                    self.i += 1;
                    break;
                }
                _ => return Err(format!("expected ',' or '}}' at {}", self.i)),
            }
        }
        self.depth -= 1;
        Ok(Json::Obj(members))
    }

    fn array(&mut self) -> Result<Json, String> {
        self.enter()?;
        let mut items = Vec::new();
        self.ws();
        if self.peek() == Some(b']') {
            self.i += 1;
            self.depth -= 1;
            return Ok(Json::Arr(items));
        }
        loop {
            self.ws();
            items.push(self.value()?);
            self.ws();
            match self.peek() {
                Some(b',') => self.i += 1,
                Some(b']') => {
                    self.i += 1;
                    break;
                }
                _ => return Err(format!("expected ',' or ']' at {}", self.i)),
            }
        }
        self.depth -= 1;
        Ok(Json::Arr(items))
    }

    fn hex4(&mut self) -> Result<u32, String> {
        let digits = self.s.get(self.i..self.i + 4).ok_or("short \\u escape")?;
        let mut value = 0u32;
        for &d in digits {
            let n = (d as char).to_digit(16).ok_or("bad \\u escape")?;
            value = value * 16 + n;
        }
        self.i += 4;
        Ok(value)
    }

    fn string(&mut self) -> Result<String, String> {
        self.i += 1; // the opening quote
        let mut out = String::new();
        let mut start = self.i;
        loop {
            let b = *self.s.get(self.i).ok_or("unterminated string")?;
            match b {
                b'"' => {
                    out.push_str(&self.text[start..self.i]);
                    self.i += 1;
                    return Ok(out);
                }
                b'\\' => {
                    out.push_str(&self.text[start..self.i]);
                    self.i += 1;
                    let e = *self.s.get(self.i).ok_or("unterminated escape")?;
                    self.i += 1;
                    match e {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{08}'),
                        b'f' => out.push('\u{0c}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let first = self.hex4()?;
                            let code = if (0xD800..0xDC00).contains(&first)
                                && self.s.get(self.i) == Some(&b'\\')
                                && self.s.get(self.i + 1) == Some(&b'u')
                            {
                                let save = self.i;
                                self.i += 2;
                                let second = self.hex4()?;
                                if (0xDC00..0xE000).contains(&second) {
                                    0x10000 + ((first - 0xD800) << 10) + (second - 0xDC00)
                                } else {
                                    self.i = save; // a lone high surrogate; the next escape is read on its own
                                    0xFFFD
                                }
                            } else {
                                first
                            };
                            out.push(char::from_u32(code).unwrap_or('\u{FFFD}'));
                        }
                        _ => return Err(format!("bad escape at {}", self.i - 1)),
                    }
                    start = self.i;
                }
                0x00..=0x1f => return Err(format!("control character in a string at {}", self.i)),
                _ => self.i += 1,
            }
        }
    }

    fn number(&mut self) -> Result<Json, String> {
        let start = self.i;
        if self.peek() == Some(b'-') {
            self.i += 1;
        }
        match self.peek() {
            Some(b'0') => self.i += 1,
            Some(b'1'..=b'9') => {
                while matches!(self.peek(), Some(b'0'..=b'9')) {
                    self.i += 1;
                }
            }
            _ => return Err(format!("bad number at {}", start)),
        }
        if self.peek() == Some(b'.') {
            self.i += 1;
            if !matches!(self.peek(), Some(b'0'..=b'9')) {
                return Err(format!("bad number at {}", start));
            }
            while matches!(self.peek(), Some(b'0'..=b'9')) {
                self.i += 1;
            }
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            self.i += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.i += 1;
            }
            if !matches!(self.peek(), Some(b'0'..=b'9')) {
                return Err(format!("bad number at {}", start));
            }
            while matches!(self.peek(), Some(b'0'..=b'9')) {
                self.i += 1;
            }
        }
        Ok(Json::Num(self.text[start..self.i].to_string()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips() {
        for text in [
            r#"{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"pptx","arguments":{"args":["slide","list","/w"]}}}"#,
            r#"[1,-2,3.5,1e-7,-0.0,true,false,null,"",{}]"#,
            r#"{"a":"é ✓ 𝄞","b":"line\nbreak\ttab\"quote\\slash"}"#,
            r#"{"id":12345678901234567890}"#,
        ] {
            let value = parse(text).unwrap();
            assert_eq!(value.dump(), text, "round trip of {text}");
        }
    }

    #[test]
    fn escapes_and_surrogates() {
        let value = parse(r#""é𝄞\/\b\f\u0001""#).unwrap();
        assert_eq!(value, Json::str("é𝄞/\u{8}\u{c}\u{1}"));
        assert_eq!(value.dump(), "\"é𝄞/\\b\\f\\u0001\"");
        assert_eq!(parse(r#""\ud834x""#).unwrap(), Json::str("\u{FFFD}x"));
        assert_eq!(parse(r#""\ud834A""#).unwrap(), Json::str("\u{FFFD}A"));
    }

    #[test]
    fn refuses_what_is_not_json() {
        for text in ["", "{", "[1,]", "{\"a\" 1}", "01", "1.", "-", "\"a", "\"\u{1}\"", "tru", "[1] x", "{'a':1}"] {
            assert!(parse(text).is_err(), "{text:?} should be refused");
        }
        let deep = "[".repeat(MAX_DEPTH + 1) + &"]".repeat(MAX_DEPTH + 1);
        assert!(parse(&deep).is_err());
        let ok = "[".repeat(MAX_DEPTH) + &"]".repeat(MAX_DEPTH);
        assert!(parse(&ok).is_ok());
    }

    #[test]
    fn members() {
        let mut value = parse(r#"{"a":1,"b":"x","a":2}"#).unwrap();
        assert_eq!(value.get("a").and_then(Json::as_i64), Some(2));
        assert_eq!(value.get("b").and_then(Json::as_str), Some("x"));
        value.set("b", Json::Bool(true));
        assert_eq!(value.get("b"), Some(&Json::Bool(true)));
        assert_eq!(value.remove("a").and_then(|v| v.as_i64()), Some(2));
        assert!(value.get("a").is_none());
        assert_eq!(parse("3.0").unwrap().as_i64(), Some(3));
        assert_eq!(parse("3.5").unwrap().as_i64(), None);
    }
}
