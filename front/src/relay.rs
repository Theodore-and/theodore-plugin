//! The plugin's MCP server: `theodore mcp [--for codex|claude-desktop] [--plugin-root <dir>]` (DESIGN §2.1, C1 "The
//! relay's mapping"). One per assistant session, shared by every subagent. It reads standard input continuously and
//! never waits on a call: each `tools/call` goes to the service at once as a C1 `call`, and answers go back as they
//! complete, in any order, one message per line under one lock. `initialize`, `ping` and `tools/list` are answered
//! here; the service is reached (or started) in the background after `initialize`.
//!
//! With no service to be had, the client's own MCP server takes the session over (the relay replays `initialize`
//! to it and passes every message through); before anything is installed, the set-up launcher does.

use crate::b64;
use crate::conn::Conn;
use crate::frame;
use crate::front;
use crate::home::{self, Launch};
use crate::json::{self, Json};
use crate::oneshot;
use crate::service::{self, Hello};
use crate::sys;
use std::collections::BTreeMap;
use std::ffi::OsString;
use std::io::{self, BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::thread;
use std::time::{Duration, Instant};

const PLUGIN_TOOLS: &str = include_str!("../assets/mcp-tools.json");
const CHAT_TOOLS: &str = include_str!("../assets/chat-tools.json");
/// The protocol versions the client's own server accepts (mcp.py PROTOCOL_VERSIONS); the first is the default.
const PROTOCOL_VERSIONS: &[&str] = &["2025-06-18", "2025-03-26", "2024-11-05"];
const MAX_PICTURES: usize = 12;
const PICTURE_MAX_BYTES: u64 = 12 * 1024 * 1024;
/// How long `tools/list` waits for the service's list when the static one may be wrong (the plugin's server in the
/// Claude app's chat, where the service decides between the chat's one tool and none).
const LIST_WAIT: Duration = Duration::from_secs(2);
/// More reconnections than this within RECONNECT_WINDOW: the service keeps stopping, the client serves the session.
const RECONNECT_LIMIT: usize = 5;
const RECONNECT_WINDOW: Duration = Duration::from_secs(60);

pub struct Opts {
    pub for_host: Option<String>,
    pub plugin_root: Option<PathBuf>,
}

impl Opts {
    pub fn parse(args: &[OsString]) -> Opts {
        let mut for_host = None;
        let mut plugin_root: Option<PathBuf> = None;
        let mut i = 0;
        while i < args.len() {
            let arg = args[i].to_string_lossy().into_owned();
            if let Some(value) = arg.strip_prefix("--for=") {
                for_host = Some(value.to_string());
            } else if arg == "--for" {
                for_host = args.get(i + 1).map(|v| v.to_string_lossy().into_owned());
                i += 1;
            } else if let Some(value) = arg.strip_prefix("--plugin-root=") {
                plugin_root = Some(PathBuf::from(value));
            } else if arg == "--plugin-root" {
                plugin_root = args.get(i + 1).map(PathBuf::from);
                i += 1;
            }
            i += 1;
        }
        // A host that does not expand ${CLAUDE_PLUGIN_ROOT} passes it as written: no plugin root then.
        let plugin_root = plugin_root
            .filter(|p| !p.to_string_lossy().contains("${") && p.is_dir())
            .or_else(|| home::env_value("CLAUDE_PLUGIN_ROOT").map(PathBuf::from).filter(|p| p.is_dir()))
            .or_else(home::plugin_root_of_exe);
        Opts { for_host: for_host.filter(|f| f == "codex" || f == "claude-desktop"), plugin_root }
    }
}

/// The Claude app as an MCP client: its chat (a name with "claude" in it other than Claude Code's "claude-code") or
/// its agent mode ("local-agent-mode-..."); chatbuild.is_claude_app, the same rule.
pub fn is_claude_app(name: &str) -> bool {
    let lowered = name.to_lowercase();
    lowered.starts_with("local-agent-mode") || (lowered.contains("claude") && !lowered.starts_with("claude-code"))
}

fn host_for(for_host: Option<&str>, client: &str) -> &'static str {
    let lowered = client.to_lowercase();
    match for_host {
        Some("codex") => "codex",
        Some("claude-desktop") => "claude-desktop",
        _ if lowered.starts_with("claude-code") => "claude-code",
        _ if lowered.starts_with("local-agent-mode") => "cowork",
        _ if lowered.contains("claude") => "claude-desktop",
        _ if lowered.contains("codex") => "codex",
        _ => "unknown",
    }
}

pub fn static_tools(chat: bool) -> Vec<Json> {
    json::parse(if chat { CHAT_TOOLS } else { PLUGIN_TOOLS })
        .ok()
        .and_then(|doc| doc.get("tools").and_then(Json::as_array).cloned())
        .unwrap_or_default()
}

#[derive(Clone, Copy, PartialEq, Debug)]
enum Mode {
    /// Before the service answered (or while it is being reached again): calls wait in the table.
    Waiting,
    Connected,
    /// The client's own server serves the session; every message goes to it as it came.
    Fallback,
    /// Nothing can serve the session: every call is answered with why.
    Dead,
}

struct Call {
    rpc: Json,
    line: String,
    tool: String,
    args: Option<Vec<Json>>,
    params: Option<Json>,
    cwd: String,
    images: bool,
    token: Option<Json>,
    attempted: bool,
    cancelled: bool,
    internal: bool,
    stdout: String,
    stderr: String,
    pictures: Vec<Json>,
}

struct State {
    mode: Mode,
    gen: u64,
    client_name: String,
    client_version: String,
    init_line: Option<String>,
    init_id: Option<Json>,
    initialized: bool,
    chat: bool,
    calls: BTreeMap<u64, Call>,
    next: u64,
    tools: Option<Vec<Json>>,
    served: Option<String>,
    connector: bool,
    draining: bool,
    closing: bool,
    refused: Option<String>,
    dead: String,
    reconnects: Vec<Instant>,
}

struct Relay {
    home: PathBuf,
    launch: Launch,
    opts: Opts,
    cwd: PathBuf,
    overlay: Json,
    session_id: String,
    out: Mutex<io::Stdout>,
    st: Mutex<State>,
    list_ready: Condvar,
    writer: Mutex<Option<(u64, Conn)>>,
    child_in: Mutex<Option<ChildStdin>>,
    child: Mutex<Option<(Child, thread::JoinHandle<()>)>>,
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(|e| e.into_inner())
}

fn response(id: &Json, result: Json) -> Json {
    Json::obj().with("jsonrpc", "2.0".into()).with("id", id.clone()).with("result", result)
}

fn error_response(id: &Json, code: i64, message: &str) -> Json {
    Json::obj()
        .with("jsonrpc", "2.0".into())
        .with("id", id.clone())
        .with("error", Json::obj().with("code", Json::int(code)).with("message", Json::str(message)))
}

fn text_result(text: &str, error: bool) -> Json {
    Json::obj()
        .with("content", Json::Arr(vec![Json::obj().with("type", "text".into()).with("text", Json::str(text))]))
        .with("isError", Json::Bool(error))
}

fn notification(method: &str, params: Option<Json>) -> Json {
    let mut n = Json::obj().with("jsonrpc", "2.0".into()).with("method", method.into());
    if let Some(p) = params {
        n.set("params", p);
    }
    n
}

pub fn main(opts: Opts) -> i32 {
    let home = home::home();
    let launch = match home::read_launch(&home) {
        Ok(launch) if launch.ready() => launch,
        _ => return setup(&home, &opts),
    };
    let cwd = std::env::current_dir().unwrap_or_else(|_| home.clone());
    let session_id = ["CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID"]
        .iter()
        .find_map(|n| home::env_value(n))
        .unwrap_or_else(|| front::random_id(&format!("relay-{}-", std::process::id())));
    let overlay = front::overlay(opts.plugin_root.as_deref());
    let relay = Arc::new(Relay {
        home,
        launch,
        opts,
        cwd,
        overlay,
        session_id,
        out: Mutex::new(io::stdout()),
        st: Mutex::new(State {
            mode: Mode::Waiting,
            gen: 0,
            client_name: String::new(),
            client_version: String::new(),
            init_line: None,
            init_id: None,
            initialized: false,
            chat: false,
            calls: BTreeMap::new(),
            next: 0,
            tools: None,
            served: None,
            connector: false,
            draining: false,
            closing: false,
            refused: None,
            dead: String::new(),
            reconnects: Vec::new(),
        }),
        list_ready: Condvar::new(),
        writer: Mutex::new(None),
        child_in: Mutex::new(None),
        child: Mutex::new(None),
    });
    if relay.launch.oneshot || home::env_value("THEODORE_RESIDENT").is_some_and(|v| v.trim() == "0") {
        // No service on this installation: the client's server from the first message on.
        relay.fallback(None);
    }
    relay.serve()
}

/// Nothing installed yet: the set-up launcher serves this session (it sets Theodore up, then runs the client's
/// server), as the plugin's MCP entry did before the front.
fn setup(home: &Path, opts: &Opts) -> i32 {
    let mut args: Vec<OsString> = vec!["mcp".into()];
    if let Some(host) = &opts.for_host {
        args.push("--for".into());
        args.push(host.into());
    }
    oneshot::setup(home, &args, opts.plugin_root.as_deref())
}

impl Relay {
    fn emit(&self, message: &Json) {
        let mut line = message.dump();
        line.push('\n');
        self.emit_raw(line.as_bytes());
    }

    fn emit_raw(&self, bytes: &[u8]) {
        let mut out = lock(&self.out);
        let _ = out.write_all(bytes);
        let _ = out.flush();
    }

    fn serve(self: &Arc<Self>) -> i32 {
        let stdin = io::stdin();
        let mut input = stdin.lock();
        let mut line = Vec::new();
        loop {
            line.clear();
            match input.read_until(b'\n', &mut line) {
                Ok(0) | Err(_) => break,
                Ok(_) => {}
            }
            let text = String::from_utf8_lossy(&line);
            let text = text.trim();
            if !text.is_empty() {
                self.on_line(text);
            }
        }
        self.finish();
        0
    }

    fn on_line(self: &Arc<Self>, text: &str) {
        if lock(&self.st).mode == Mode::Fallback {
            self.to_child(text);
            return;
        }
        match json::parse(text) {
            Ok(Json::Arr(items)) => {
                for item in items {
                    let raw = item.dump();
                    self.on_message(item, &raw);
                }
            }
            Ok(message @ Json::Obj(_)) => self.on_message(message, text),
            Ok(_) => self.emit(&error_response(&Json::Null, -32600, "invalid request")),
            Err(_) => self.emit(&error_response(&Json::Null, -32700, "parse error")),
        }
    }

    fn on_message(self: &Arc<Self>, message: Json, raw: &str) {
        let Some(method) = message.get("method").and_then(Json::as_str).map(str::to_string) else {
            return; // a response to something this server never asked
        };
        let id = message.get("id").cloned();
        match method.as_str() {
            "initialize" => self.on_initialize(&message, raw, id.unwrap_or(Json::Null)),
            "notifications/initialized" => {
                let mut st = lock(&self.st);
                st.initialized = true;
                if st.mode == Mode::Fallback {
                    drop(st);
                    self.to_child(raw);
                }
            }
            "ping" => {
                if let Some(id) = id {
                    self.emit(&response(&id, Json::obj()));
                }
            }
            "tools/list" => self.on_list(raw, id.unwrap_or(Json::Null)),
            "tools/call" => self.on_call(&message, raw, id.unwrap_or(Json::Null)),
            "notifications/cancelled" => self.on_cancelled(&message, raw),
            _ => {
                if let Some(id) = id {
                    if lock(&self.st).mode == Mode::Fallback {
                        self.to_child(raw);
                    } else {
                        self.emit(&error_response(&id, -32601, &format!("method not found: {method}")));
                    }
                }
            }
        }
    }

    fn on_initialize(self: &Arc<Self>, message: &Json, raw: &str, id: Json) {
        let params = message.get("params").cloned().unwrap_or(Json::Null);
        let info = params.get("clientInfo");
        let name = info.and_then(|i| i.get("name")).and_then(Json::as_str).unwrap_or("").to_string();
        let version = info.and_then(|i| i.get("version")).and_then(Json::as_str).unwrap_or("").to_string();
        let asked = params.get("protocolVersion").and_then(Json::as_str).unwrap_or("");
        let protocol = if PROTOCOL_VERSIONS.contains(&asked) { asked } else { PROTOCOL_VERSIONS[0] };
        let start = {
            let mut st = lock(&self.st);
            st.chat = self.opts.for_host.is_some() || is_claude_app(&name);
            st.client_name = name;
            st.client_version = version;
            st.init_line = Some(raw.to_string());
            st.init_id = Some(id.clone());
            if st.mode == Mode::Fallback {
                drop(st);
                self.to_child(raw); // the client's server answers it itself
                return;
            }
            let start = !st.connector;
            st.connector = true;
            start
        };
        let result = Json::obj()
            .with("protocolVersion", protocol.into())
            .with("capabilities", Json::obj().with("tools", Json::obj().with("listChanged", Json::Bool(true))))
            .with("serverInfo", Json::obj().with("name", "theodore".into()).with("version", crate::VERSION.into()));
        self.emit(&response(&id, result));
        if start {
            let me = Arc::clone(self);
            thread::spawn(move || me.connector());
        }
    }

    fn on_list(self: &Arc<Self>, raw: &str, id: Json) {
        let mut st = lock(&self.st);
        if st.mode == Mode::Fallback {
            drop(st);
            self.to_child(raw);
            return;
        }
        // The plugin's server in the Claude app's chat: the service says which tools (the chat's one, or none when
        // the app runs Theodore's own entry); wait a moment for it rather than show the wrong ones.
        if self.opts.for_host.is_none() && st.chat && st.tools.is_none() && st.mode == Mode::Waiting {
            let deadline = Instant::now() + LIST_WAIT;
            while st.tools.is_none() && st.mode == Mode::Waiting {
                let left = deadline.saturating_duration_since(Instant::now());
                if left.is_zero() {
                    break;
                }
                st = self.list_ready.wait_timeout(st, left).map(|r| r.0).unwrap_or_else(|e| e.into_inner().0);
            }
            if st.mode == Mode::Fallback {
                drop(st);
                self.to_child(raw);
                return;
            }
        }
        let tools = Json::Arr(st.tools.clone().unwrap_or_else(|| static_tools(st.chat)));
        st.served = Some(tools.dump());
        drop(st);
        self.emit(&response(&id, Json::obj().with("tools", tools)));
    }

    fn chat_tool_name(st: &State) -> String {
        st.tools
            .as_ref()
            .and_then(|t| t.first())
            .and_then(|t| t.get("name"))
            .and_then(Json::as_str)
            .unwrap_or("theodore")
            .to_string()
    }

    /// C1: `call {tool: name, args: arguments.args, params: the other arguments, cwd: arguments.cwd or the relay's
    /// own folder, images: arguments.images (default true)}`; the chat's one tool goes as tool `chat` with every
    /// argument as `params`.
    fn map_call(&self, st: &State, name: &str, arguments: &Json) -> Result<(String, Option<Vec<Json>>, Option<Json>, bool, String), String> {
        let arguments = if matches!(arguments, Json::Obj(_)) { arguments.clone() } else { Json::obj() };
        let images = arguments.get("images").and_then(Json::as_bool).unwrap_or(true);
        let cwd = match arguments.get("cwd").and_then(Json::as_str) {
            Some(dir) if Path::new(dir).is_absolute() => dir.to_string(),
            Some(dir) => self.cwd.join(dir).to_string_lossy().into_owned(),
            None => self.cwd.to_string_lossy().into_owned(),
        };
        if st.chat {
            if name != Self::chat_tool_name(st) {
                return Err(format!("unknown tool '{name}'."));
            }
            return Ok(("chat".into(), None, Some(arguments), images, cwd));
        }
        match name {
            "pptx" | "theodore" => {
                let args = arguments.get("args").and_then(Json::as_array).cloned().ok_or("args: a list of strings is required.")?;
                if !args.iter().all(|a| matches!(a, Json::Str(_))) {
                    return Err("args: every item is a string.".into());
                }
                let mut rest = arguments.clone();
                for key in ["args", "cwd", "images"] {
                    rest.remove(key);
                }
                let params = match &rest {
                    Json::Obj(members) if members.is_empty() => None,
                    _ => Some(rest),
                };
                Ok((name.into(), Some(args), params, images, cwd))
            }
            "guide" | "specimen" | "list" => Ok((name.into(), None, Some(arguments), images, cwd)),
            _ => Err(format!("unknown tool '{name}'.")),
        }
    }

    fn on_call(self: &Arc<Self>, message: &Json, raw: &str, id: Json) {
        let params = message.get("params").cloned().unwrap_or(Json::Null);
        let name = params.get("name").and_then(Json::as_str).unwrap_or("").to_string();
        let arguments = params.get("arguments").cloned().unwrap_or(Json::Null);
        let token = params.get("_meta").and_then(|m| m.get("progressToken")).cloned();
        let mut st = lock(&self.st);
        match st.mode {
            Mode::Fallback => {
                drop(st);
                self.to_child(raw);
                return;
            }
            Mode::Dead => {
                let why = st.dead.clone();
                drop(st);
                self.emit(&response(&id, text_result(&why, true)));
                return;
            }
            _ => {}
        }
        let (tool, args, call_params, images, cwd) = match self.map_call(&st, &name, &arguments) {
            Ok(mapped) => mapped,
            Err(why) => {
                drop(st);
                self.emit(&response(&id, text_result(&why, true)));
                return;
            }
        };
        if !st.connector {
            st.connector = true; // a call before initialize: reach the service all the same
            let me = Arc::clone(self);
            thread::spawn(move || me.connector());
        }
        st.next += 1;
        let n = st.next;
        st.calls.insert(n, Call {
            rpc: id,
            line: raw.to_string(),
            tool,
            args,
            params: call_params,
            cwd,
            images,
            token,
            attempted: false,
            cancelled: false,
            internal: false,
            stdout: String::new(),
            stderr: String::new(),
            pictures: Vec::new(),
        });
        let send = st.mode == Mode::Connected && !st.draining;
        let gen = st.gen;
        drop(st);
        if send {
            self.send(n, gen);
        }
    }

    fn on_cancelled(self: &Arc<Self>, message: &Json, raw: &str) {
        let Some(request) = message.get("params").and_then(|p| p.get("requestId")).cloned() else { return };
        let mut st = lock(&self.st);
        if st.mode == Mode::Fallback {
            drop(st);
            self.to_child(raw);
            return;
        }
        let Some((&n, _)) = st.calls.iter().find(|(_, c)| !c.internal && c.rpc == request) else { return };
        let call = st.calls.get_mut(&n).expect("found above");
        if !call.attempted {
            st.calls.remove(&n); // never sent: forgotten, and never answered (MCP: no answer to a cancelled request)
            return;
        }
        call.cancelled = true;
        let live = st.mode == Mode::Connected;
        drop(st);
        if live {
            self.write_frame(None, &Json::obj().with("t", "cancel".into()).with("id", Json::str(n.to_string())));
        }
    }

    fn call_frame(&self, n: u64, call: &Call, resend: bool) -> Json {
        let mut f = Json::obj()
            .with("t", "call".into())
            .with("id", Json::str(n.to_string()))
            .with("tool", Json::str(&*call.tool))
            .with("cwd", Json::str(&*call.cwd))
            .with("env", self.overlay.clone())
            .with("images", Json::Bool(call.images));
        if let Some(args) = &call.args {
            f.set("args", Json::Arr(args.clone()));
        }
        if let Some(params) = &call.params {
            f.set("params", params.clone());
        }
        if resend {
            f.set("resend", Json::Bool(true));
        }
        f
    }

    /// Sends call `n` on connection `gen` (a call registered against an earlier connection is sent by the
    /// connector's flush instead). A call sent once before goes with `resend: true`.
    fn send(&self, n: u64, gen: u64) {
        let frame = {
            let mut st = lock(&self.st);
            let Some(call) = st.calls.get_mut(&n) else { return };
            let resend = call.attempted;
            call.attempted = true;
            let call = st.calls.get(&n).expect("present");
            self.call_frame(n, call, resend)
        };
        self.write_frame(Some(gen), &frame);
    }

    fn write_frame(&self, gen: Option<u64>, value: &Json) {
        let mut slot = lock(&self.writer);
        let Some((current, conn)) = slot.as_mut() else { return };
        if gen.is_some_and(|g| g != *current) {
            return;
        }
        if frame::write(conn, value).is_err() {
            conn.shutdown(); // the reader sees the end and reaches the service again
        }
    }

    fn hello(&self) -> Hello {
        let st = lock(&self.st);
        Hello {
            role: "relay",
            client_name: "theodore-relay",
            session_id: self.session_id.clone(),
            host: host_for(self.opts.for_host.as_deref(), &st.client_name).into(),
            pid: sys::parent_pid(),
            plugin_root: self.opts.plugin_root.as_ref().map(|p| p.to_string_lossy().into_owned()),
        }
    }

    /// Reaches the service (starting it when needed), sends every waiting call, reads its frames; when it goes away,
    /// reaches it again and resends what is unanswered. With none to be had, the client's server takes over.
    fn connector(self: Arc<Self>) {
        let mut first = true;
        loop {
            let hello = self.hello();
            let got = if first {
                service::ensure(&self.home, &self.launch, &hello, true)
            } else {
                match service::reconnect(&self.home, &hello, Duration::from_secs(3)) {
                    Some(found) => Ok(found),
                    None => service::ensure(&self.home, &self.launch, &hello, true),
                }
            };
            first = false;
            let connected = match got {
                Ok(connected) => connected,
                Err(failure) => {
                    self.fallback(Some(failure.reason()));
                    return;
                }
            };
            let reader = connected.conn;
            let Ok(writer) = reader.try_clone() else {
                self.fallback(Some("the connection to the service could not be shared".into()));
                return;
            };
            let flush: Vec<u64> = {
                let mut st = lock(&self.st);
                if st.closing {
                    return;
                }
                let now = Instant::now();
                st.reconnects.retain(|t| now.duration_since(*t) < RECONNECT_WINDOW);
                st.reconnects.push(now);
                if st.reconnects.len() > RECONNECT_LIMIT {
                    drop(st);
                    self.fallback(Some("the service keeps stopping".into()));
                    return;
                }
                st.gen += 1;
                *lock(&self.writer) = Some((st.gen, writer));
                st.mode = Mode::Connected;
                st.draining = false;
                st.calls.retain(|_, c| !c.cancelled && !c.internal);
                st.calls.keys().copied().collect()
            };
            let gen = lock(&self.st).gen;
            for n in flush {
                self.send(n, gen);
            }
            self.request_tools(gen);
            self.read_frames(reader);
            *lock(&self.writer) = None;
            let refused = {
                let mut st = lock(&self.st);
                if st.closing {
                    return;
                }
                st.mode = Mode::Waiting;
                st.refused.take()
            };
            if let Some(why) = refused {
                self.fallback(Some(why));
                return;
            }
        }
    }

    fn request_tools(&self, gen: u64) {
        let n = {
            let mut st = lock(&self.st);
            st.next += 1;
            let n = st.next;
            let params = Json::obj()
                .with("skill", "*".into())
                .with("form", "tools".into())
                .with("for", Json::str(self.opts.for_host.as_deref().unwrap_or("plugin")))
                .with("client", Json::obj().with("name", Json::str(&*st.client_name)).with("version", Json::str(&*st.client_version)));
            st.calls.insert(n, Call {
                rpc: Json::Null,
                line: String::new(),
                tool: "list".into(),
                args: None,
                params: Some(params),
                cwd: self.cwd.to_string_lossy().into_owned(),
                images: false,
                token: None,
                attempted: false,
                cancelled: false,
                internal: true,
                stdout: String::new(),
                stderr: String::new(),
                pictures: Vec::new(),
            });
            n
        };
        self.send(n, gen);
    }

    fn read_frames(self: &Arc<Self>, mut reader: Conn) {
        loop {
            let mut frame = match frame::read(&mut reader) {
                Ok(Some(frame)) => frame,
                _ => return,
            };
            let kind = frame.get("t").and_then(Json::as_str).unwrap_or("").to_string();
            if kind == "image" && frame.get("data").is_none() {
                if let Some(path) = frame.get("path").and_then(Json::as_str).map(PathBuf::from) {
                    let small = std::fs::metadata(&path).map(|m| m.len() <= PICTURE_MAX_BYTES).unwrap_or(false);
                    if let Some(bytes) = small.then(|| std::fs::read(&path).ok()).flatten() {
                        frame.set("data", Json::Str(b64::encode(&bytes)));
                    }
                }
            }
            let (outgoing, retry) = self.on_frame(&kind, &frame);
            for message in outgoing {
                self.emit(&message);
            }
            if let Some((n, wait, gen)) = retry {
                let me = Arc::clone(self);
                thread::spawn(move || {
                    thread::sleep(wait);
                    me.send(n, gen);
                });
            }
            if kind == "notify" && frame.get("method").and_then(Json::as_str) == Some("tools/list_changed") {
                let gen = lock(&self.st).gen;
                self.request_tools(gen);
            }
            if lock(&self.st).refused.is_some() {
                reader.shutdown();
                return;
            }
        }
    }

    /// One frame from the service: the messages it makes for the host, and a call to send again later (BUSY).
    fn on_frame(&self, kind: &str, frame: &Json) -> (Vec<Json>, Option<(u64, Duration, u64)>) {
        let mut outgoing = Vec::new();
        let mut st = lock(&self.st);
        let n = frame.get("id").and_then(Json::as_str).and_then(|s| s.parse::<u64>().ok());
        match kind {
            "out" => {
                if let Some(call) = n.and_then(|n| st.calls.get_mut(&n)) {
                    let data = frame.get("data").and_then(Json::as_str).unwrap_or("");
                    if frame.get("stream").and_then(Json::as_str) == Some("stderr") {
                        call.stderr.push_str(data);
                    } else {
                        call.stdout.push_str(data);
                    }
                }
            }
            "image" => {
                if let Some(call) = n.and_then(|n| st.calls.get_mut(&n)) {
                    if let (true, Some(data)) = (call.images && call.pictures.len() < MAX_PICTURES, frame.get("data").and_then(Json::as_str)) {
                        let mime = frame.get("mime").and_then(Json::as_str).unwrap_or("image/jpeg");
                        call.pictures.push(
                            Json::obj().with("type", "image".into()).with("data", Json::str(data)).with("mimeType", Json::str(mime)),
                        );
                    }
                }
            }
            "progress" => {
                if let Some(call) = n.and_then(|n| st.calls.get(&n)) {
                    if let (Some(token), false) = (&call.token, call.cancelled) {
                        let elapsed = frame.get("elapsed_ms").and_then(Json::as_i64).unwrap_or(0);
                        let params = Json::obj()
                            .with("progressToken", token.clone())
                            .with("progress", Json::Num(format!("{:.1}", elapsed as f64 / 1000.0)))
                            .with("message", Json::str(frame.get("phase").and_then(Json::as_str).unwrap_or("")));
                        outgoing.push(notification("notifications/progress", Some(params)));
                    }
                }
            }
            "done" => {
                if let Some(call) = n.and_then(|n| st.calls.remove(&n)) {
                    let exit = frame.get("exit").and_then(Json::as_i64).unwrap_or(70);
                    if call.internal {
                        self.adopt_tools(&mut st, exit, &call.stdout, &mut outgoing);
                    } else if !call.cancelled {
                        let mut content = vec![Json::obj().with("type", "text".into()).with("text", Json::Str(call.stdout))];
                        if !call.stderr.is_empty() {
                            content.push(Json::obj().with("type", "text".into()).with("text", Json::Str(format!("--- stderr ---\n{}", call.stderr))));
                        }
                        content.extend(call.pictures);
                        let mut meta = Json::obj()
                            .with("exitCode", Json::int(exit))
                            .with("elapsedMs", Json::int(frame.get("elapsed_ms").and_then(Json::as_i64).unwrap_or(0)));
                        if let Some(pending) = frame.get("pending") {
                            meta.set("pending", pending.clone());
                        }
                        let result = Json::obj().with("content", Json::Arr(content)).with("isError", Json::Bool(exit != 0)).with("_meta", meta);
                        outgoing.push(response(&call.rpc, result));
                    }
                }
            }
            "error" => {
                let code = frame.get("code").and_then(Json::as_str).unwrap_or("").to_string();
                let message = frame.get("message").and_then(Json::as_str).unwrap_or("").to_string();
                match n {
                    Some(n) if code == "BUSY" && st.calls.contains_key(&n) => {
                        let wait = frame.get("retry_after_ms").and_then(Json::as_i64).unwrap_or(200).clamp(20, 5000) as u64;
                        if let Some(call) = st.calls.get_mut(&n) {
                            call.attempted = false; // refused before it ran: sent again as new
                        }
                        let gen = st.gen;
                        return (outgoing, Some((n, Duration::from_millis(wait), gen)));
                    }
                    Some(n) => {
                        if let Some(call) = st.calls.remove(&n) {
                            if !call.internal && !call.cancelled {
                                outgoing.push(response(&call.rpc, text_result(&format!("Theodore's service refused this call ({code}: {message})."), true)));
                            }
                        }
                    }
                    None => match code.as_str() {
                        "SHUTTING_DOWN" => st.draining = true,
                        "PROTOCOL" | "BAD_SECRET" => st.refused = Some(format!("{code}: {message}")),
                        _ => {}
                    },
                }
            }
            "notify" => {
                if frame.get("method").and_then(Json::as_str) == Some("theodore/reconnect") {
                    st.draining = true; // calls wait until the successor answers
                }
            }
            _ => {}
        }
        (outgoing, None)
    }

    /// The service's tool list (`list` with form `tools`): served from now on; when it differs from what the host was
    /// given, the host is told to list again.
    fn adopt_tools(&self, st: &mut State, exit: i64, stdout: &str, outgoing: &mut Vec<Json>) {
        if exit != 0 {
            return;
        }
        let tools = match json::parse(stdout.trim()) {
            Ok(Json::Arr(items)) => items,
            Ok(doc) => match doc.get("tools").and_then(Json::as_array) {
                Some(items) => items.clone(),
                None => return,
            },
            Err(_) => return,
        };
        let dumped = Json::Arr(tools.clone()).dump();
        st.tools = Some(tools);
        self.list_ready.notify_all();
        if st.served.as_ref().is_some_and(|served| *served != dumped) {
            outgoing.push(notification("notifications/tools/list_changed", None));
        }
    }

    fn to_child(&self, line: &str) {
        let mut slot = lock(&self.child_in);
        if let Some(stdin) = slot.as_mut() {
            if stdin.write_all(line.as_bytes()).and_then(|_| stdin.write_all(b"\n")).and_then(|_| stdin.flush()).is_err() {
                *slot = None;
            }
        }
    }

    /// The client's own MCP server takes the session over: it gets `initialize` (its answer is not passed on: the
    /// host has had one), then every waiting call as the host sent it, then everything that comes.
    fn fallback(self: &Arc<Self>, reason: Option<String>) {
        if let Some(why) = &reason {
            eprintln!("theodore: Theodore's service is not available here ({why}); the client serves this session itself.");
        }
        let mut cmd = Command::new(&self.launch.runtime_python);
        cmd.arg("-I").arg(&self.launch.client_dir).arg("mcp");
        if let Some(host) = &self.opts.for_host {
            cmd.arg("--for").arg(host);
        }
        cmd.env("THEODORE_HOME", &self.home).current_dir(&self.cwd).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::inherit());
        if home::env_value("THEODORE_SKILL_DIR").is_none() {
            if let Some(skill) = self.opts.plugin_root.as_ref().map(|r| r.join("skills").join("pptx")).filter(|s| s.is_dir()) {
                cmd.env("THEODORE_SKILL_DIR", skill);
            }
        }
        let mut child = match cmd.spawn() {
            Ok(child) => child,
            Err(err) => {
                let why = format!("Theodore could not start on this computer: {} ({err}).", self.launch.runtime_python.display());
                self.die(&why);
                return;
            }
        };
        let stdin = child.stdin.take();
        let stdout = child.stdout.take();
        let mut slot = lock(&self.child_in);
        *slot = stdin;
        let (replay, init_id, listed) = {
            let mut st = lock(&self.st);
            st.mode = Mode::Fallback;
            self.list_ready.notify_all();
            let mut lines: Vec<String> = Vec::new();
            if let Some(init) = &st.init_line {
                lines.push(init.clone());
            }
            if st.initialized {
                lines.push(notification("notifications/initialized", None).dump());
            }
            for call in st.calls.values() {
                if !call.internal && !call.cancelled {
                    lines.push(call.line.clone());
                }
            }
            st.calls.clear();
            (lines, st.init_id.clone(), st.served.is_some())
        };
        if let Some(stdin) = slot.as_mut() {
            for line in &replay {
                if stdin.write_all(line.as_bytes()).and_then(|_| stdin.write_all(b"\n")).is_err() {
                    break;
                }
            }
            let _ = stdin.flush();
        }
        drop(slot);
        *lock(&self.writer) = None;
        let me = Arc::clone(self);
        let pump = thread::spawn(move || {
            let Some(stdout) = stdout else { return };
            let mut skipped = init_id.is_none();
            for line in BufReader::new(stdout).split(b'\n') {
                let Ok(line) = line else { break };
                if !skipped {
                    if let Ok(message) = json::parse(String::from_utf8_lossy(&line).trim()) {
                        if message.get("id") == init_id.as_ref() && message.get("result").is_some() {
                            skipped = true;
                            continue;
                        }
                    }
                }
                let mut bytes = line;
                bytes.push(b'\n');
                me.emit_raw(&bytes);
            }
            let closing = lock(&me.st).closing;
            if !closing {
                me.die("Theodore's client server stopped; start a new session to use Theodore again.");
            }
        });
        *lock(&self.child) = Some((child, pump));
        if listed {
            self.emit(&notification("notifications/tools/list_changed", None));
        }
    }

    /// Nothing serves the session any more: every waiting and later call is answered with why.
    fn die(&self, why: &str) {
        let waiting: Vec<Json> = {
            let mut st = lock(&self.st);
            st.mode = Mode::Dead;
            st.dead = why.to_string();
            self.list_ready.notify_all();
            let rpcs = st.calls.values().filter(|c| !c.internal && !c.cancelled).map(|c| c.rpc.clone()).collect();
            st.calls.clear();
            rpcs
        };
        for rpc in waiting {
            self.emit(&response(&rpc, text_result(why, true)));
        }
    }

    /// Standard input ended (the session is over): a `bye` to the service, or the client's server let finish.
    fn finish(&self) {
        let mode = {
            let mut st = lock(&self.st);
            st.closing = true;
            st.mode
        };
        if mode == Mode::Connected {
            self.write_frame(None, &Json::obj().with("t", "bye".into()));
        }
        *lock(&self.child_in) = None; // its standard input ends: the client's server finishes and exits
        let child = lock(&self.child).take();
        if let Some((mut child, pump)) = child {
            let deadline = Instant::now() + Duration::from_secs(5);
            while Instant::now() < deadline {
                if matches!(child.try_wait(), Ok(Some(_))) {
                    break;
                }
                thread::sleep(Duration::from_millis(20));
            }
            if !matches!(child.try_wait(), Ok(Some(_))) {
                let _ = child.kill();
            }
            let _ = pump.join();
        }
        let _ = lock(&self.out).flush();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn claude_app_names() {
        for (name, app) in [("claude-ai", true), ("Claude", true), ("claude-code", false), ("Claude-Code", false),
                            ("local-agent-mode-x", true), ("codex-mcp-client", false), ("", false), ("cursor", false)] {
            assert_eq!(is_claude_app(name), app, "{name}");
        }
    }

    #[test]
    fn hosts() {
        assert_eq!(host_for(None, "claude-code"), "claude-code");
        assert_eq!(host_for(Some("codex"), "anything"), "codex");
        assert_eq!(host_for(None, "local-agent-mode-1"), "cowork");
        assert_eq!(host_for(None, "claude-ai"), "claude-desktop");
        assert_eq!(host_for(None, "codex-mcp-client"), "codex");
        assert_eq!(host_for(None, "zed"), "unknown");
    }

    #[test]
    fn static_lists() {
        let plugin: Vec<String> = static_tools(false).iter().filter_map(|t| t.get("name").and_then(Json::as_str).map(String::from)).collect();
        assert_eq!(plugin, ["pptx", "guide", "specimen", "theodore", "list"]);
        let pptx = &static_tools(false)[0];
        assert_eq!(pptx.get("_meta").and_then(|m| m.get("anthropic/alwaysLoad")), Some(&Json::Bool(true)));
        let chat = static_tools(true);
        assert_eq!(chat.len(), 1);
        assert_eq!(chat[0].get("name").and_then(Json::as_str), Some("theodore"));
    }

    #[test]
    fn options() {
        let opts = Opts::parse(&["--for".into(), "codex".into(), "--plugin-root".into(), "${CLAUDE_PLUGIN_ROOT}".into()]);
        assert_eq!(opts.for_host.as_deref(), Some("codex"));
        assert!(!opts.plugin_root.as_ref().is_some_and(|p| p.to_string_lossy().contains("${")));
        assert_eq!(Opts::parse(&["--for=elsewhere".into()]).for_host, None);
    }
}
