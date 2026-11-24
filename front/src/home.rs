//! Theodore's folder and the two small files the front reads in it: `bin/launch.json` (how to start the service and
//! the one-shot client) and `state/run/service.json` (where the running service listens), both C1.

use crate::json::{self, Json};
use crate::sha256;
use crate::sys;
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

pub fn env_value(name: &str) -> Option<String> {
    env::var(name).ok().filter(|v| !v.trim().is_empty())
}

/// Theodore's folder, by the rule every launcher and the client use: THEODORE_HOME when set; else the folder whose
/// `bin` holds this very program (the copy the client puts in `<home>/bin`, also through a symbolic link); else the
/// platform's default (`~/.theodore`; on Windows `%USERPROFILE%\.theodore`, or `%LOCALAPPDATA%\Theodore` when only
/// that one holds an installation).
pub fn home() -> PathBuf {
    if let Some(value) = env_value("THEODORE_HOME") {
        let path = PathBuf::from(value);
        return if path.is_absolute() { path } else { env::current_dir().map(|d| d.join(&path)).unwrap_or(path) };
    }
    if let Some(found) = home_of_exe() {
        return found;
    }
    default_home()
}

fn home_of_exe() -> Option<PathBuf> {
    let exe = sys::simplify(fs::canonicalize(env::current_exe().ok()?).ok()?);
    let bin = exe.parent()?;
    if bin.file_name()? == "bin" && bin.join("launch.json").is_file() {
        return Some(bin.parent()?.to_path_buf());
    }
    None
}

#[cfg(unix)]
pub fn default_home() -> PathBuf {
    PathBuf::from(env_value("HOME").unwrap_or_else(|| "/".into())).join(".theodore")
}

#[cfg(windows)]
pub fn default_home() -> PathBuf {
    let profile = env_value("USERPROFILE").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("C:\\"));
    let new = profile.join(".theodore");
    let old = env_value("LOCALAPPDATA").map(PathBuf::from).unwrap_or_else(|| profile.join("AppData").join("Local")).join("Theodore");
    if !new.join("bin").join("launcher.json").is_file() && old.join("bin").join("launcher.json").is_file() {
        return old;
    }
    new
}

/// The plugin folder this program was shipped in (`<plugin>/bin/<this>`), when it was: the relay's `--plugin-root`
/// when the host gave none, and where the set-up launcher is found.
pub fn plugin_root_of_exe() -> Option<PathBuf> {
    let exe = sys::simplify(fs::canonicalize(env::current_exe().ok()?).ok()?);
    let root = exe.parent()?.parent()?;
    root.join("skills").join("pptx").join("scripts").is_dir().then(|| root.to_path_buf())
}

pub struct Launch {
    pub runtime_python: PathBuf,
    pub client_dir: PathBuf,
    pub client_version: String,
    pub oneshot: bool,
}

impl Launch {
    /// The runtime and the client are on disk: the service and the one-shot client can start.
    pub fn ready(&self) -> bool {
        self.runtime_python.is_file() && self.client_dir.join("__main__.py").is_file()
    }
}

/// How to start the service and the one-shot client: `<home>/bin/launch.json` (C1 `launch-json`, v 1), written by
/// the client at every install and update; else, for an installation made before it existed, the runtime and client
/// the launchers' pointers name (`runtime/current.txt`, `client/current.txt`), one-shot only (that client has no
/// service to start). An error when neither is there: nothing is installed yet.
pub fn read_launch(home: &Path) -> Result<Launch, String> {
    let path = home.join("bin").join("launch.json");
    match fs::read_to_string(&path) {
        Ok(text) => launch_document(&path, &text),
        Err(_) => pointers(home).ok_or_else(|| format!("{} is not there", path.display())),
    }
}

fn launch_document(path: &Path, text: &str) -> Result<Launch, String> {
    let doc = json::parse(text.trim_start_matches('\u{feff}')).map_err(|e| format!("{}: {e}", path.display()))?;
    if doc.get("v").and_then(Json::as_i64) != Some(1) {
        return Err(format!("{}: not version 1", path.display()));
    }
    let field = |name: &str| doc.get(name).and_then(Json::as_str).map(str::to_string).ok_or(format!("{}: no {name}", path.display()));
    Ok(Launch {
        runtime_python: PathBuf::from(field("runtime_python")?),
        client_dir: PathBuf::from(field("client_dir")?),
        client_version: field("client_version").unwrap_or_default(),
        oneshot: doc.get("oneshot").and_then(Json::as_bool).unwrap_or(false),
    })
}

fn pointers(home: &Path) -> Option<Launch> {
    let first_line = |p: PathBuf| fs::read_to_string(p).ok().map(|t| t.trim().to_string()).filter(|t| !t.is_empty());
    let runtime = first_line(home.join("runtime").join("current.txt"))?;
    let client = first_line(home.join("client").join("current.txt"))?;
    let python = if cfg!(windows) {
        home.join("runtime").join(&runtime).join("python").join("python.exe")
    } else {
        home.join("runtime").join(&runtime).join("python").join("bin").join("python3")
    };
    let launch = Launch { runtime_python: python, client_dir: home.join("client").join(&client), client_version: client, oneshot: true };
    launch.ready().then_some(launch)
}

#[derive(Clone, Debug, PartialEq)]
pub enum Endpoint {
    Pipe(String),
    Unix(PathBuf),
    Tcp(String, u16),
}

#[derive(Clone, Debug)]
pub struct ServiceInfo {
    pub pid: i64,
    pub version: String,
    pub endpoint: Endpoint,
    pub secret: String,
}

pub fn run_dir(home: &Path) -> PathBuf {
    home.join("state").join("run")
}

/// `<home>/state/run/service.json` (C1 `service-json`, v 1), or None when there is none to use: absent, unreadable,
/// another version, or (on macOS and Linux) a file this user does not own.
pub fn read_service(home: &Path) -> Option<ServiceInfo> {
    let path = run_dir(home).join("service.json");
    let text = fs::read_to_string(&path).ok()?;
    if !sys::owned_by_me(&path) {
        return None;
    }
    let doc = json::parse(&text).ok()?;
    if doc.get("v").and_then(Json::as_i64) != Some(1) {
        return None;
    }
    let secret = doc.get("secret")?.as_str()?.to_string();
    if secret.len() != 64 || !secret.bytes().all(|b| b.is_ascii_hexdigit()) {
        return None;
    }
    let ep = doc.get("endpoint")?;
    let address = ep.get("address")?.as_str()?.to_string();
    let endpoint = match ep.get("kind")?.as_str()? {
        "pipe" => Endpoint::Pipe(address),
        "unix" => Endpoint::Unix(PathBuf::from(address)),
        "tcp" => Endpoint::Tcp(address, u16::try_from(ep.get("port")?.as_i64()?).ok()?),
        _ => return None,
    };
    Some(ServiceInfo {
        pid: doc.get("pid").and_then(Json::as_i64).unwrap_or(0),
        version: doc.get("version").and_then(Json::as_str).unwrap_or("").to_string(),
        endpoint,
        secret,
    })
}

/// C1's `id16`: the first 16 hex of sha256(utf8(<user id> + "\n" + <realpath of the home>)); the user id is the SID on
/// Windows and the numeric uid elsewhere. It names the Windows start lock (and the pipe the service makes).
pub fn id16(home: &Path) -> String {
    id16_of(&sys::user_id(), &realpath(home))
}

pub fn id16_of(user: &str, real_home: &Path) -> String {
    let text = format!("{user}\n{}", real_home.to_string_lossy());
    sha256::hex_digest(text.as_bytes())[..16].to_string()
}

/// The folder with every link resolved, as Python's os.path.realpath writes it (no `\\?\` on Windows).
pub fn realpath(path: &Path) -> PathBuf {
    fs::canonicalize(path).map(sys::simplify).unwrap_or_else(|_| path.to_path_buf())
}

pub fn now_secs() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

/// A start that failed (no service within its time, or the runtime refused to start) is remembered for
/// START_FAILED_HOLD_S: the commands meanwhile go one-shot at once instead of each waiting for a start that fails.
pub const START_FAILED_HOLD_S: u64 = 600;

pub fn start_failed_recently(home: &Path) -> Option<String> {
    let text = fs::read_to_string(run_dir(home).join("start-failed")).ok()?;
    let doc = json::parse(&text).ok()?;
    let at = doc.get("at").and_then(Json::as_i64)? as u64;
    let now = now_secs();
    (at <= now && now - at < START_FAILED_HOLD_S).then(|| doc.get("reason").and_then(Json::as_str).unwrap_or("").to_string())
}

pub fn note_start_failed(home: &Path, reason: &str) {
    let dir = run_dir(home);
    if fs::create_dir_all(&dir).is_err() {
        return;
    }
    let doc = Json::obj().with("at", Json::int(now_secs() as i64)).with("reason", Json::str(reason));
    let tmp = dir.join(format!("start-failed.{}.tmp", std::process::id()));
    if fs::write(&tmp, doc.dump()).is_ok() && fs::rename(&tmp, dir.join("start-failed")).is_err() {
        let _ = fs::remove_file(&tmp);
    }
}

pub fn clear_start_failed(home: &Path) {
    let _ = fs::remove_file(run_dir(home).join("start-failed"));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn id16_vectors() {
        // The same vectors are in tests/test_front_contract.py, against the client's Python.
        assert_eq!(id16_of("501", Path::new("/Users/someone/.theodore")), sha256::hex_digest(b"501\n/Users/someone/.theodore")[..16]);
        assert_eq!(id16_of("1000", Path::new("/home/a b/.theodore")).len(), 16);
        assert_eq!(id16_of("S-1-5-21-1-2-3-1001", Path::new("C:\\Users\\a\\.theodore")),
                   sha256::hex_digest("S-1-5-21-1-2-3-1001\nC:\\Users\\a\\.theodore".as_bytes())[..16]);
    }
}
