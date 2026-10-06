// Reconstruction of dotnetapp (NotesApp, .NET 8) authored from Rebuild Studio evidence:
// ILSpy 9.1 recovery of dotnetapp.dll (NotesApp/Program.cs, Menus.cs, AppState.cs,
// StateCorruptException.cs) plus the feature list. System.Text.Json behaviour that the
// recovered code relies on (WriteIndented output, default escaping, strict parsing,
// case-sensitive property binding) is emulated by hand so the state file is byte-exact.
use std::fs;
use std::io::{self, Read, Write};
use std::path::Path;
use std::process::exit;

// ---------------------------------------------------------------- AppState

struct AppState {
    version: i32,
    user_name: Option<String>,
    theme: Option<String>,
    notes: Option<Vec<Option<String>>>,
}

impl AppState {
    fn new() -> Self {
        AppState { version: 1, user_name: Some("guest".into()), theme: Some("light".into()), notes: Some(Vec::new()) }
    }
    fn user_name(&self) -> &str { self.user_name.as_deref().unwrap_or("") }
    fn theme(&self) -> &str { self.theme.as_deref().unwrap_or("") }
    fn notes(&self) -> &Vec<Option<String>> { self.notes.as_ref().unwrap() }
    fn notes_mut(&mut self) -> &mut Vec<Option<String>> { self.notes.as_mut().unwrap() }

    /// AppState.Load: missing file -> defaults; JsonException -> "not valid JSON";
    /// null root or null Notes/Theme/UserName -> "missing fields".
    fn load(path: &str) -> Result<AppState, String> {
        let p = Path::new(path);
        if !p.is_file() {
            return Ok(AppState::new());
        }
        let bytes = match fs::read(p) { Ok(b) => b, Err(_) => return Ok(AppState::new()) };
        let text = decode_text(&bytes);
        let value = match Parser::new(&text).parse_document() {
            Ok(v) => v,
            Err(()) => return Err("state file is not valid JSON".into()),
        };
        let st = match bind_state(&value) {
            Ok(Some(s)) => s,
            Ok(None) => return Err("state file has missing fields".into()),
            Err(()) => return Err("state file is not valid JSON".into()),
        };
        if st.notes.is_none() || st.theme.is_none() || st.user_name.is_none() {
            return Err("state file has missing fields".into());
        }
        Ok(st)
    }

    /// AppState.Save: JsonSerializer.Serialize(this, WriteIndented) + "\n".
    fn save(&self, path: &str) {
        let mut s = String::new();
        s.push_str("{\n");
        s.push_str(&format!("  \"Version\": {},\n", self.version));
        s.push_str("  \"UserName\": "); push_jstr(&mut s, &self.user_name); s.push_str(",\n");
        s.push_str("  \"Theme\": "); push_jstr(&mut s, &self.theme); s.push_str(",\n");
        s.push_str("  \"Notes\": ");
        match &self.notes {
            None => s.push_str("null"),
            Some(v) if v.is_empty() => s.push_str("[]"),
            Some(v) => {
                s.push_str("[\n");
                for (i, n) in v.iter().enumerate() {
                    s.push_str("    ");
                    push_jstr(&mut s, n);
                    if i + 1 < v.len() { s.push(','); }
                    s.push('\n');
                }
                s.push_str("  ]");
            }
        }
        s.push_str("\n}");
        s.push('\n');
        if let Err(e) = fs::write(path, s.as_bytes()) {
            eprintln!("Unhandled exception. System.IO.IOException: {}", e);
            exit(134);
        }
    }
}

/// File.ReadAllText: strip a UTF-8 BOM (UTF-16 BOMs handled too), invalid bytes -> U+FFFD.
fn decode_text(b: &[u8]) -> String {
    if b.len() >= 3 && b[0] == 0xEF && b[1] == 0xBB && b[2] == 0xBF {
        return String::from_utf8_lossy(&b[3..]).into_owned();
    }
    if b.len() >= 2 && (b[0] == 0xFF && b[1] == 0xFE || b[0] == 0xFE && b[1] == 0xFF) {
        let le = b[0] == 0xFF;
        let units: Vec<u16> = b[2..].chunks(2).filter(|c| c.len() == 2)
            .map(|c| if le { u16::from_le_bytes([c[0], c[1]]) } else { u16::from_be_bytes([c[0], c[1]]) }).collect();
        return String::from_utf16_lossy(&units);
    }
    String::from_utf8_lossy(b).into_owned()
}

/// System.Text.Json default (JavaScriptEncoder.Default) string escaping.
fn push_jstr(out: &mut String, v: &Option<String>) {
    let s = match v { None => { out.push_str("null"); return; } Some(s) => s };
    out.push('"');
    for ch in s.chars() {
        match ch {
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            '\\' => out.push_str("\\\\"),
            '"' | '&' | '\'' | '+' | '<' | '>' | '`' => out.push_str(&format!("\\u{:04X}", ch as u32)),
            c if (c as u32) < 0x20 || (c as u32) >= 0x7F => {
                let mut buf = [0u16; 2];
                for u in c.encode_utf16(&mut buf) { out.push_str(&format!("\\u{:04X}", u)); }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

// ---------------------------------------------------------------- minimal strict JSON

enum J { Null, Bool, Num(String), Str(String), Arr(Vec<J>), Obj(Vec<(String, J)>) }

struct Parser<'a> { s: &'a [u8], i: usize, depth: usize }

impl<'a> Parser<'a> {
    fn new(t: &'a str) -> Self { Parser { s: t.as_bytes(), i: 0, depth: 0 } }
    fn ws(&mut self) { while self.i < self.s.len() && matches!(self.s[self.i], b' ' | b'\t' | b'\n' | b'\r') { self.i += 1; } }
    fn parse_document(&mut self) -> Result<J, ()> {
        self.ws();
        let v = self.value()?;
        self.ws();
        if self.i != self.s.len() { return Err(()); }
        Ok(v)
    }
    fn lit(&mut self, w: &[u8]) -> Result<(), ()> {
        if self.s[self.i..].starts_with(w) { self.i += w.len(); Ok(()) } else { Err(()) }
    }
    fn value(&mut self) -> Result<J, ()> {
        if self.i >= self.s.len() { return Err(()); }
        match self.s[self.i] {
            b'n' => { self.lit(b"null")?; Ok(J::Null) }
            b't' => { self.lit(b"true")?; Ok(J::Bool) }
            b'f' => { self.lit(b"false")?; Ok(J::Bool) }
            b'"' => Ok(J::Str(self.string()?)),
            b'[' => {
                self.depth += 1; if self.depth > 64 { return Err(()); }
                self.i += 1; self.ws();
                let mut v = Vec::new();
                if self.i < self.s.len() && self.s[self.i] == b']' { self.i += 1; self.depth -= 1; return Ok(J::Arr(v)); }
                loop {
                    self.ws(); v.push(self.value()?); self.ws();
                    if self.i >= self.s.len() { return Err(()); }
                    match self.s[self.i] { b',' => self.i += 1, b']' => { self.i += 1; break; } _ => return Err(()) }
                }
                self.depth -= 1; Ok(J::Arr(v))
            }
            b'{' => {
                self.depth += 1; if self.depth > 64 { return Err(()); }
                self.i += 1; self.ws();
                let mut v = Vec::new();
                if self.i < self.s.len() && self.s[self.i] == b'}' { self.i += 1; self.depth -= 1; return Ok(J::Obj(v)); }
                loop {
                    self.ws();
                    if self.i >= self.s.len() || self.s[self.i] != b'"' { return Err(()); }
                    let k = self.string()?; self.ws();
                    if self.i >= self.s.len() || self.s[self.i] != b':' { return Err(()); }
                    self.i += 1; self.ws();
                    let val = self.value()?; v.push((k, val)); self.ws();
                    if self.i >= self.s.len() { return Err(()); }
                    match self.s[self.i] { b',' => self.i += 1, b'}' => { self.i += 1; break; } _ => return Err(()) }
                }
                self.depth -= 1; Ok(J::Obj(v))
            }
            b'-' | b'0'..=b'9' => Ok(J::Num(self.number()?)),
            _ => Err(()),
        }
    }
    fn number(&mut self) -> Result<String, ()> {
        let st = self.i;
        let s = self.s;
        if s[self.i] == b'-' { self.i += 1; }
        if self.i >= s.len() { return Err(()); }
        if s[self.i] == b'0' { self.i += 1; }
        else if s[self.i].is_ascii_digit() { while self.i < s.len() && s[self.i].is_ascii_digit() { self.i += 1; } }
        else { return Err(()); }
        if self.i < s.len() && s[self.i] == b'.' {
            self.i += 1;
            let d = self.i; while self.i < s.len() && s[self.i].is_ascii_digit() { self.i += 1; }
            if d == self.i { return Err(()); }
        }
        if self.i < s.len() && (s[self.i] == b'e' || s[self.i] == b'E') {
            self.i += 1;
            if self.i < s.len() && (s[self.i] == b'+' || s[self.i] == b'-') { self.i += 1; }
            let d = self.i; while self.i < s.len() && s[self.i].is_ascii_digit() { self.i += 1; }
            if d == self.i { return Err(()); }
        }
        if self.i < s.len() && !matches!(s[self.i], b' ' | b'\t' | b'\n' | b'\r' | b',' | b']' | b'}') { return Err(()); }
        Ok(String::from_utf8_lossy(&s[st..self.i]).into_owned())
    }
    fn hex4(&mut self) -> Result<u32, ()> {
        if self.i + 4 > self.s.len() { return Err(()); }
        let h = std::str::from_utf8(&self.s[self.i..self.i + 4]).map_err(|_| ())?;
        let v = u32::from_str_radix(h, 16).map_err(|_| ())?;
        if !h.bytes().all(|c| c.is_ascii_hexdigit()) { return Err(()); }
        self.i += 4; Ok(v)
    }
    fn string(&mut self) -> Result<String, ()> {
        self.i += 1;
        let mut out: Vec<u8> = Vec::new();
        loop {
            if self.i >= self.s.len() { return Err(()); }
            let c = self.s[self.i];
            match c {
                b'"' => { self.i += 1; break; }
                b'\\' => {
                    self.i += 1;
                    if self.i >= self.s.len() { return Err(()); }
                    let e = self.s[self.i]; self.i += 1;
                    let ch = match e {
                        b'"' => '"', b'\\' => '\\', b'/' => '/', b'b' => '\u{8}', b'f' => '\u{c}',
                        b'n' => '\n', b'r' => '\r', b't' => '\t',
                        b'u' => {
                            let u = self.hex4()?;
                            if (0xD800..0xDC00).contains(&u) {
                                if self.s[self.i..].starts_with(b"\\u") {
                                    self.i += 2;
                                    let l = self.hex4()?;
                                    if (0xDC00..0xE000).contains(&l) {
                                        char::from_u32(0x10000 + ((u - 0xD800) << 10) + (l - 0xDC00)).ok_or(())?
                                    } else { return Err(()); }
                                } else { return Err(()); }
                            } else if (0xDC00..0xE000).contains(&u) { return Err(()); }
                            else { char::from_u32(u).ok_or(())? }
                        }
                        _ => return Err(()),
                    };
                    let mut b = [0u8; 4];
                    out.extend_from_slice(ch.encode_utf8(&mut b).as_bytes());
                }
                0..=0x1F => return Err(()),
                _ => { out.push(c); self.i += 1; }
            }
        }
        String::from_utf8(out).map_err(|_| ())
    }
}

/// JsonSerializer.Deserialize<AppState>: Ok(None) for a null root, Err for JsonException.
fn bind_state(v: &J) -> Result<Option<AppState>, ()> {
    let props = match v { J::Null => return Ok(None), J::Obj(p) => p, _ => return Err(()) };
    let mut st = AppState::new();
    for (k, val) in props {
        match k.as_str() {
            "Version" => match val {
                J::Num(n) => st.version = n.parse::<i32>().map_err(|_| ())?,
                _ => return Err(()),
            },
            "UserName" => st.user_name = bind_str(val)?,
            "Theme" => st.theme = bind_str(val)?,
            "Notes" => st.notes = match val {
                J::Null => None,
                J::Arr(items) => Some(items.iter().map(bind_str).collect::<Result<Vec<_>, ()>>()?),
                _ => return Err(()),
            },
            _ => {}
        }
    }
    Ok(Some(st))
}

fn bind_str(v: &J) -> Result<Option<String>, ()> {
    match v { J::Null => Ok(None), J::Str(s) => Ok(Some(s.clone())), _ => Err(()) }
}

// ---------------------------------------------------------------- console I/O

/// StreamReader.ReadLine over UTF-8 stdin: \n, \r and \r\n terminate a line; BOM skipped.
struct LineReader { buf: Vec<u8>, pos: usize, eof: bool, first: bool }

impl LineReader {
    fn new() -> Self { LineReader { buf: Vec::new(), pos: 0, eof: false, first: true } }
    fn fill(&mut self) -> bool {
        if self.pos < self.buf.len() { return true; }
        if self.eof { return false; }
        let mut tmp = [0u8; 4096];
        loop {
            match io::stdin().lock().read(&mut tmp) {
                Ok(0) => { self.eof = true; return false; }
                Ok(n) => { self.buf.clear(); self.buf.extend_from_slice(&tmp[..n]); self.pos = 0; return true; }
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                Err(_) => { self.eof = true; return false; }
            }
        }
    }
    fn peek(&mut self) -> Option<u8> { if self.fill() { Some(self.buf[self.pos]) } else { None } }
    fn read_line(&mut self) -> Option<String> {
        if self.first {
            self.first = false;
            // StreamReader skips a UTF-8 preamble at the start of the stream
            if self.fill() && self.buf[self.pos..].starts_with(&[0xEF, 0xBB, 0xBF]) { self.pos += 3; }
        }
        let mut line = Vec::new();
        let mut any = false;
        loop {
            match self.peek() {
                None => { return if any { Some(String::from_utf8_lossy(&line).into_owned()) } else { None }; }
                Some(b) => {
                    any = true; self.pos += 1;
                    if b == b'\n' { break; }
                    if b == b'\r' { if self.peek() == Some(b'\n') { self.pos += 1; } break; }
                    line.push(b);
                }
            }
        }
        Some(String::from_utf8_lossy(&line).into_owned())
    }
}

struct Out;
impl Out {
    fn write(&self, s: &str) { let mut o = io::stdout().lock(); let _ = o.write_all(s.as_bytes()); let _ = o.flush(); }
    fn line(&self, s: &str) { self.write(s); self.write("\n"); }
}

// ---------------------------------------------------------------- Menus

struct Menus<'a> { state: &'a mut AppState, path: String, input: LineReader, out: Out }

#[derive(PartialEq)]
enum Choice { Eof, Text(String) }

impl<'a> Menus<'a> {
    fn prompt(&mut self, text: &str) -> Option<String> {
        self.out.write(text);
        match self.input.read_line() {
            None => { self.out.line(""); None }
            Some(l) => Some(l.trim().to_string()),
        }
    }
    fn choice(&mut self, text: &str) -> Choice {
        match self.prompt(text) { None => Choice::Eof, Some(s) => Choice::Text(s) }
    }

    fn run_main(&mut self) {
        loop {
            self.out.line("=== Main Menu ===");
            self.out.line("1) Notes");
            self.out.line("2) Settings");
            self.out.line("0) Quit");
            let c = self.choice("> ");
            let t = match c { Choice::Eof => return, Choice::Text(t) => t };
            match t.as_str() {
                "0" | "q" => return,
                "1" => self.run_notes(),
                "2" => self.run_settings(),
                _ => self.out.line(&format!("Unknown choice: {}", t)),
            }
        }
    }

    fn run_notes(&mut self) {
        loop {
            self.out.line(&format!("--- Notes ({}) ---", self.state.notes().len()));
            self.out.line("1) Add note");
            self.out.line("2) List notes");
            self.out.line("3) Delete note");
            self.out.line("0) Back");
            let t = match self.choice("notes> ") { Choice::Eof => return, Choice::Text(t) => t };
            match t.as_str() {
                "0" => return,
                "1" => {
                    let note = self.prompt("Text: ");
                    match note {
                        Some(n) if !n.is_empty() => {
                            self.state.notes_mut().push(Some(n));
                            self.state.save(&self.path);
                            self.out.line(&format!("Added note #{}.", self.state.notes().len()));
                        }
                        _ => self.out.line("Note not added (empty)."),
                    }
                }
                "2" => {
                    if self.state.notes().is_empty() { self.out.line("(no notes)"); }
                    let lines: Vec<String> = self.state.notes().iter().enumerate()
                        .map(|(i, n)| format!("{}. {}", i + 1, n.as_deref().unwrap_or(""))).collect();
                    for l in lines { self.out.line(&l); }
                }
                "3" => {
                    let ans = self.prompt("Delete which number? ");
                    let n = ans.as_deref().and_then(parse_int);
                    match n {
                        Some(k) if k >= 1 && (k as usize) <= self.state.notes().len() => {
                            let removed = self.state.notes_mut().remove(k as usize - 1);
                            self.state.save(&self.path);
                            self.out.line(&format!("Deleted: {}", removed.as_deref().unwrap_or("")));
                        }
                        _ => self.out.line("No such note."),
                    }
                }
                _ => self.out.line(&format!("Unknown choice: {}", t)),
            }
        }
    }

    fn run_settings(&mut self) {
        loop {
            self.out.line("--- Settings ---");
            self.out.line(&format!("1) User name: {}", self.state.user_name()));
            self.out.line(&format!("2) Theme: {}", self.state.theme()));
            self.out.line("0) Back");
            let t = match self.choice("settings> ") { Choice::Eof => return, Choice::Text(t) => t };
            match t.as_str() {
                "0" => return,
                "1" => {
                    match self.prompt("New name: ") {
                        Some(n) if !n.is_empty() => {
                            self.state.user_name = Some(n.clone());
                            self.state.save(&self.path);
                            self.out.line(&format!("Name set to {}.", n));
                        }
                        _ => self.out.line("Name unchanged."),
                    }
                }
                "2" => {
                    let nt = if self.state.theme() == "light" { "dark" } else { "light" };
                    self.state.theme = Some(nt.to_string());
                    self.state.save(&self.path);
                    self.out.line(&format!("Theme is now {}.", nt));
                }
                _ => self.out.line(&format!("Unknown choice: {}", t)),
            }
        }
    }
}

/// int.TryParse(s) with NumberStyles.Integer: optional surrounding whitespace, optional sign, ASCII digits.
fn parse_int(s: &str) -> Option<i32> {
    let t = s.trim();
    let (neg, digits) = if let Some(r) = t.strip_prefix('-') { (true, r) } else if let Some(r) = t.strip_prefix('+') { (false, r) } else { (false, t) };
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) { return None; }
    let mut v: i64 = 0;
    for b in digits.bytes() {
        v = v * 10 + (b - b'0') as i64;
        if v > 1i64 << 32 { return None; }
    }
    let v = if neg { -v } else { v };
    if v < i32::MIN as i64 || v > i32::MAX as i64 { None } else { Some(v as i32) }
}

// ---------------------------------------------------------------- Program.Main

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.len() != 1 {
        eprintln!("usage: dotnetapp <state.json>");
        exit(2);
    }
    let path = args[0].clone();
    let mut state = match AppState::load(&path) {
        Ok(s) => s,
        Err(msg) => {
            eprintln!("Sorry, your saved data in '{}' could not be read ({}).", path, msg);
            eprintln!("Please fix or delete the file and start again.");
            exit(4);
        }
    };
    let out = Out;
    out.line(&format!("Notes App 1.0 - hello, {} ({} notes, theme {})", state.user_name(), state.notes().len(), state.theme()));
    {
        let mut m = Menus { state: &mut state, path: path.clone(), input: LineReader::new(), out: Out };
        m.run_main();
    }
    state.save(&path);
    out.line(&format!("Goodbye, {}.", state.user_name()));
    exit(0);
}
