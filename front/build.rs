// The front carries the plugin's version (the `client.version` of its hello, C1): read from the plugin manifest beside
// this crate, so a release never has two numbers to keep in step. THEODORE_FRONT_VERSION overrides it (a build made
// outside this repository); without either, the crate's own version.
use std::{env, fs, path::Path};

fn main() {
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR")).join("../plugins/theodore/.claude-plugin/plugin.json");
    println!("cargo:rerun-if-changed={}", manifest.display());
    println!("cargo:rerun-if-env-changed=THEODORE_FRONT_VERSION");
    let version = env::var("THEODORE_FRONT_VERSION")
        .ok()
        .filter(|v| !v.trim().is_empty())
        .or_else(|| fs::read_to_string(&manifest).ok().and_then(|text| version_of(&text)))
        .unwrap_or_else(|| env::var("CARGO_PKG_VERSION").unwrap_or_default());
    println!("cargo:rustc-env=THEODORE_FRONT_VERSION={}", version.trim());
}

/// The value of the first `"version": "..."` in a small JSON document (the plugin manifest is flat).
fn version_of(text: &str) -> Option<String> {
    let at = text.find("\"version\"")?;
    let rest = &text[at + 9..];
    let rest = &rest[rest.find(':')? + 1..];
    let start = rest.find('"')? + 1;
    let end = start + rest[start..].find('"')?;
    let value = &rest[start..end];
    let ok = !value.is_empty() && value.chars().all(|c| c.is_ascii_alphanumeric() || ".-+".contains(c));
    ok.then(|| value.to_string())
}
