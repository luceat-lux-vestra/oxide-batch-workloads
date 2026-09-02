#[test]
fn fixture_smoke_contract_is_exposed() {
    let output = std::process::Command::new(env!("CARGO_BIN_EXE_fixture-heterogeneous"))
        .arg("smoke")
        .output()
        .expect("fixture binary should run");
    assert!(output.status.success());
    let stdout = String::from_utf8(output.stdout).expect("stdout should be utf-8");
    assert!(stdout.contains("smoke passed"));
}
