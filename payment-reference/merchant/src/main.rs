use payment_reference_contract::{Currency, Money};

fn main() -> Result<(), Box<dyn std::error::Error>> {
    // HTTP and persistence arrive in the next bounded checkpoint. Keeping a
    // real binary here makes the reference-system ownership boundary explicit
    // without pretending the online path is implemented already.
    let currency = Currency::parse("KRW")?;
    let _probe = Money::new(1, currency)?;
    println!("payment-reference merchant scaffold ready");
    Ok(())
}
