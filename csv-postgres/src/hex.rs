//! Lowercase hex encoding for digest output.
//!
//! `digest` 0.11 dropped its output type's `LowerHex` impl (which used to
//! make `format!("{:x}", hasher.finalize())` work) in favor of a plain
//! fixed-size `Array` with no such trait. This crate now owns the encoding
//! step instead of borrowing it from the hasher's output type, so every
//! digest consumer (`generator::sha256_of_file`, `verify`'s source/database
//! digests) renders the same lowercase hex this codebase has always used.

use std::fmt::Write;

/// Renders `bytes` as lowercase hexadecimal, e.g. for a SHA-256 digest.
pub fn hex_digest(bytes: &[u8]) -> String {
    let mut hex = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        let _ = write!(hex, "{byte:02x}");
    }
    hex
}

#[cfg(test)]
mod tests {
    use super::hex_digest;
    use sha2::{Digest, Sha256};

    /// Known-answer test for the `sha2` 0.10 -> 0.11 migration: NIST's
    /// standard SHA-256 test vector for the ASCII string "abc"
    /// (<https://csrc.nist.gov/projects/cryptographic-algorithm-validation-program/secure-hashing>),
    /// cross-checked here against `shasum -a 256`. This verifies the actual
    /// digest value against an external, independently known-correct
    /// answer -- not merely that two calls to this crate's own
    /// implementation agree with each other -- so a migration that silently
    /// swapped in the wrong algorithm or a byte-order bug would fail this
    /// test. No fallible operation is involved (hashing an in-memory byte
    /// slice cannot fail), so this test needs no `unwrap`/`expect`.
    #[test]
    fn known_answer_sha256_of_abc() {
        let mut hasher = Sha256::new();
        hasher.update(b"abc");
        let digest = hex_digest(&hasher.finalize());

        assert_eq!(
            digest,
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    /// Same known-answer input, but fed through `update` in three separate
    /// calls instead of one -- proving the streaming/multi-`update`
    /// contract (which `generator::sha256_of_file` and `verify`'s digest
    /// functions rely on to stay bounded-memory) produces the exact same
    /// digest as a single call, not just *some* stable digest.
    #[test]
    fn known_answer_sha256_of_abc_via_multiple_streamed_updates() {
        let mut hasher = Sha256::new();
        hasher.update(b"a");
        hasher.update(b"b");
        hasher.update(b"c");
        let digest = hex_digest(&hasher.finalize());

        assert_eq!(
            digest,
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    /// `hex_digest` must render lowercase hex (the contract `VerifyReport`
    /// and every other digest consumer in this codebase depends on), never
    /// uppercase or another encoding.
    #[test]
    fn hex_digest_is_lowercase() {
        let digest = hex_digest(&[0xAB, 0xCD, 0xEF, 0x01]);
        assert_eq!(digest, "abcdef01");
    }
}
