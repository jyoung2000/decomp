// Reconstruction of pecli authored from Rebuild Studio evidence (rz-ghidra decompilation of the stripped PE:
// fcn.140001d63 = main, fcn.140001599 = load_store, fcn.140001bfd = write_store, fcn.140001ab0 = serialize,
// fcn.140001480 = crc32, fcn.1400014de = find_key). Strings/format texts come from the recovered .rdata.
use std::fs;
use std::io::Write;
use std::process::exit;

const MAX_KEY: usize = 0x40;
const MAX_VAL: usize = 0x400;
const MAX_RECORDS: usize = 0x100;

const USAGE: &str = "usage: pecli <command> <file> [args]\n  init <file>               create an empty store\n  add <file> <key> <value>  add or replace a record\n  get <file> <key>          print a record value\n  list <file>               list all records\n  remove <file> <key>       delete a record\n  checksum <file>           print stored and computed CRC32\n";

struct Store {
    records: Vec<(Vec<u8>, Vec<u8>)>,
}

fn crc32(seed: u32, data: &[u8]) -> u32 {
    let mut c = !seed;
    for &b in data {
        c ^= b as u32;
        for _ in 0..8 {
            c = ((c & 1).wrapping_neg() & 0xedb8_8320) ^ (c >> 1);
        }
    }
    !c
}

fn u32le(b: &[u8]) -> u32 {
    (b[0] as u32) | ((b[1] as u32) << 8) | ((b[2] as u32) << 16) | ((b[3] as u32) << 24)
}

fn serialize(s: &Store) -> Vec<u8> {
    let mut out = Vec::new();
    for (k, v) in &s.records {
        out.extend_from_slice(&(k.len() as u16).to_le_bytes());
        out.extend_from_slice(&(v.len() as u16).to_le_bytes());
        out.extend_from_slice(k);
        out.extend_from_slice(v);
    }
    out
}

fn err(msg: String) {
    let _ = std::io::stderr().write_all(msg.as_bytes());
}

fn out(msg: String) {
    let so = std::io::stdout();
    let mut l = so.lock();
    let _ = l.write_all(msg.as_bytes());
    let _ = l.flush();
}

/// Returns Ok(store) or Err(exit code) after printing the matching error.
fn load_store(path: &str) -> Result<Store, i32> {
    let data = match fs::read(path) {
        Ok(d) => d,
        Err(_) => {
            err(format!("error: file not found: {}\n", path));
            return Err(3);
        }
    };
    if data.len() < 16 || &data[0..4] != b"PCLI" {
        err(format!("error: bad magic in {} (not a PCLI file)\n", path));
        return Err(2);
    }
    let version = u32le(&data[4..8]);
    if version != 1 {
        err(format!("error: unsupported version {} in {}\n", version, path));
        return Err(2);
    }
    let count = u32le(&data[8..12]);
    let stored = u32le(&data[12..16]);
    let body = &data[16..];
    if crc32(0, body) != stored {
        err(format!("error: checksum mismatch in {} (file is corrupt)\n", path));
        return Err(4);
    }
    if count > MAX_RECORDS as u32 {
        err(format!("error: too many records ({}) in {}\n", count, path));
        return Err(4);
    }
    let mut records = Vec::new();
    if count == 0 {
        return Ok(Store { records });
    }
    let mut pos = 0usize;
    let mut parsed = 0u32;
    while pos + 4 <= body.len() {
        let klen = u16::from_le_bytes([body[pos], body[pos + 1]]) as usize;
        let vlen = u16::from_le_bytes([body[pos + 2], body[pos + 3]]) as usize;
        if klen > MAX_KEY || vlen > MAX_VAL {
            break;
        }
        if pos + 4 + klen + vlen > body.len() {
            break;
        }
        let k = body[pos + 4..pos + 4 + klen].to_vec();
        let v = body[pos + 4 + klen..pos + 4 + klen + vlen].to_vec();
        records.push((k, v));
        pos += 4 + klen + vlen;
        parsed += 1;
        if parsed == count {
            return Ok(Store { records });
        }
    }
    err(format!("error: truncated or malformed record {} in {}\n", parsed, path));
    Err(4)
}

fn write_store(path: &str, s: &Store) -> i32 {
    let body = serialize(s);
    let mut file = Vec::with_capacity(16 + body.len());
    file.extend_from_slice(b"PCLI");
    file.extend_from_slice(&1u32.to_le_bytes());
    file.extend_from_slice(&(s.records.len() as u32).to_le_bytes());
    file.extend_from_slice(&crc32(0, &body).to_le_bytes());
    file.extend_from_slice(&body);
    match fs::File::create(path) {
        Ok(mut f) => {
            if f.write_all(&file).is_err() {
                err(format!("error: short write to '{}'\n", path));
                return 5;
            }
            0
        }
        Err(_) => {
            err(format!("error: cannot write '{}'\n", path));
            5
        }
    }
}

fn find_key(s: &Store, key: &[u8]) -> Option<usize> {
    s.records.iter().position(|(k, _)| k.as_slice() == key)
}

fn lossy(b: &[u8]) -> String {
    String::from_utf8_lossy(b).into_owned()
}

fn run(args: &[String]) -> i32 {
    let argc = args.len();
    if argc < 3 {
        err(USAGE.to_string());
        return 1;
    }
    let cmd = args[1].as_str();
    let path = args[2].as_str();
    match (cmd, argc) {
        ("init", 3) => {
            let s = Store { records: Vec::new() };
            let rc = write_store(path, &s);
            if rc == 0 {
                out(format!("initialized {} (0 records)\n", path));
            }
            rc
        }
        ("add", 5) => {
            let key = args[3].as_bytes();
            let value = args[4].as_bytes();
            if key.is_empty() || key.len() > MAX_KEY || value.len() > MAX_VAL {
                err(format!("error: key must be 1..{} chars and value at most {} chars\n", MAX_KEY, MAX_VAL));
                return 1;
            }
            let mut s = match load_store(path) {
                Ok(s) => s,
                Err(rc) => return rc,
            };
            match find_key(&s, key) {
                Some(i) => {
                    s.records[i].1 = value.to_vec();
                    let rc = write_store(path, &s);
                    if rc == 0 {
                        out(format!("updated {}\n", lossy(key)));
                    }
                    rc
                }
                None => {
                    if s.records.len() >= MAX_RECORDS {
                        err("error: store is full\n".to_string());
                        return 1;
                    }
                    s.records.push((key.to_vec(), value.to_vec()));
                    let rc = write_store(path, &s);
                    if rc == 0 {
                        out(format!("added {} ({} records)\n", lossy(key), s.records.len()));
                    }
                    rc
                }
            }
        }
        ("get", 4) => {
            let s = match load_store(path) {
                Ok(s) => s,
                Err(rc) => return rc,
            };
            match find_key(&s, args[3].as_bytes()) {
                Some(i) => {
                    out(format!("{}\n", lossy(&s.records[i].1)));
                    0
                }
                None => {
                    err(format!("error: key not found: {}\n", args[3]));
                    1
                }
            }
        }
        ("list", 3) => {
            let s = match load_store(path) {
                Ok(s) => s,
                Err(rc) => return rc,
            };
            let mut text = format!("{} records\n", s.records.len());
            for (k, v) in &s.records {
                text.push_str(&format!("{}={}\n", lossy(k), lossy(v)));
            }
            out(text);
            0
        }
        ("remove", 4) => {
            let mut s = match load_store(path) {
                Ok(s) => s,
                Err(rc) => return rc,
            };
            match find_key(&s, args[3].as_bytes()) {
                Some(i) => {
                    s.records.remove(i);
                    let rc = write_store(path, &s);
                    if rc == 0 {
                        out(format!("removed {} ({} records)\n", args[3], s.records.len()));
                    }
                    rc
                }
                None => {
                    err(format!("error: key not found: {}\n", args[3]));
                    1
                }
            }
        }
        ("checksum", 3) => {
            let s = match load_store(path) {
                Ok(s) => s,
                Err(rc) => return rc,
            };
            let data = match fs::read(path) {
                Ok(d) if d.len() >= 16 => d,
                _ => {
                    err(format!("error: file not found: {}\n", path));
                    return 3;
                }
            };
            let stored = u32le(&data[12..16]);
            let computed = crc32(0, &serialize(&s));
            out(format!("stored:   {:08x}\ncomputed: {:08x}\nrecords:  {}\n", stored, computed, s.records.len()));
            0
        }
        _ => {
            err(USAGE.to_string());
            1
        }
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    exit(run(&args));
}
