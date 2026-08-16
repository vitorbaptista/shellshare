//! Build script for shellshare
//!
//! When the `embed-binaries` feature is enabled, this script:
//! 1. Sets the `SHELLSHARE_BINARIES_DIR` env var for `include_bytes!()` macros
//! 2. Validates that all required platform binaries exist
//! 3. Triggers rebuilds when the binaries change

use std::env;
use std::path::PathBuf;

/// Platform binaries that must exist when embed-binaries feature is enabled
#[cfg(feature = "embed-binaries")]
const REQUIRED_BINARIES: &[&str] = &[
    "shellshare-linux-x86_64",
    "shellshare-linux-aarch64",
    "shellshare-windows-x86_64.exe",
    "shellshare-macos-x86_64",
    "shellshare-macos-aarch64",
];

fn main() {
    // Always set the binaries directory path for include_bytes!()
    let manifest_dir = env::var("CARGO_MANIFEST_DIR").unwrap();
    let binaries_dir = PathBuf::from(&manifest_dir).join("binaries");

    println!(
        "cargo:rustc-env=SHELLSHARE_BINARIES_DIR={}",
        binaries_dir.display()
    );

    // When embed-binaries feature is enabled, validate binaries exist
    #[cfg(feature = "embed-binaries")]
    {
        for binary in REQUIRED_BINARIES {
            let binary_path = binaries_dir.join(binary);
            if !binary_path.exists() {
                panic!(
                    "Required binary not found: {}\n\
                     When building with --features embed-binaries, all platform binaries must exist in the binaries/ directory.\n\
                     Expected path: {}",
                    binary,
                    binary_path.display()
                );
            }
            // Trigger rebuild if binary changes
            println!("cargo:rerun-if-changed={}", binary_path.display());
        }
    }

    // Rebuild if binaries directory contents change
    println!("cargo:rerun-if-changed={}", binaries_dir.display());
}
