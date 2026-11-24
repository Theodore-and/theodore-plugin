//! Windows: the calls of kernel32 and advapi32 the front needs, declared here.
//!
//! - the user's SID (C1's id16), the named start mutex `Local\theodore-start-<id16>`;
//! - a named-pipe client opened for overlapped I/O, so one thread reads while another writes (a synchronous handle
//!   serialises them, and a cancel could never be sent while a read waits);
//! - the service started detached (`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB |
//!   CREATE_NO_WINDOW`, again without the breakaway when the job forbids it) by a helper copy of this program that
//!   exits at once, so the service is nobody's child; it inherits only NUL for its three standard streams;
//! - Ctrl+C.

#![allow(non_snake_case, clippy::upper_case_acronyms)]

use std::ffi::{c_void, OsStr, OsString};
use std::io::{self, Read, Write};
use std::os::windows::ffi::OsStrExt;
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::ptr::{null, null_mut};
use std::sync::atomic::{AtomicU32, Ordering};
use std::sync::OnceLock;
use std::time::{Duration, Instant};

type HANDLE = *mut c_void;
type BOOL = i32;

#[repr(C)]
struct OVERLAPPED {
    internal: usize,
    internal_high: usize,
    offset: u32,
    offset_high: u32,
    h_event: HANDLE,
}

#[repr(C)]
struct SECURITY_ATTRIBUTES {
    n_length: u32,
    lp_security_descriptor: *mut c_void,
    b_inherit_handle: BOOL,
}

#[repr(C)]
struct STARTUPINFOW {
    cb: u32,
    lp_reserved: *mut u16,
    lp_desktop: *mut u16,
    lp_title: *mut u16,
    dw_x: u32,
    dw_y: u32,
    dw_x_size: u32,
    dw_y_size: u32,
    dw_x_count_chars: u32,
    dw_y_count_chars: u32,
    dw_fill_attribute: u32,
    dw_flags: u32,
    w_show_window: u16,
    cb_reserved2: u16,
    lp_reserved2: *mut u8,
    h_std_input: HANDLE,
    h_std_output: HANDLE,
    h_std_error: HANDLE,
}

#[repr(C)]
struct STARTUPINFOEXW {
    startup_info: STARTUPINFOW,
    lp_attribute_list: *mut c_void,
}

#[repr(C)]
struct PROCESS_INFORMATION {
    h_process: HANDLE,
    h_thread: HANDLE,
    dw_process_id: u32,
    dw_thread_id: u32,
}

#[repr(C)]
struct SID_AND_ATTRIBUTES {
    sid: *mut c_void,
    attributes: u32,
}

#[link(name = "kernel32")]
extern "system" {
    fn CreateFileW(name: *const u16, access: u32, share: u32, sa: *mut SECURITY_ATTRIBUTES, disposition: u32, flags: u32, template: HANDLE) -> HANDLE;
    fn ReadFile(h: HANDLE, buf: *mut u8, len: u32, read: *mut u32, ov: *mut OVERLAPPED) -> BOOL;
    fn WriteFile(h: HANDLE, buf: *const u8, len: u32, written: *mut u32, ov: *mut OVERLAPPED) -> BOOL;
    fn GetOverlappedResult(h: HANDLE, ov: *mut OVERLAPPED, transferred: *mut u32, wait: BOOL) -> BOOL;
    fn CancelIoEx(h: HANDLE, ov: *mut OVERLAPPED) -> BOOL;
    fn CreateEventW(sa: *mut SECURITY_ATTRIBUTES, manual_reset: BOOL, initial: BOOL, name: *const u16) -> HANDLE;
    fn WaitForSingleObject(h: HANDLE, ms: u32) -> u32;
    fn CloseHandle(h: HANDLE) -> BOOL;
    fn GetLastError() -> u32;
    fn WaitNamedPipeW(name: *const u16, ms: u32) -> BOOL;
    fn CreateMutexW(sa: *mut SECURITY_ATTRIBUTES, initial_owner: BOOL, name: *const u16) -> HANDLE;
    fn ReleaseMutex(h: HANDLE) -> BOOL;
    fn GetCurrentProcess() -> HANDLE;
    fn CreateProcessW(app: *const u16, cmd: *mut u16, pa: *mut SECURITY_ATTRIBUTES, ta: *mut SECURITY_ATTRIBUTES, inherit: BOOL,
                      flags: u32, env: *mut c_void, cwd: *const u16, si: *mut STARTUPINFOEXW, pi: *mut PROCESS_INFORMATION) -> BOOL;
    fn InitializeProcThreadAttributeList(list: *mut c_void, count: u32, flags: u32, size: *mut usize) -> BOOL;
    fn UpdateProcThreadAttribute(list: *mut c_void, flags: u32, attribute: usize, value: *mut c_void, size: usize,
                                 previous: *mut c_void, returned: *mut usize) -> BOOL;
    fn DeleteProcThreadAttributeList(list: *mut c_void);
    fn SetConsoleCtrlHandler(handler: Option<unsafe extern "system" fn(u32) -> BOOL>, add: BOOL) -> BOOL;
    fn LocalFree(mem: *mut c_void) -> *mut c_void;
    fn ExitProcess(code: u32) -> !;
}

#[link(name = "advapi32")]
extern "system" {
    fn OpenProcessToken(process: HANDLE, access: u32, token: *mut HANDLE) -> BOOL;
    fn GetTokenInformation(token: HANDLE, class: u32, info: *mut c_void, len: u32, returned: *mut u32) -> BOOL;
    fn ConvertSidToStringSidW(sid: *mut c_void, out: *mut *mut u16) -> BOOL;
}

const GENERIC_READ: u32 = 0x8000_0000;
const GENERIC_WRITE: u32 = 0x4000_0000;
const FILE_SHARE_READ: u32 = 1;
const FILE_SHARE_WRITE: u32 = 2;
const OPEN_EXISTING: u32 = 3;
const FILE_FLAG_OVERLAPPED: u32 = 0x4000_0000;
const SECURITY_SQOS_PRESENT: u32 = 0x0010_0000;
const SECURITY_IDENTIFICATION: u32 = 0x0002_0000;
const ERROR_FILE_NOT_FOUND: u32 = 2;
const ERROR_ACCESS_DENIED: u32 = 5;
const ERROR_HANDLE_EOF: u32 = 38;
const ERROR_BROKEN_PIPE: u32 = 109;
const ERROR_PIPE_BUSY: u32 = 231;
const ERROR_NO_DATA: u32 = 232;
const ERROR_PIPE_NOT_CONNECTED: u32 = 233;
const ERROR_MORE_DATA: u32 = 234;
const ERROR_OPERATION_ABORTED: u32 = 995;
const ERROR_IO_PENDING: u32 = 997;
const WAIT_OBJECT_0: u32 = 0;
const WAIT_ABANDONED: u32 = 0x80;
const WAIT_TIMEOUT: u32 = 0x102;
const INFINITE: u32 = 0xFFFF_FFFF;
const TOKEN_QUERY: u32 = 0x0008;
const TOKEN_USER_CLASS: u32 = 1;
const DETACHED_PROCESS: u32 = 0x0000_0008;
const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;
const CREATE_UNICODE_ENVIRONMENT: u32 = 0x0000_0400;
const EXTENDED_STARTUPINFO_PRESENT: u32 = 0x0008_0000;
const CREATE_BREAKAWAY_FROM_JOB: u32 = 0x0100_0000;
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const STARTF_USESTDHANDLES: u32 = 0x0000_0100;
const PROC_THREAD_ATTRIBUTE_HANDLE_LIST: usize = 0x0002_0002;
const CTRL_C_EVENT: u32 = 0;
const CTRL_BREAK_EVENT: u32 = 1;

fn invalid(h: HANDLE) -> bool {
    h.is_null() || h as isize == -1
}

fn wide(s: &OsStr) -> Vec<u16> {
    s.encode_wide().chain(std::iter::once(0)).collect()
}

fn last_error() -> io::Error {
    io::Error::from_raw_os_error(unsafe { GetLastError() } as i32)
}

/// The user's SID as text (S-1-5-21-...), from this process's token.
pub fn user_id() -> String {
    unsafe {
        let mut token: HANDLE = null_mut();
        if OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) == 0 {
            return String::new();
        }
        let mut len = 0u32;
        GetTokenInformation(token, TOKEN_USER_CLASS, null_mut(), 0, &mut len);
        let mut buf = vec![0u64; (len as usize).div_ceil(8).max(1)];
        let mut text = String::new();
        if GetTokenInformation(token, TOKEN_USER_CLASS, buf.as_mut_ptr() as *mut c_void, len, &mut len) != 0 {
            let user = &*(buf.as_ptr() as *const SID_AND_ATTRIBUTES);
            let mut out: *mut u16 = null_mut();
            if ConvertSidToStringSidW(user.sid, &mut out) != 0 && !out.is_null() {
                let mut n = 0;
                while *out.add(n) != 0 {
                    n += 1;
                }
                text = String::from_utf16_lossy(std::slice::from_raw_parts(out, n));
                LocalFree(out as *mut c_void);
            }
        }
        CloseHandle(token);
        text
    }
}

pub fn parent_pid() -> Option<u32> {
    None
}

/// A canonical path as Python's os.path.realpath writes it: `C:\…`, `\\server\share\…`, never `\\?\`.
pub fn simplify(path: PathBuf) -> PathBuf {
    let text = path.as_os_str().to_string_lossy();
    if let Some(rest) = text.strip_prefix(r"\\?\UNC\") {
        return PathBuf::from(format!(r"\\{rest}"));
    }
    if let Some(rest) = text.strip_prefix(r"\\?\") {
        if rest.as_bytes().get(1) == Some(&b':') {
            return PathBuf::from(rest);
        }
    }
    path
}

pub fn owned_by_me(_path: &Path) -> bool {
    true // the folder is the user's (%USERPROFILE%); the pipe's DACL is the service's protection
}

/// C1's start lock on Windows: the named mutex `Local\theodore-start-<id16>`, released when dropped.
pub struct StartLock {
    h: HANDLE,
}

unsafe impl Send for StartLock {}

impl StartLock {
    pub fn acquire(_home: &Path, id16: &str, timeout: Duration) -> io::Result<StartLock> {
        let name = wide(OsStr::new(&format!("Local\\theodore-start-{id16}")));
        let h = unsafe { CreateMutexW(null_mut(), 0, name.as_ptr()) };
        if invalid(h) {
            return Err(last_error());
        }
        let ms = timeout.as_millis().min(u128::from(INFINITE - 1)) as u32;
        match unsafe { WaitForSingleObject(h, ms) } {
            WAIT_OBJECT_0 | WAIT_ABANDONED => Ok(StartLock { h }),
            WAIT_TIMEOUT => {
                unsafe { CloseHandle(h) };
                Err(io::Error::new(io::ErrorKind::TimedOut, "another Theodore command is starting the service"))
            }
            _ => {
                let err = last_error();
                unsafe { CloseHandle(h) };
                Err(err)
            }
        }
    }
}

impl Drop for StartLock {
    fn drop(&mut self) {
        unsafe {
            ReleaseMutex(self.h);
            CloseHandle(self.h);
        }
    }
}

/// A named-pipe client handle opened for overlapped I/O: reads and writes from different threads at once.
pub struct Pipe {
    h: HANDLE,
    read_timeout_ms: AtomicU32,
}

unsafe impl Send for Pipe {}
unsafe impl Sync for Pipe {}

impl Pipe {
    pub fn connect(name: &str, timeout: Duration) -> io::Result<Pipe> {
        let path = wide(OsStr::new(name));
        let deadline = Instant::now() + timeout;
        loop {
            let h = unsafe {
                CreateFileW(path.as_ptr(), GENERIC_READ | GENERIC_WRITE, 0, null_mut(), OPEN_EXISTING,
                            FILE_FLAG_OVERLAPPED | SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION, null_mut())
            };
            if !invalid(h) {
                return Ok(Pipe { h, read_timeout_ms: AtomicU32::new(0) });
            }
            let code = unsafe { GetLastError() };
            let left = deadline.saturating_duration_since(Instant::now());
            if code == ERROR_PIPE_BUSY && !left.is_zero() {
                unsafe { WaitNamedPipeW(path.as_ptr(), left.as_millis().clamp(1, 2000) as u32) };
                continue;
            }
            if code == ERROR_FILE_NOT_FOUND {
                return Err(io::Error::new(io::ErrorKind::NotFound, "no Theodore service listens on its pipe"));
            }
            return Err(io::Error::from_raw_os_error(code as i32));
        }
    }

    pub fn set_read_timeout(&self, timeout: Option<Duration>) {
        let ms = timeout.map(|t| t.as_millis().clamp(1, u128::from(INFINITE - 1)) as u32).unwrap_or(0);
        self.read_timeout_ms.store(ms, Ordering::SeqCst);
    }

    /// Ends every read and write waiting on this pipe (a reader thread then sees an error and stops).
    pub fn cancel_all(&self) {
        unsafe { CancelIoEx(self.h, null_mut()) };
    }

    fn io(&self, write: bool, ptr: *mut u8, len: usize) -> io::Result<usize> {
        let len = len.min(u32::MAX as usize) as u32;
        let timeout = if write { 0 } else { self.read_timeout_ms.load(Ordering::SeqCst) };
        unsafe {
            let event = CreateEventW(null_mut(), 1, 0, null());
            if invalid(event) {
                return Err(last_error());
            }
            let mut ov = OVERLAPPED { internal: 0, internal_high: 0, offset: 0, offset_high: 0, h_event: event };
            let started = if write { WriteFile(self.h, ptr, len, null_mut(), &mut ov) } else { ReadFile(self.h, ptr, len, null_mut(), &mut ov) };
            let first = if started == 0 { GetLastError() } else { 0 };
            let mut done = 0u32;
            let outcome = if started == 0 && first != ERROR_IO_PENDING {
                Err(first)
            } else {
                let mut timed_out = false;
                if timeout != 0 && WaitForSingleObject(event, timeout) == WAIT_TIMEOUT {
                    CancelIoEx(self.h, &mut ov);
                    timed_out = true;
                }
                if GetOverlappedResult(self.h, &mut ov, &mut done, 1) != 0 {
                    Ok(done as usize)
                } else {
                    let code = GetLastError();
                    if timed_out && code == ERROR_OPERATION_ABORTED {
                        CloseHandle(event);
                        return Err(io::Error::new(io::ErrorKind::TimedOut, "the Theodore service did not answer in time"));
                    }
                    if code == ERROR_MORE_DATA {
                        Ok(done as usize)
                    } else {
                        Err(code)
                    }
                }
            };
            CloseHandle(event);
            match outcome {
                Ok(n) => Ok(n),
                Err(ERROR_BROKEN_PIPE | ERROR_PIPE_NOT_CONNECTED | ERROR_HANDLE_EOF | ERROR_NO_DATA) if !write => Ok(0),
                Err(ERROR_BROKEN_PIPE | ERROR_PIPE_NOT_CONNECTED | ERROR_NO_DATA) => {
                    Err(io::Error::new(io::ErrorKind::BrokenPipe, "the Theodore service closed its pipe"))
                }
                Err(code) => Err(io::Error::from_raw_os_error(code as i32)),
            }
        }
    }
}

impl Drop for Pipe {
    fn drop(&mut self) {
        unsafe { CloseHandle(self.h) };
    }
}

impl Read for &Pipe {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        self.io(false, buf.as_mut_ptr(), buf.len())
    }
}

impl Write for &Pipe {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.io(true, buf.as_ptr() as *mut u8, buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

/// Starts the service through a helper copy of this program (`--theodore-detach <program> <args…>`), which starts it
/// detached and exits at once: the service's parent is gone, so a host that ends the command's process tree never
/// ends it. The environment is prepared here and inherited by both.
pub fn spawn_detached(program: &Path, args: &[OsString], cwd: &Path, set: &[(String, OsString)], remove: &[String]) -> io::Result<()> {
    let me = std::env::current_exe()?;
    let mut cmd = Command::new(me);
    cmd.arg("--theodore-detach").arg(program).args(args).current_dir(cwd)
        .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::piped())
        .creation_flags(CREATE_NO_WINDOW);
    for name in remove {
        cmd.env_remove(name);
    }
    for (name, value) in set {
        cmd.env(name, value);
    }
    let out = cmd.output()?;
    if out.status.success() {
        return Ok(());
    }
    let text = String::from_utf8_lossy(&out.stderr).trim().to_string();
    Err(io::Error::new(io::ErrorKind::Other, if text.is_empty() { "the service could not be started".to_string() } else { text }))
}

/// The helper's own work: start `<program> <args…>` detached from the current folder, then exit (0 when started).
pub fn detach_main(args: &[OsString]) -> i32 {
    let Some((program, rest)) = args.split_first() else {
        eprintln!("theodore: --theodore-detach needs a program");
        return 2;
    };
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    match create_detached(Path::new(program), rest, &cwd) {
        Ok(()) => 0,
        Err(err) => {
            eprintln!("the service could not be started: {err}");
            1
        }
    }
}

fn quote(arg: &OsStr, out: &mut Vec<u16>) {
    let text: Vec<u16> = arg.encode_wide().collect();
    let plain = !text.is_empty() && !text.iter().any(|&c| c == b' ' as u16 || c == b'\t' as u16 || c == b'\n' as u16 || c == 0x0b || c == b'"' as u16);
    if plain {
        out.extend_from_slice(&text);
        return;
    }
    out.push(b'"' as u16);
    let mut slashes = 0usize;
    for &c in &text {
        if c == b'\\' as u16 {
            slashes += 1;
            continue;
        }
        if c == b'"' as u16 {
            out.extend(std::iter::repeat(b'\\' as u16).take(slashes * 2 + 1));
        } else {
            out.extend(std::iter::repeat(b'\\' as u16).take(slashes));
        }
        out.push(c);
        slashes = 0;
    }
    out.extend(std::iter::repeat(b'\\' as u16).take(slashes * 2));
    out.push(b'"' as u16);
}

fn create_detached(program: &Path, args: &[OsString], cwd: &Path) -> io::Result<()> {
    let app = wide(program.as_os_str());
    let mut line: Vec<u16> = Vec::new();
    line.push(b'"' as u16);
    line.extend(program.as_os_str().encode_wide());
    line.push(b'"' as u16);
    for arg in args {
        line.push(b' ' as u16);
        quote(arg, &mut line);
    }
    line.push(0);
    let dir = wide(cwd.as_os_str());
    unsafe {
        // NUL for the three standard streams, and that handle the only one the service inherits.
        let mut sa = SECURITY_ATTRIBUTES { n_length: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32, lp_security_descriptor: null_mut(), b_inherit_handle: 1 };
        let nul_name = wide(OsStr::new("NUL"));
        let nul = CreateFileW(nul_name.as_ptr(), GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, &mut sa, OPEN_EXISTING, 0, null_mut());
        if invalid(nul) {
            return Err(last_error());
        }
        let mut size = 0usize;
        InitializeProcThreadAttributeList(null_mut(), 1, 0, &mut size);
        let mut list_mem = vec![0u64; size.div_ceil(8).max(1)];
        let list = list_mem.as_mut_ptr() as *mut c_void;
        if InitializeProcThreadAttributeList(list, 1, 0, &mut size) == 0 {
            let err = last_error();
            CloseHandle(nul);
            return Err(err);
        }
        let mut handles = [nul];
        if UpdateProcThreadAttribute(list, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles.as_mut_ptr() as *mut c_void,
                                     std::mem::size_of::<HANDLE>(), null_mut(), null_mut()) == 0 {
            let err = last_error();
            DeleteProcThreadAttributeList(list);
            CloseHandle(nul);
            return Err(err);
        }
        let mut info: STARTUPINFOEXW = std::mem::zeroed();
        info.startup_info.cb = std::mem::size_of::<STARTUPINFOEXW>() as u32;
        info.startup_info.dw_flags = STARTF_USESTDHANDLES;
        info.startup_info.h_std_input = nul;
        info.startup_info.h_std_output = nul;
        info.startup_info.h_std_error = nul;
        info.lp_attribute_list = list;
        let base = EXTENDED_STARTUPINFO_PRESENT | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT;
        let mut result = Err(io::Error::new(io::ErrorKind::Other, "not started"));
        for flags in [base | CREATE_BREAKAWAY_FROM_JOB, base] {
            let mut pi: PROCESS_INFORMATION = std::mem::zeroed();
            let mut cmdline = line.clone();
            if CreateProcessW(app.as_ptr(), cmdline.as_mut_ptr(), null_mut(), null_mut(), 1, flags, null_mut(), dir.as_ptr(), &mut info, &mut pi) != 0 {
                CloseHandle(pi.h_thread);
                CloseHandle(pi.h_process);
                result = Ok(());
                break;
            }
            let code = GetLastError();
            result = Err(io::Error::from_raw_os_error(code as i32));
            if code != ERROR_ACCESS_DENIED {
                break; // only a job that forbids breaking away is tried again without it
            }
        }
        DeleteProcThreadAttributeList(list);
        CloseHandle(nul);
        result
    }
}

static INTERRUPT: OnceLock<Box<dyn Fn() + Send + Sync>> = OnceLock::new();
static PRESSED: AtomicU32 = AtomicU32::new(0);

unsafe extern "system" fn on_ctrl(kind: u32) -> BOOL {
    if kind != CTRL_C_EVENT && kind != CTRL_BREAK_EVENT {
        return 0;
    }
    if PRESSED.fetch_add(1, Ordering::SeqCst) > 0 {
        ExitProcess(130);
    }
    if let Some(cancel) = INTERRUPT.get() {
        cancel();
    }
    1
}

/// Ctrl+C (or Ctrl+Break) calls `cancel` once; a second one ends the program with 130.
pub fn on_interrupt<F: Fn() + Send + Sync + 'static>(cancel: F) {
    if INTERRUPT.set(Box::new(cancel)).is_ok() {
        unsafe { SetConsoleCtrlHandler(Some(on_ctrl), 1) };
    }
}
