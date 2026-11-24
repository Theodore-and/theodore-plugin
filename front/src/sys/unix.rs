//! macOS and Linux: a few calls of the C library, declared here (the same names and values on both).

use std::ffi::OsString;
use std::fs::{self, File};
use std::io::{self, Read};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicI32, AtomicU32, Ordering};
use std::thread;
use std::time::{Duration, Instant};

extern "C" {
    fn getuid() -> u32;
    fn getppid() -> i32;
    fn setsid() -> i32;
    fn fork() -> i32;
    fn _exit(code: i32) -> !;
    fn signal(signum: i32, handler: usize) -> usize;
    fn write(fd: i32, buf: *const u8, count: usize) -> isize;
}

const SIGINT: i32 = 2;

pub fn user_id() -> String {
    unsafe { getuid() }.to_string()
}

pub fn parent_pid() -> Option<u32> {
    u32::try_from(unsafe { getppid() }).ok()
}

pub fn simplify(path: PathBuf) -> PathBuf {
    path
}

/// The file belongs to this user (service.json is the service's, written 0600 in a 0700 folder).
pub fn owned_by_me(path: &Path) -> bool {
    fs::metadata(path).map(|m| m.uid() == unsafe { getuid() }).unwrap_or(false)
}

/// C1's start lock: `flock` on `<home>/state/run/start.lock`, held until dropped.
pub struct StartLock {
    _file: File,
}

impl StartLock {
    pub fn acquire(home: &Path, _id16: &str, timeout: Duration) -> io::Result<StartLock> {
        Ok(StartLock { _file: super::lock_start_file(home, Instant::now() + timeout)? })
    }
}

/// The run folder is this user's alone (0700).
pub fn restrict_folder(path: &Path) {
    let _ = fs::set_permissions(path, fs::Permissions::from_mode(0o700));
}

/// Starts `program args` in a session of its own and as nobody's child (a double fork: the first child starts a new
/// session, forks the program and exits), with no standard streams, from `cwd`. A host that stops the command that
/// started it, with the whole process tree, never stops the service.
pub fn spawn_detached(program: &Path, args: &[OsString], cwd: &Path, set: &[(String, OsString)], remove: &[String]) -> io::Result<()> {
    let mut cmd = Command::new(program);
    cmd.args(args).current_dir(cwd).stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
    for name in remove {
        cmd.env_remove(name);
    }
    for (name, value) in set {
        cmd.env(name, value);
    }
    unsafe {
        cmd.pre_exec(|| {
            setsid();
            match fork() {
                -1 => Err(io::Error::last_os_error()),
                0 => Ok(()),
                _ => _exit(0),
            }
        });
    }
    let mut child = cmd.spawn()?;
    let _ = child.wait();
    Ok(())
}

static SIGNAL_FD: AtomicI32 = AtomicI32::new(-1);
static SIGNALS: AtomicU32 = AtomicU32::new(0);

extern "C" fn on_sigint(_signum: i32) {
    if SIGNALS.fetch_add(1, Ordering::SeqCst) > 0 {
        unsafe { _exit(130) }
    }
    let fd = SIGNAL_FD.load(Ordering::SeqCst);
    if fd >= 0 {
        let byte = 1u8;
        unsafe { write(fd, &byte, 1) };
    }
}

/// Ctrl+C calls `cancel` once (from a thread of its own, never inside the signal); a second Ctrl+C ends the program
/// with 130 at once.
pub fn on_interrupt<F: Fn() + Send + Sync + 'static>(cancel: F) {
    let Ok((mut reader, writer)) = io::pipe() else { return };
    SIGNAL_FD.store(writer.as_raw_fd(), Ordering::SeqCst);
    std::mem::forget(writer);
    thread::spawn(move || {
        let mut byte = [0u8; 1];
        if matches!(reader.read(&mut byte), Ok(1)) {
            cancel();
        }
    });
    unsafe { signal(SIGINT, on_sigint as extern "C" fn(i32) as usize) };
}

/// Standard output and standard error are one pipe, file or terminal (the same device and inode).
pub fn stdout_stderr_shared() -> bool {
    use std::os::fd::AsFd;
    let identity = |fd: std::os::fd::BorrowedFd<'_>| -> Option<(u64, u64)> {
        let file = File::from(fd.try_clone_to_owned().ok()?);
        let meta = file.metadata().ok()?;
        Some((meta.dev(), meta.ino()))
    };
    match (identity(io::stdout().as_fd()), identity(io::stderr().as_fd())) {
        (Some(out), Some(err)) => out == err,
        _ => false,
    }
}

/// The Windows start helper does not exist here.
pub fn detach_main(_args: &[OsString]) -> i32 {
    eprintln!("theodore: --theodore-detach is for Windows only");
    2
}
