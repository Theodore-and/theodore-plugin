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

use std::fs::{self, File, OpenOptions};
use std::io;
use std::path::Path;
use std::thread;
use std::time::{Duration, Instant};

/// `<home>/state/run/start.lock`, held exclusively (C1's start lock; the service's own start takes it too, and it is
/// the one lock that is the same file inside and outside the Claude app's package).
pub fn lock_start_file(home: &Path, deadline: Instant) -> io::Result<File> {
    let run = crate::home::run_dir(home);
    fs::create_dir_all(&run)?;
    restrict_folder(&run);
    let file = OpenOptions::new().create(true).read(true).append(true).open(run.join("start.lock"))?;
    loop {
        match file.try_lock() {
            Ok(()) => return Ok(file),
            Err(fs::TryLockError::WouldBlock) => {}
            Err(fs::TryLockError::Error(err)) => return Err(err),
        }
        if Instant::now() >= deadline {
            return Err(io::Error::new(io::ErrorKind::TimedOut, "another Theodore command is starting the service"));
        }
        thread::sleep(Duration::from_millis(20));
    }
}
