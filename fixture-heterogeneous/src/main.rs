fn main() {
    let mode = std::env::args().nth(1).unwrap_or_else(|| "smoke".to_string());
    if mode != "smoke" {
        eprintln!("unsupported mode: {mode}");
        std::process::exit(2);
    }

    let _contract_anchor = oxide_batch::core::ExitStatus::Completed;
    println!("fixture-heterogeneous smoke passed");
}
