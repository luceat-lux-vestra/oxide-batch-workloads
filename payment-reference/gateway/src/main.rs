use payment_reference_contract::{Currency, Money};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    // The Gateway intentionally owns only the canonical contract boundary.
    // Billing/PG adapters are wired in later checkpoints.
    let currency = Currency::parse("KRW")?;
    let _probe = Money::new(1, currency)?;
    println!("payment-reference gateway scaffold ready");
    Ok(())
}
