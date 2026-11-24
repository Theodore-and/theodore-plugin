//! What differs between macOS and Linux on one side and Windows on the other: the user's id, the start lock, the
//! detached start of the service, the named-pipe client, and Ctrl+C.

#[cfg(unix)]
mod unix;
#[cfg(unix)]
pub use unix::*;

#[cfg(windows)]
mod windows;
#[cfg(windows)]
pub use self::windows::*;
