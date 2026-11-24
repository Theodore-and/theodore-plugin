//! `theodore <command …>` on every shell (DESIGN §2.2): one native process, no shell chain. It sends the command to
//! the service as one C1 `call`, streams its output, and exits with its code. With no service it starts one; when one
//! cannot be had it runs the client once itself; before anything is installed the set-up launcher takes over.

use crate::b64;
use crate::conn::Conn;
use crate::frame;
use crate::home::{self, Launch};
use crate::json::Json;
use crate::oneshot;
use crate::service::{self, Connected, Failure, Hello};
use crate::sha256;
use crate::sys;
use std::ffi::OsString;
use std::io::{self, Read, Write};
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// Commands that never go to the service: they remove or set up the installation the service runs from, or give the
/// engine's Python to the terminal itself.
const LOCAL: &[&str] = &["uninstall", "setup", "python", "__bootstrap"];
/// Standard input larger than this is refused (a frame carries at most 16 MiB; base64 adds a third).
const STDIN_MAX: usize = 12 * 1024 * 1024;

fn env_true(name: &str) -> bool {
    home::env_value(name).map(|v| !matches!(v.trim().to_ascii_lowercase().as_str(), "0" | "false" | "no")).unwrap_or(false)
}

fn env_false(name: &str) -> bool {
    home::env_value(name).map(|v| matches!(v.trim().to_ascii_lowercase().as_str(), "0" | "false" | "no")).unwrap_or(false)
}

/// Inside Codex's sandbox a service cannot be started (it would run in the sandbox too); one already running may
/// still be reached.
fn sandboxed() -> bool {
    home::env_value("CODEX_SANDBOX").is_some()
}

pub fn host_of_env() -> &'static str {
    if home::env_value("CLAUDECODE").is_some() || home::env_value("CLAUDE_CODE_ENTRYPOINT").is_some() {
        return "claude-code";
    }
    if ["CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_SANDBOX", "CODEX_HOME"].iter().any(|n| home::env_value(n).is_some()) {
        return "codex";
    }
    if home::env_value("CLAUDE_PLUGIN_ROOT").is_some() {
        return "claude-code";
    }
    "shell"
}

/// The environment overlay every call carries (C1's allow-list), each name that is set here. THEODORE_STDIO is never
/// taken from the environment: the front measures its own streams (`stdio_flags`), the relay sends none.
pub fn overlay(plugin_root: Option<&Path>) -> Json {
    let mut env = Json::obj();
    let mut names: Vec<(String, String)> = std::env::vars_os()
        .filter_map(|(k, v)| Some((k.into_string().ok()?, v.into_string().ok()?)))
        .filter(|(k, _)| {
            matches!(k.as_str(), "CLAUDECODE" | "CLAUDE_CODE_SESSION_ID" | "CLAUDE_PID" | "CLAUDE_PLUGIN_ROOT" | "CODEX_THREAD_ID")
                || (k.starts_with("THEODORE_") && k.len() > 9 && k[9..].bytes().all(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || b == b'_'))
        })
        .filter(|(k, _)| k != "THEODORE_STDIO")
        .collect();
    names.sort();
    for (k, v) in names {
        env.set(&k, Json::Str(v));
    }
    if env.get("CLAUDE_PLUGIN_ROOT").is_none() {
        if let Some(root) = plugin_root {
            env.set("CLAUDE_PLUGIN_ROOT", Json::str(root.to_string_lossy()));
        }
    }
    env
}

pub fn random_id(prefix: &str) -> String {
    let nanos = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
    let local = 0u8;
    let seed = format!("{nanos}:{}:{:p}:{}", std::process::id(), &local, std::thread::current().name().unwrap_or(""));
    format!("{prefix}{}", &sha256::hex_digest(seed.as_bytes())[..16])
}

fn hello() -> Hello {
    let session = ["CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID"].iter().find_map(|n| home::env_value(n));
    Hello {
        role: "front",
        client_name: "theodore-front",
        session_id: session.unwrap_or_else(|| "shell".into()),
        host: host_of_env().into(),
        pid: sys::parent_pid(),
        plugin_root: home::env_value("CLAUDE_PLUGIN_ROOT").or_else(|| home::plugin_root_of_exe().map(|p| p.to_string_lossy().into_owned())),
    }
}

/// The Bash tool's limit minus 30 s (C1 "Deadline"), when an assistant runs this command: past it the service answers
/// "still running" and carries on, and the same command attaches to it.
fn deadline_ms() -> Option<i64> {
    if let Some(ms) = home::env_value("THEODORE_DEADLINE_MS").and_then(|v| v.trim().parse::<i64>().ok()) {
        return (ms >= 1000).then_some(ms);
    }
    if home::env_value("CLAUDECODE").is_none() {
        return None;
    }
    let max = home::env_value("BASH_MAX_TIMEOUT_MS").and_then(|v| v.trim().parse::<i64>().ok()).unwrap_or(600_000);
    Some((max - 30_000).max(1000))
}

/// C1 "The caller's streams": `stdout-tty`, `stderr-tty` (that stream is a terminal) and `shared` (stdout and stderr are
/// one pipe, file or console), comma-separated; empty when none applies.
pub fn stdio_flags() -> String {
    use std::io::IsTerminal;
    let mut flags = Vec::new();
    if io::stdout().is_terminal() {
        flags.push("stdout-tty");
    }
    if io::stderr().is_terminal() {
        flags.push("stderr-tty");
    }
    if sys::stdout_stderr_shared() {
        flags.push("shared");
    }
    flags.join(",")
}

struct Call {
    tool: &'static str,
    args: Vec<String>,
    stdin: Option<Json>,
    /// `skill`: whatever happens, one body on standard output and exit 0 (as the launcher's `skill` always did).
    skill: bool,
}

fn read_stdin() -> Result<Json, String> {
    let mut data = Vec::new();
    io::stdin().take(STDIN_MAX as u64 + 1).read_to_end(&mut data).map_err(|e| format!("standard input: {e}"))?;
    if data.len() > STDIN_MAX {
        return Err(format!("standard input is over {} MiB", STDIN_MAX >> 20));
    }
    Ok(match String::from_utf8(data) {
        Ok(text) => Json::obj().with("encoding", "utf-8".into()).with("data", Json::Str(text)),
        Err(raw) => Json::obj().with("encoding", "base64".into()).with("data", Json::Str(b64::encode(raw.as_bytes()))),
    })
}

impl Call {
    fn of(argv: &[String]) -> Result<Call, String> {
        let (tool, args) = match argv.first().map(String::as_str) {
            Some("pptx") => ("pptx", argv[1..].to_vec()),
            _ => ("theodore", argv.to_vec()),
        };
        let stdin = if args.iter().any(|a| a == "-") { Some(read_stdin()?) } else { None };
        Ok(Call { tool, skill: argv.first().map(String::as_str) == Some("skill"), args, stdin })
    }

    fn frame(&self, id: &str, cwd: &Path, resend: bool) -> Json {
        let mut call = Json::obj()
            .with("t", "call".into())
            .with("id", Json::str(id))
            .with("tool", self.tool.into())
            .with("args", Json::Arr(self.args.iter().map(|a| Json::str(&**a)).collect()))
            .with("cwd", Json::str(cwd.to_string_lossy()))
            .with("env", overlay(home::plugin_root_of_exe().as_deref()).with("THEODORE_STDIO", Json::str(stdio_flags())))
            .with("images", Json::Bool(false));
        if let Some(stdin) = &self.stdin {
            call.set("stdin", stdin.clone());
        }
        if let Some(ms) = deadline_ms() {
            call.set("deadline_ms", Json::int(ms));
        }
        if resend {
            call.set("resend", Json::Bool(true));
        }
        call
    }
}

pub fn main(args: Vec<OsString>) -> i32 {
    let home = home::home();
    let launch = match home::read_launch(&home) {
        Ok(launch) if launch.ready() => launch,
        _ => {
            if env_true("THEODORE_FRONT_SETUP") {
                eprintln!("theodore: Theodore is still being set up; run the same command again in a minute.");
                return 67;
            }
            return oneshot::setup(&home, &args, None);
        }
    };
    let argv: Vec<String> = match args.iter().map(|a| a.clone().into_string()).collect() {
        Ok(argv) => argv,
        Err(_) => return oneshot::client(&launch, &home, &args, None),
    };
    if argv.first().is_some_and(|c| LOCAL.contains(&c.as_str())) || launch.oneshot || env_false("THEODORE_RESIDENT") {
        return oneshot::client(&launch, &home, &args, None);
    }
    let call = match Call::of(&argv) {
        Ok(call) => call,
        Err(why) => {
            eprintln!("theodore: {why}");
            return 2;
        }
    };
    let hello = hello();
    match service::ensure(&home, &launch, &hello, !sandboxed()) {
        Ok(connected) => run(connected, call, &home, &launch, &hello),
        Err(failure) => {
            // Said once, by the command whose start failed; the commands after it (start-failed) go one-shot quietly.
            let note = matches!(failure, Failure::StartFailed(ref why) if !why.starts_with("a start failed"))
                || matches!(failure, Failure::Refused(..));
            let reason = failure.reason();
            oneshot_with(&launch, &home, &args, &call, note.then_some(reason.as_str()))
        }
    }
}

/// The client once, with the standard input already read for the call given back to it.
fn oneshot_with(launch: &Launch, home: &Path, args: &[OsString], call: &Call, note: Option<&str>) -> i32 {
    if call.stdin.is_none() {
        return oneshot::client(launch, home, args, note);
    }
    // The input was read for the service: the one-shot client gets it from a file, named where it said "-".
    let Some(Json::Str(data)) = call.stdin.as_ref().and_then(|s| s.get("data")).cloned() else {
        return oneshot::client(launch, home, args, note);
    };
    let base64 = call.stdin.as_ref().and_then(|s| s.get("encoding")).and_then(Json::as_str) == Some("base64");
    if base64 {
        eprintln!("theodore: standard input that is not text cannot be passed on here; give it as a file.");
        return 2;
    }
    let dir = home.join("state").join("run");
    let _ = std::fs::create_dir_all(&dir);
    let path = dir.join(format!("stdin-{}.txt", random_id("")));
    if std::fs::write(&path, data.as_bytes()).is_err() {
        eprintln!("theodore: standard input could not be kept for the command");
        return 70;
    }
    let replaced: Vec<OsString> = args.iter().map(|a| if a == "-" { path.clone().into_os_string() } else { a.clone() }).collect();
    let code = oneshot::client_waiting(launch, home, &replaced, note);
    let _ = std::fs::remove_file(&path);
    code
}

struct Out {
    stdout: Option<io::Stdout>,
    stderr: io::Stderr,
}

impl Out {
    fn write(&mut self, stream: &str, data: &str, skill: bool) {
        if stream == "stderr" && !skill {
            let _ = self.stderr.write_all(data.as_bytes());
            let _ = self.stderr.flush();
            return;
        }
        if let Some(out) = self.stdout.as_mut() {
            if out.write_all(data.as_bytes()).and_then(|_| out.flush()).is_err() {
                self.stdout = None; // the reader went away (`| head`): the call still runs to its end
            }
        }
    }
}

fn run(connected: Connected, call: Call, home: &Path, launch: &Launch, hello: &Hello) -> i32 {
    let cwd = std::env::current_dir().unwrap_or_else(|_| home.to_path_buf());
    let id = random_id("f-");
    let mut reader = connected.conn;
    let writer: Arc<Mutex<Option<Conn>>> = Arc::new(Mutex::new(reader.try_clone().ok()));
    let cancel_id = id.clone();
    let cancel_writer = Arc::clone(&writer);
    sys::on_interrupt(move || {
        let frame = Json::obj().with("t", "cancel".into()).with("id", Json::str(&*cancel_id));
        if let Some(conn) = cancel_writer.lock().unwrap_or_else(|e| e.into_inner()).as_mut() {
            let _ = frame::write(conn, &frame);
        }
    });
    let mut out = Out { stdout: Some(io::stdout()), stderr: io::stderr() };
    let mut resend = false;
    let mut lost = 0;
    let mut busy = 0;
    'call: loop {
        let sent = {
            let mut guard = writer.lock().unwrap_or_else(|e| e.into_inner());
            match guard.as_mut() {
                Some(conn) => frame::write(conn, &call.frame(&id, &cwd, resend)).is_ok(),
                None => false,
            }
        };
        if sent {
            loop {
                let frame = match frame::read(&mut reader) {
                    Ok(Some(frame)) => frame,
                    Ok(None) | Err(_) => break,
                };
                if frame.get("id").and_then(Json::as_str).is_some_and(|f| f != id) {
                    continue;
                }
                match frame.get("t").and_then(Json::as_str).unwrap_or("") {
                    "out" => {
                        let stream = frame.get("stream").and_then(Json::as_str).unwrap_or("stdout");
                        out.write(stream, frame.get("data").and_then(Json::as_str).unwrap_or(""), call.skill);
                    }
                    "image" => {
                        if let Some(path) = frame.get("path").and_then(Json::as_str) {
                            out.write("stderr", &format!("picture: {path}\n"), false);
                        }
                    }
                    "done" => {
                        let code = frame.get("exit").and_then(Json::as_i64).unwrap_or(70) as i32;
                        return if call.skill && code != 130 { 0 } else { code };
                    }
                    "error" => {
                        let code = frame.get("code").and_then(Json::as_str).unwrap_or("");
                        let message = frame.get("message").and_then(Json::as_str).unwrap_or("");
                        match code {
                            "BUSY" if busy < 20 => {
                                busy += 1;
                                let wait = frame.get("retry_after_ms").and_then(Json::as_i64).unwrap_or(200).clamp(20, 5000);
                                std::thread::sleep(Duration::from_millis(wait as u64));
                                continue 'call;
                            }
                            "SHUTTING_DOWN" => break,
                            _ => {
                                out.write("stderr", &format!("theodore: the service refused the command ({code}: {message})\n"), call.skill);
                                return if call.skill { 0 } else { 70 };
                            }
                        }
                    }
                    _ => {} // progress and notices: the shell shows the answer only
                }
            }
        }
        // The service went away during the call (an update's hand-over or a crash): reach it again, or start it, and
        // resend the call with resend: true (C1 "Updates and crashes").
        lost += 1;
        if lost > 3 {
            break;
        }
        let again = service::reconnect(home, hello, Duration::from_secs(3)).map(Ok).unwrap_or_else(|| service::ensure(home, launch, hello, !sandboxed()));
        match again {
            Ok(found) => {
                reader = found.conn;
                *writer.lock().unwrap_or_else(|e| e.into_inner()) = reader.try_clone().ok();
                resend = true;
            }
            Err(_) => break,
        }
    }
    out.write(
        "stderr",
        "theodore: Theodore's service stopped during this command and could not be reached again; nothing was half-written. Run the same command again.\n",
        call.skill,
    );
    if call.skill {
        0
    } else {
        75
    }
}

/// `theodore __front version`: what this program is (for doctor and the tests); never reaches the service.
pub fn diagnostics(args: &[OsString]) -> i32 {
    let home = home::home();
    let launch = home::read_launch(&home);
    let service = home::read_service(&home);
    let doc = Json::obj()
        // The client knows its own copy of this program by these words (commands._shim_ours).
        .with("what", "Theodore's own short command, and the plugin's MCP relay".into())
        .with("front", crate::VERSION.into())
        .with("protocol", Json::int(service::PROTOCOL))
        .with("home", Json::str(home.to_string_lossy()))
        .with("exe", Json::str(std::env::current_exe().map(|p| p.to_string_lossy().into_owned()).unwrap_or_default()))
        .with("launch", match &launch {
            Ok(l) => Json::obj()
                .with("ready", Json::Bool(l.ready()))
                .with("oneshot", Json::Bool(l.oneshot))
                .with("client", Json::str(&*l.client_version)),
            Err(why) => Json::obj().with("error", Json::str(&**why)),
        })
        .with("service", match &service {
            Some(s) => Json::obj().with("pid", Json::int(s.pid)).with("version", Json::str(&*s.version)),
            None => Json::Null,
        })
        .with("id16", Json::str(home::id16(&home)))
        .with("start_failed", home::start_failed_recently(&home).map(Json::Str).unwrap_or(Json::Null));
    let what = args.first().and_then(|a| a.to_str()).unwrap_or("version");
    if what != "version" {
        eprintln!("theodore: __front knows only `version`");
        return 2;
    }
    println!("{}", doc.dump());
    0
}
