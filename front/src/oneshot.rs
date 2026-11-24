//! When the service is not the way (DESIGN §2.2): the client run once by the runtime's own Python, as the launchers
//! run it but with no shell around it; and, before anything is installed, the set-up launcher.

use crate::home::{self, Launch};
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::process::Command;

#[cfg(unix)]
use std::os::unix::process::CommandExt;

/// The variables the launchers give the client: its home, and the skill folder it was called from when that is known.
fn client_command(launch: &Launch, home: &Path, args: &[OsString]) -> Command {
    let mut cmd = Command::new(&launch.runtime_python);
    cmd.arg("-I").arg(&launch.client_dir).args(args).env("THEODORE_HOME", home);
    if home::env_value("THEODORE_SKILL_DIR").is_none() {
        let root = home::env_value("CLAUDE_PLUGIN_ROOT").map(PathBuf::from).or_else(home::plugin_root_of_exe);
        if let Some(skill) = root.map(|r| r.join("skills").join("pptx")).filter(|s| s.is_dir()) {
            cmd.env("THEODORE_SKILL_DIR", skill);
        }
    }
    cmd
}

/// Runs `theodore <args>` in the client, once, with this program's standard streams: its answer and exit code are
/// the command's. `note`, when given, says on standard error why the service is not used.
pub fn client(launch: &Launch, home: &Path, args: &[OsString], note: Option<&str>) -> i32 {
    if let Some(note) = note {
        eprintln!("theodore: {note}; this command runs without Theodore's service.");
    }
    let mut cmd = client_command(launch, home, args);
    #[cfg(unix)]
    {
        let err = cmd.exec(); // only returns when the program could not be started
        blocked(&launch.runtime_python, home, &err.to_string())
    }
    #[cfg(windows)]
    {
        match cmd.status() {
            Ok(status) => status.code().unwrap_or(1),
            Err(err) => blocked(&launch.runtime_python, home, &err.to_string()),
        }
    }
}

/// `client`, waited for (the caller has a file to remove after it).
pub fn client_waiting(launch: &Launch, home: &Path, args: &[OsString], note: Option<&str>) -> i32 {
    if let Some(note) = note {
        eprintln!("theodore: {note}; this command runs without Theodore's service.");
    }
    match client_command(launch, home, args).status() {
        Ok(status) => status.code().unwrap_or(1),
        Err(err) => blocked(&launch.runtime_python, home, &err.to_string()),
    }
}

/// The launchers' status when the system refuses to start Theodore's Python (a policy, a noexec mount).
fn blocked(python: &Path, home: &Path, err: &str) -> i32 {
    println!(
        "THEODORE BLOCKED_BY_POLICY: Theodore's Python could not be started ({}, {err}): a security policy on this computer may block programs in the user folder.\n\
         NEXT: do not retry. Tell the user; their IT team can allow programs in {}, or they can use Theodore through its connector.\n\
         TELL THE USER: This computer's security policy blocked Theodore's runtime. Your IT team can allow it, or you can use Theodore through its connector.",
        python.display(),
        home.display()
    );
    73
}

/// The set-up launcher: the stable copy in `<home>/bin`, else the plugin's own beside this program, else the skill
/// folder the host named.
fn setup_launcher(home: &Path, plugin_root: Option<&Path>) -> Option<PathBuf> {
    let name = if cfg!(windows) { "theodore.cmd" } else { "theodore" };
    let mut found = vec![home.join("bin").join(name)];
    if let Some(root) = plugin_root.map(Path::to_path_buf).or_else(home::plugin_root_of_exe) {
        found.push(root.join("skills").join("pptx").join("scripts").join(name));
    }
    if let Some(skill) = home::env_value("THEODORE_SKILL_DIR") {
        found.push(PathBuf::from(skill).join("scripts").join(name));
    }
    found.into_iter().find(|p| p.is_file())
}

fn not_installed() -> i32 {
    println!(
        "THEODORE INTERNAL: Theodore is not set up on this computer, and no Theodore launcher was found to set it up.\n\
         NEXT: do not retry. Tell the user to install Theodore from https://theodoreand.ai/install, then run the same command again.\n\
         TELL THE USER: Theodore is not installed on this computer yet. You can install it from theodoreand.ai/install."
    );
    78
}

/// Nothing installed yet (no `launch.json`, or no runtime): the set-up launcher takes the command over, exactly as it
/// would have been run before the front existed.
pub fn setup(home: &Path, args: &[OsString], plugin_root: Option<&Path>) -> i32 {
    let Some(launcher) = setup_launcher(home, plugin_root) else { return not_installed() };
    run_launcher(&launcher, args)
}

#[cfg(unix)]
fn run_launcher(launcher: &Path, args: &[OsString]) -> i32 {
    let err = Command::new("/bin/sh").arg(launcher).args(args).exec();
    eprintln!("theodore: {} could not be started: {err}", launcher.display());
    78
}

/// cmd cannot carry an argument with a quote, a metacharacter or a line feed: such a command first lets the launcher
/// set Theodore up (`setup --wait 20`), then runs as usual.
#[cfg(windows)]
fn run_launcher(launcher: &Path, args: &[OsString]) -> i32 {
    use std::os::windows::process::CommandExt;
    let plain = args.iter().all(|a| {
        let text = a.to_string_lossy();
        !text.is_empty() && !text.chars().any(|c| "\"%!^&|<>\r\n".contains(c))
    });
    let mut line = format!("/d /s /c \"\"{}\"", launcher.display());
    if plain {
        for arg in args {
            line.push_str(&format!(" \"{}\"", arg.to_string_lossy()));
        }
    } else {
        line.push_str(" setup --wait 20");
    }
    line.push('"');
    let comspec = home::env_value("ComSpec").unwrap_or_else(|| "cmd.exe".into());
    let status = Command::new(comspec).raw_arg(line).status();
    match status {
        Ok(s) if plain || !s.success() => s.code().unwrap_or(1),
        Ok(_) => {
            // Set up now: the command itself, through the front again.
            match std::env::current_exe().and_then(|me| Command::new(me).args(args).status()) {
                Ok(s) => s.code().unwrap_or(1),
                Err(err) => {
                    eprintln!("theodore: {err}");
                    78
                }
            }
        }
        Err(err) => {
            eprintln!("theodore: {} could not be started: {err}", launcher.display());
            78
        }
    }
}
