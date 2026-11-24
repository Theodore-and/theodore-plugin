//! Theodore's native front (DESIGN §2.1, §2.2; contract C1).
//!
//! - `theodore mcp [--for codex|claude-desktop] [--plugin-root <dir>]`: the plugin's MCP server, relaying to the
//!   service;
//! - `theodore <command …>`: the command on every shell, one process, no shell chain;
//! - `theodore __front version`: what this program is.

mod b64;
mod conn;
mod frame;
mod front;
mod home;
mod json;
mod oneshot;
mod relay;
mod service;
mod sha256;
mod sys;

use std::ffi::OsString;

pub const VERSION: &str = env!("THEODORE_FRONT_VERSION");

fn main() {
    let args: Vec<OsString> = std::env::args_os().skip(1).collect();
    let code = match args.first().and_then(|a| a.to_str()) {
        Some("mcp") => relay::main(relay::Opts::parse(&args[1..])),
        Some("--theodore-detach") => sys::detach_main(&args[1..]),
        Some("__front") => front::diagnostics(&args[1..]),
        _ => front::main(args),
    };
    std::process::exit(code);
}
