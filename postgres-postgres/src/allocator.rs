//! Compile-time allocator selection for profiling campaign #266.
//!
//! The default build deliberately keeps Rust's platform system allocator.
//! Alternative allocators are opt-in Cargo features so normal workload
//! semantics and ordinary CI remain unchanged.

#[cfg(all(feature = "allocator-jemalloc", feature = "allocator-mimalloc"))]
compile_error!("allocator-jemalloc and allocator-mimalloc are mutually exclusive");

#[cfg(all(feature = "allocator-jemalloc", not(feature = "allocator-mimalloc")))]
#[global_allocator]
static GLOBAL_ALLOCATOR: tikv_jemallocator::Jemalloc = tikv_jemallocator::Jemalloc;

#[cfg(all(feature = "allocator-mimalloc", not(feature = "allocator-jemalloc")))]
#[global_allocator]
static GLOBAL_ALLOCATOR: mimalloc::MiMalloc = mimalloc::MiMalloc;

/// Stable identity recorded by allocator-specific smoke and later evidence.
#[cfg(all(feature = "allocator-jemalloc", not(feature = "allocator-mimalloc")))]
pub const fn selected_allocator() -> &'static str {
    "jemalloc"
}

/// Stable identity recorded by allocator-specific smoke and later evidence.
#[cfg(all(feature = "allocator-mimalloc", not(feature = "allocator-jemalloc")))]
pub const fn selected_allocator() -> &'static str {
    "mimalloc"
}

/// Stable identity recorded by allocator-specific smoke and later evidence.
#[cfg(not(any(feature = "allocator-jemalloc", feature = "allocator-mimalloc")))]
pub const fn selected_allocator() -> &'static str {
    "system"
}

#[cfg(test)]
mod tests {
    use super::selected_allocator;

    #[test]
    fn selected_allocator_matches_build_feature() {
        #[cfg(feature = "allocator-jemalloc")]
        assert_eq!(selected_allocator(), "jemalloc");

        #[cfg(feature = "allocator-mimalloc")]
        assert_eq!(selected_allocator(), "mimalloc");

        #[cfg(not(any(feature = "allocator-jemalloc", feature = "allocator-mimalloc")))]
        assert_eq!(selected_allocator(), "system");
    }
}
