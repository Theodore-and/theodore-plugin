//! Reaching the service (C1 "Endpoint and discovery"): read `service.json`, connect, `hello`; with nobody listening,
//! take the start lock, look again, start the service detached from `launch.json`, and wait for it (20 ms polls, 5 s).

use crate::conn::Conn;
use crate::frame;
use crate::home::{self, Launch};
use crate::json::Json;
use crate::sys;
use std::ffi::OsString;
use std::path::Path;
use std::thread;
use std::time::{Duration, Instant};

pub const PROTOCOL: i64 = 1;
const CONNECT_TIMEOUT: Duration = Duration::from_secs(2);
const WELCOME_TIMEOUT: Duration = Duration::from_secs(10);
const LOCK_WAIT: Duration = Duration::from_secs(10);
const START_WAIT: Duration = Duration::from_secs(5);
const POLL: Duration = Duration::from_millis(20);

/// Variables that belong to one assistant session: the service is every session's, so it never inherits them (each
/// call carries its own in its `env`).
pub const SESSION_VARIABLES: &[&str] = &[
    "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PLUGIN_ROOT", "CODEX_THREAD_ID",
    "CODEX_SESSION_ID", "THEODORE_SKILL_DIR", "THEODORE_LAUNCHER",
];

pub struct Hello {
    pub role: &'static str,
    pub client_name: &'static str,
    pub session_id: String,
    pub host: String,
    pub pid: Option<u32>,
    pub plugin_root: Option<String>,
}

impl Hello {
    pub fn frame(&self, secret: &str) -> Json {
        let mut session = Json::obj().with("id", Json::str(self.session_id.chars().take(128).collect::<String>())).with("host", Json::str(&*self.host));
        if let Some(pid) = self.pid {
            session.set("pid", Json::int(i64::from(pid)));
        }
        let mut hello = Json::obj()
            .with("t", "hello".into())
            .with("v", Json::int(1))
            .with("secret", Json::str(secret))
            .with("role", self.role.into())
            .with("protocol", Json::int(PROTOCOL))
            .with("client", Json::obj().with("name", self.client_name.into()).with("version", crate::VERSION.into()))
            .with("session", session);
        if let Some(root) = &self.plugin_root {
            hello.set("plugin_root", Json::str(&**root));
        }
        hello
    }
}

/// A connection the service has welcomed.
pub struct Connected {
    pub conn: Conn,
}

#[derive(Debug)]
pub enum Failure {
    /// Nobody listens, or the service did not answer its `hello`.
    Unreachable(String),
    /// The service answered and will not serve this client (its `error` frame's code and message).
    Refused(String, String),
    /// The service could not be started.
    StartFailed(String),
}

impl Failure {
    pub fn reason(&self) -> String {
        match self {
            Failure::Unreachable(why) | Failure::StartFailed(why) => why.clone(),
            Failure::Refused(code, message) => format!("{code}: {message}"),
        }
    }
}

/// One attempt with the service `service.json` names now. A wrong secret (a new service replaced the file between
/// the read and the connection) is tried once more with the file read again.
pub fn connect(home: &Path, hello: &Hello) -> Result<Connected, Failure> {
    for attempt in 0..3 {
        let info = home::read_service(home).ok_or_else(|| Failure::Unreachable("no service is running".into()))?;
        let mut conn = Conn::connect(&info.endpoint, CONNECT_TIMEOUT)
            .map_err(|e| Failure::Unreachable(format!("its endpoint does not answer ({e})")))?;
        let _ = conn.set_read_timeout(Some(WELCOME_TIMEOUT));
        frame::write(&mut conn, &hello.frame(&info.secret)).map_err(|e| Failure::Unreachable(format!("hello: {e}")))?;
        let first = match frame::read(&mut conn) {
            Ok(Some(first)) => first,
            Ok(None) => return Err(Failure::Unreachable("the service closed the connection".into())),
            Err(e) => return Err(Failure::Unreachable(format!("no welcome from the service ({e})"))),
        };
        let _ = conn.set_read_timeout(None);
        match first.get("t").and_then(Json::as_str) {
            Some("welcome") => return Ok(Connected { conn }),
            Some("error") => {
                let code = first.get("code").and_then(Json::as_str).unwrap_or("").to_string();
                let message = first.get("message").and_then(Json::as_str).unwrap_or("").to_string();
                match code.as_str() {
                    "BAD_SECRET" if attempt < 2 => thread::sleep(Duration::from_millis(50)),
                    "BUSY" | "SHUTTING_DOWN" if attempt < 2 => {
                        let wait = first.get("retry_after_ms").and_then(Json::as_i64).unwrap_or(200).clamp(20, 3000);
                        thread::sleep(Duration::from_millis(wait as u64));
                    }
                    "BAD_SECRET" | "BUSY" | "SHUTTING_DOWN" => return Err(Failure::Unreachable(format!("{code}: {message}"))),
                    _ => return Err(Failure::Refused(code, message)),
                }
            }
            _ => return Err(Failure::Unreachable("the service's first frame was not a welcome".into())),
        }
    }
    Err(Failure::Unreachable("the service did not take this connection".into()))
}

/// The service, reached or started (C1 "Start"). `may_start` false: only an attempt to reach one already running.
pub fn ensure(home: &Path, launch: &Launch, hello: &Hello, may_start: bool) -> Result<Connected, Failure> {
    match connect(home, hello) {
        Ok(found) => return Ok(found),
        Err(refused @ Failure::Refused(..)) => return Err(refused),
        Err(other) if !may_start => return Err(other),
        Err(_) => {}
    }
    if let Some(reason) = home::start_failed_recently(home) {
        return Err(Failure::StartFailed(format!("a start failed less than {} minutes ago ({reason})", home::START_FAILED_HOLD_S / 60)));
    }
    let _lock = sys::StartLock::acquire(home, &home::id16(home), LOCK_WAIT)
        .map_err(|e| Failure::StartFailed(format!("the start lock: {e}")))?;
    match connect(home, hello) {
        Ok(found) => return Ok(found), // started by whoever held the lock before us
        Err(refused @ Failure::Refused(..)) => return Err(refused),
        Err(_) => {}
    }
    if let Some(reason) = home::start_failed_recently(home) {
        return Err(Failure::StartFailed(format!("a start failed just now ({reason})")));
    }
    if let Err(why) = start(home, launch) {
        home::note_start_failed(home, &why);
        return Err(Failure::StartFailed(why));
    }
    let deadline = Instant::now() + START_WAIT;
    loop {
        thread::sleep(POLL);
        match connect(home, hello) {
            Ok(found) => {
                home::clear_start_failed(home);
                return Ok(found);
            }
            Err(refused @ Failure::Refused(..)) => return Err(refused),
            Err(_) if Instant::now() < deadline => {}
            Err(last) => {
                let why = format!("no service within {} s of its start ({})", START_WAIT.as_secs(), last.reason());
                home::note_start_failed(home, &why);
                return Err(Failure::StartFailed(why));
            }
        }
    }
}

/// After the service went away or announced a hand-over: the endpoint again with backoff (50, 100, 200, 400, 800 ms,
/// then every 800 ms) for up to `budget` (C1 "Updates and crashes").
pub fn reconnect(home: &Path, hello: &Hello, budget: Duration) -> Option<Connected> {
    let deadline = Instant::now() + budget;
    let mut wait = Duration::from_millis(50);
    loop {
        thread::sleep(wait);
        if let Ok(found) = connect(home, hello) {
            return Some(found);
        }
        if Instant::now() >= deadline {
            return None;
        }
        wait = (wait * 2).min(Duration::from_millis(800));
    }
}

/// `<runtime python> -I <client> service --home <home>`, detached, from the home folder, without the session's own
/// variables.
fn start(home: &Path, launch: &Launch) -> Result<(), String> {
    let args: Vec<OsString> =
        vec!["-I".into(), launch.client_dir.clone().into_os_string(), "service".into(), "--home".into(), home.as_os_str().to_owned()];
    let set = vec![("THEODORE_HOME".to_string(), home.as_os_str().to_owned())];
    let remove: Vec<String> = SESSION_VARIABLES.iter().map(|s| s.to_string()).collect();
    sys::spawn_detached(&launch.runtime_python, &args, home, &set, &remove)
        .map_err(|e| format!("{} could not be started: {e}", launch.runtime_python.display()))
}
