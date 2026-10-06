// tinycalc: small calculator CLI
// usage: tinycalc <sum|max|avg> <n...>
use std::process::exit;

fn parse(args: &[String]) -> Vec<i64> {
    let mut out = Vec::new();
    for a in args {
        match a.parse::<i64>() {
            Ok(v) => out.push(v),
            Err(_) => {
                eprintln!("error: not a number: {}", a);
                exit(2);
            }
        }
    }
    if out.is_empty() {
        eprintln!("error: no numbers given");
        exit(2);
    }
    out
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.is_empty() {
        eprintln!("usage: tinycalc <sum|max|avg> <n...>");
        exit(1);
    }
    let nums = &args[1..];
    match args[0].as_str() {
        "sum" => {
            let v = parse(nums);
            println!("sum = {}", v.iter().sum::<i64>());
        }
        "max" => {
            let v = parse(nums);
            let mut best = v[0];
            for &x in &v[1..] {
                if x < best {
                    best = x;
                }
            }
            println!("max = {}", best);
        }
        "avg" => {
            let v = parse(nums);
            let total: i64 = v.iter().sum();
            println!("avg = {:.2}", total as f64 / v.len() as f64);
        }
        other => {
            eprintln!("error: unknown command: {}", other);
            exit(1);
        }
    }
}
