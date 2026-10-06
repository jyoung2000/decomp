//! DELIBERATELY WRONG remake of `pecli` (fixture negative control).
//!
//! It looks plausible and builds, but differs from the original in ways the verifier
//! must catch:
//!   * CRC32 omits the final XOR (so every stored checksum and file differs byte-wise),
//!   * missing file exits 1 instead of 3,
//!   * bad magic exits 1 instead of 2.
use std::fs;
use std::process::exit;

fn crc32_wrong(data: &[u8]) -> u32 {
    let mut crc: u32 = 0xFFFF_FFFF;
    for &b in data {
        crc ^= b as u32;
        for _ in 0..8 {
            crc = (crc >> 1) ^ (0xEDB8_8320 & (0u32.wrapping_sub(crc & 1)));
        }
    }
    crc // BUG: original returns !crc
}

fn pack(recs: &[(String, String)]) -> Vec<u8> {
    let mut body = Vec::new();
    for (k, v) in recs {
        body.extend_from_slice(&(k.len() as u16).to_le_bytes());
        body.extend_from_slice(&(v.len() as u16).to_le_bytes());
        body.extend_from_slice(k.as_bytes());
        body.extend_from_slice(v.as_bytes());
    }
    body
}

fn save(path: &str, recs: &[(String, String)]) {
    let body = pack(recs);
    let mut out = b"PCLI".to_vec();
    out.extend_from_slice(&1u32.to_le_bytes());
    out.extend_from_slice(&(recs.len() as u32).to_le_bytes());
    out.extend_from_slice(&crc32_wrong(&body).to_le_bytes());
    out.extend_from_slice(&body);
    if fs::write(path, out).is_err() {
        eprintln!("error: cannot write '{}'", path);
        exit(5);
    }
}

fn load(path: &str) -> Vec<(String, String)> {
    let data = match fs::read(path) {
        Ok(d) => d,
        Err(_) => {
            eprintln!("error: file not found: {}", path);
            exit(1); // BUG: original exits 3
        }
    };
    if data.len() < 16 || &data[0..4] != b"PCLI" {
        eprintln!("error: bad magic in {} (not a PCLI file)", path);
        exit(1); // BUG: original exits 2
    }
    let count = u32::from_le_bytes(data[8..12].try_into().unwrap());
    let mut pos = 16;
    let mut recs = Vec::new();
    for _ in 0..count {
        if pos + 4 > data.len() {
            eprintln!("error: truncated");
            exit(4);
        }
        let kl = u16::from_le_bytes([data[pos], data[pos + 1]]) as usize;
        let vl = u16::from_le_bytes([data[pos + 2], data[pos + 3]]) as usize;
        let k = String::from_utf8_lossy(&data[pos + 4..pos + 4 + kl]).to_string();
        let v = String::from_utf8_lossy(&data[pos + 4 + kl..pos + 4 + kl + vl]).to_string();
        recs.push((k, v));
        pos += 4 + kl + vl;
    }
    recs
}

fn usage() -> ! {
    eprintln!("usage: pecli <command> <file> [args]");
    exit(1);
}

fn main() {
    let a: Vec<String> = std::env::args().collect();
    if a.len() < 3 {
        usage();
    }
    let path = &a[2];
    match (a[1].as_str(), a.len()) {
        ("init", 3) => {
            save(path, &[]);
            println!("initialized {} (0 records)", path);
        }
        ("add", 5) => {
            let mut r = load(path);
            if let Some(e) = r.iter_mut().find(|e| e.0 == a[3]) {
                e.1 = a[4].clone();
                save(path, &r);
                println!("updated {}", a[3]);
            } else {
                r.push((a[3].clone(), a[4].clone()));
                save(path, &r);
                println!("added {} ({} records)", a[3], r.len());
            }
        }
        ("get", 4) => {
            let r = load(path);
            match r.iter().find(|e| e.0 == a[3]) {
                Some(e) => println!("{}", e.1),
                None => {
                    eprintln!("error: key not found: {}", a[3]);
                    exit(1);
                }
            }
        }
        ("list", 3) => {
            let r = load(path);
            println!("{} records", r.len());
            for (k, v) in r {
                println!("{}={}", k, v);
            }
        }
        ("remove", 4) => {
            let mut r = load(path);
            let n = r.len();
            r.retain(|e| e.0 != a[3]);
            if r.len() == n {
                eprintln!("error: key not found: {}", a[3]);
                exit(1);
            }
            save(path, &r);
            println!("removed {} ({} records)", a[3], r.len());
        }
        ("checksum", 3) => {
            let r = load(path);
            let c = crc32_wrong(&pack(&r));
            println!("stored:   {:08x}", c);
            println!("computed: {:08x}", c);
            println!("records:  {}", r.len());
        }
        _ => usage(),
    }
}
