//! benchrs: matrix and word-frequency CLI used as R0 benchmark ground truth (Rebuild Studio fixtures/bench).
//! usage: benchrs <freq|matrix|primes> [args...]
//! Exit codes: 0 ok, 1 usage, 2 parse error. Written for this repository.
use std::collections::BTreeMap;
use std::env;
use std::fmt;
use std::process;

const USAGE: &str = "benchrs 1.0 - Rebuild Studio benchmark fixture\nusage: benchrs <freq <words...>|matrix <n>|primes <limit>>";

#[derive(Debug)]
enum BenchError {
    Usage,
    Parse(String),
}

impl fmt::Display for BenchError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            BenchError::Usage => write!(f, "{}", USAGE),
            BenchError::Parse(s) => write!(f, "benchrs: cannot parse '{}' as a number", s),
        }
    }
}

struct Matrix {
    n: usize,
    cells: Vec<i64>,
}

impl Matrix {
    #[inline(never)]
    fn identity_plus(n: usize) -> Matrix {
        let mut cells = vec![0i64; n * n];
        for i in 0..n {
            for j in 0..n {
                cells[i * n + j] = if i == j { 2 } else { ((i + j) % 3) as i64 };
            }
        }
        Matrix { n, cells }
    }

    #[inline(never)]
    fn multiply(&self, other: &Matrix) -> Matrix {
        let n = self.n;
        let mut cells = vec![0i64; n * n];
        for i in 0..n {
            for k in 0..n {
                let a = self.cells[i * n + k];
                for j in 0..n {
                    cells[i * n + j] = cells[i * n + j].wrapping_add(a.wrapping_mul(other.cells[k * n + j]));
                }
            }
        }
        Matrix { n, cells }
    }

    #[inline(never)]
    fn trace(&self) -> i64 {
        (0..self.n).map(|i| self.cells[i * self.n + i]).sum()
    }
}

#[inline(never)]
fn word_frequencies(words: &[String]) -> BTreeMap<String, usize> {
    let mut map = BTreeMap::new();
    for w in words {
        let key: String = w.chars().filter(|c| c.is_alphanumeric()).flat_map(|c| c.to_lowercase()).collect();
        if !key.is_empty() {
            *map.entry(key).or_insert(0) += 1;
        }
    }
    map
}

#[inline(never)]
fn sieve(limit: usize) -> Vec<usize> {
    let mut is = vec![true; limit + 1];
    let mut out = Vec::new();
    for i in 2..=limit {
        if is[i] {
            out.push(i);
            let mut j = i * i;
            while j <= limit {
                is[j] = false;
                j += i;
            }
        }
    }
    out
}

#[inline(never)]
fn parse_usize(s: &str) -> Result<usize, BenchError> {
    s.parse::<usize>().map_err(|_| BenchError::Parse(s.to_string()))
}

#[inline(never)]
fn run(args: &[String]) -> Result<(), BenchError> {
    let cmd = args.first().ok_or(BenchError::Usage)?;
    match cmd.as_str() {
        "freq" => {
            for (w, n) in word_frequencies(&args[1..]) {
                println!("{:>4} {}", n, w);
            }
        }
        "matrix" => {
            let n = parse_usize(args.get(1).ok_or(BenchError::Usage)?)?.min(64);
            let m = Matrix::identity_plus(n);
            let p = m.multiply(&m).multiply(&m);
            println!("matrix n={} trace(m^3)={}", n, p.trace());
        }
        "primes" => {
            let limit = parse_usize(args.get(1).ok_or(BenchError::Usage)?)?.min(1_000_000);
            let ps = sieve(limit);
            println!("primes<={} count={} last={:?}", limit, ps.len(), ps.last());
        }
        _ => return Err(BenchError::Usage),
    }
    Ok(())
}

fn main() {
    let args: Vec<String> = env::args().skip(1).collect();
    if let Err(e) = run(&args) {
        eprintln!("{}", e);
        process::exit(match e {
            BenchError::Usage => 1,
            BenchError::Parse(_) => 2,
        });
    }
}
