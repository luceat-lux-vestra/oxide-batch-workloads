//! Compile-time allocator selection for profiling campaign #266.
//!
//! The default build deliberately keeps Rust's platform system allocator.
//! Alternative allocators are opt-in Cargo features so normal workload
//! semantics and ordinary CI remain unchanged.

#[cfg(all(feature = "allocator-jemalloc", feature = "allocator-mimalloc"))]
compile_error!("allocator-jemalloc and allocator-mimalloc are mutually exclusive");
#[cfg(all(feature = "allocator-jemalloc", feature = "allocator-snmalloc"))]
compile_error!("allocator-jemalloc and allocator-snmalloc are mutually exclusive");
#[cfg(all(feature = "allocator-mimalloc", feature = "allocator-snmalloc"))]
compile_error!("allocator-mimalloc and allocator-snmalloc are mutually exclusive");

#[cfg(all(
    feature = "allocator-jemalloc",
    not(any(feature = "allocator-mimalloc", feature = "allocator-snmalloc"))
))]
#[global_allocator]
static GLOBAL_ALLOCATOR: tikv_jemallocator::Jemalloc = tikv_jemallocator::Jemalloc;

#[cfg(all(
    feature = "allocator-mimalloc",
    not(any(feature = "allocator-jemalloc", feature = "allocator-snmalloc"))
))]
#[global_allocator]
static GLOBAL_ALLOCATOR: mimalloc::MiMalloc = mimalloc::MiMalloc;

#[cfg(all(
    feature = "allocator-snmalloc",
    not(any(feature = "allocator-jemalloc", feature = "allocator-mimalloc"))
))]
#[global_allocator]
static GLOBAL_ALLOCATOR: snmalloc_rs::SnMalloc = snmalloc_rs::SnMalloc;

/// Stable identity recorded by allocator-specific smoke and later evidence.
#[cfg(all(
    feature = "allocator-jemalloc",
    not(any(feature = "allocator-mimalloc", feature = "allocator-snmalloc"))
))]
pub const fn selected_allocator() -> &'static str {
    "jemalloc"
}

/// Stable identity recorded by allocator-specific smoke and later evidence.
#[cfg(all(
    feature = "allocator-mimalloc",
    not(any(feature = "allocator-jemalloc", feature = "allocator-snmalloc"))
))]
pub const fn selected_allocator() -> &'static str {
    "mimalloc"
}

/// Stable identity recorded by allocator-specific smoke and later evidence.
#[cfg(all(
    feature = "allocator-snmalloc",
    not(any(feature = "allocator-jemalloc", feature = "allocator-mimalloc"))
))]
pub const fn selected_allocator() -> &'static str {
    "snmalloc"
}

/// Stable identity recorded by allocator-specific smoke and later evidence.
#[cfg(not(any(
    feature = "allocator-jemalloc",
    feature = "allocator-mimalloc",
    feature = "allocator-snmalloc"
)))]
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

        #[cfg(feature = "allocator-snmalloc")]
        assert_eq!(selected_allocator(), "snmalloc");

        #[cfg(not(any(
            feature = "allocator-jemalloc",
            feature = "allocator-mimalloc",
            feature = "allocator-snmalloc"
        )))]
        assert_eq!(selected_allocator(), "system");
    }
}
