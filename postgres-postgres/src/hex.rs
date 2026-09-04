//! Minimal hex encoding, since `digest` 0.11's fixed-size output `Array`
//! dropped its `LowerHex` impl.

use std::fmt::Write as _;

pub fn hex_digest(bytes: &[u8]) -> String {
    bytes.iter().fold(String::new(), |mut hex, byte| {
        let _ = write!(hex, "{byte:02x}");
        hex
    })
}
