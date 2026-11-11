"""Shared test helpers: scratch homes, a tiny fake runtime and engine, and runners for the launcher and client.

Nothing here touches the real home: HOME, THEODORE_HOME, CLAUDE_CONFIG_DIR and CODEX_HOME all point into a
temporary directory, the credential store is the 0600 file (never the login keychain), and no browser opens.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT_DIR = ROOT / "client"
PLUGIN = ROOT / "plugins" / "theodore"
LAUNCHER_DIR = PLUGIN / "skills" / "pptx" / "scripts"
IS_WINDOWS = os.name == "nt"

sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "runtime" / "build"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(CLIENT_DIR))

from fake_api import FakeApi, sha256_hex  # noqa: E402
import build_client  # noqa: E402

FAKE_PPTX = r'''"""A stand-in engine entry for the client's tests. It only echoes what it was asked."""
import json, os, sys
from pathlib import Path
HERE = Path(__file__).resolve()
ROOT = HERE.parents[3]
VERSION = (ROOT / "VERSION").read_text().strip()
args = sys.argv[1:]
if (ROOT / "REFUSE").exists():          # stands in for the real engine's licence check: one JSON object, exit code 1
    print(json.dumps({"ok": False, "error": "refused for the test", "code": (ROOT / "REFUSE").read_text().strip()})); sys.exit(1)
if "--help" in args or not args:
    if (ROOT / "BROKEN").exists():
        print("engine is broken", file=sys.stderr); sys.exit(3)
    print("usage: pptx <verb>"); sys.exit(0)
verb = args[0]
if verb == "version":
    print(VERSION)
elif verb == "start":
    ws = Path(args[-1]); (ws / ".pptx-state").mkdir(parents=True, exist_ok=True)
    (ws / ".pptx-state" / "engine-used.txt").write_text(VERSION)
    print(json.dumps({"ok": True, "engine": VERSION}))
elif verb == "apply":
    ws = Path(args[-1]); (ws / ".pptx-state" / "engine-used.txt").write_text(VERSION)
    print(json.dumps({"ok": True, "engine": VERSION, "packs": sorted(p.name for p in (ROOT / "skills" / "pptx-asset-packs").iterdir() if p.is_dir())}))
elif verb == "exit":
    sys.exit(int(args[1]))
elif verb == "env":
    print(json.dumps({"path0": os.environ["PATH"].split(os.pathsep)[0], "home": os.environ.get("THEODORE_HOME"),
                      "soffice": os.environ.get("SOFFICE_BIN"), "node_modules": (ROOT / "node_modules").is_dir(),
                      "venv": sys.prefix != sys.base_prefix, "exe": sys.executable}))
else:
    print(json.dumps({"verb": verb, "args": args[1:], "engine": VERSION}))
'''


def tar_bytes(files: dict[str, bytes | tuple[bytes, int]], mode: str = "w:gz", links: dict[str, str] | None = None) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode=mode) as tar:
        for name, content in sorted(files.items()):
            data, perm = content if isinstance(content, tuple) else (content, 0o644)
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), perm
            tar.addfile(info, io.BytesIO(data))
        for name, target in sorted((links or {}).items()):
            info = tarfile.TarInfo(name)
            info.type, info.linkname = tarfile.SYMTYPE, target
            tar.addfile(info)
    return raw.getvalue()


def zip_bytes(files: dict[str, bytes], prefix: str = "") -> bytes:
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, data in sorted(files.items()):
            bundle.writestr(prefix + name, data)
    return raw.getvalue()


def engine_zip(version: str, skill_text: str | None = None, broken: bool = False, prefix: str = "") -> bytes:
    files = {
        "VERSION": version.encode(),
        "package.json": json.dumps({"name": "fake-engine", "dependencies": {ENGINE_DEPENDENCY: "*"}}).encode(),
        "requirements.txt": b"# none\n",
        "skills/pptx/SKILL.md": (skill_text if skill_text is not None else f"---\nname: pptx\n---\n\n# PPTX Skill\n\nFake manual for engine {version}.\n").encode("utf-8"),
        "skills/pptx/pptxgenjs.md": b"# build API (fake)\n",
        "skills/pptx/scripts/pptx.py": FAKE_PPTX.encode("utf-8"),
    }
    if broken:
        files["BROKEN"] = b"1"
    return zip_bytes(files, prefix)


def pack_zip(pack_id: str, version: str, prefix: str = "") -> bytes:
    return zip_bytes({"pack.json": json.dumps({"schema": "pptx-asset-pack-v1", "id": pack_id, "version": version}).encode(),
                      "catalog.json": b"{}", "data/one.txt": b"1"}, prefix)


# THEODORE_TEST_BUNDLE=<output folder of runtime/build/build_bundle.py> makes the tests install the real runtime
# bundle instead of the tiny stand-in. CI sets it on Windows, where a shell-script "python" cannot stand in.
REAL_BUNDLE = os.environ.get("THEODORE_TEST_BUNDLE")
RUNTIME_ID = "rt-test-1"
ENGINE_DEPENDENCY = "fakedep"
if REAL_BUNDLE:
    _manifest = json.loads(next(Path(REAL_BUNDLE).glob("*.manifest.json")).read_text(encoding="utf-8"))
    RUNTIME_ID = _manifest["runtime_id"]
    ENGINE_DEPENDENCY = "pptxgenjs"


def fake_runtime_parts() -> dict[str, tuple[str, bytes | Path]]:
    """A runtime small enough for tests: `python` is a wrapper around the interpreter running the tests."""
    if REAL_BUNDLE:
        return {p["part"]: (p["file"], Path(REAL_BUNDLE) / p["file"]) for p in _manifest["parts"] if p["part"] != "client"}
    if IS_WINDOWS:
        raise unittest.SkipTest("on Windows the tests need the real runtime bundle: set THEODORE_TEST_BUNDLE")
    wrapper = f'#!/bin/sh\nexec "{sys.executable}" "$@"\n'.encode()
    node = shutil.which("node")
    node_script = (f'#!/bin/sh\nexec "{node}" "$@"\n' if node else '#!/bin/sh\necho v24.0.0-fake\n').encode()
    return {
        "python": ("python.tar.gz", tar_bytes({"python/bin/python3": (wrapper, 0o755)})),
        "wheels": ("wheels.tar.xz", tar_bytes({"python/lib/python3.12/site-packages/fakewheel/__init__.py": b"VERSION = '1'\n"}, "w:xz")),
        "node": ("node.tar.xz", tar_bytes({"node/bin/node": (node_script, 0o755), "licenses/node/LICENSE": b"MIT\n"}, "w:xz")),
        "node_modules": ("node_modules.tar.xz", tar_bytes({"node_modules/fakedep/package.json": b'{"name":"fakedep","version":"1.0.0"}',
                                                            "node_modules/fakedep/index.js": b"module.exports = 1;\n"}, "w:xz")),
    }


def make_certificate(folder: Path) -> tuple[str, str]:
    """A throwaway self-signed certificate for 127.0.0.1 that no certificate store trusts."""
    openssl = shutil.which("openssl")
    if not openssl:
        raise unittest.SkipTest("openssl is not installed")
    cert, key = folder / "test-cert.pem", folder / "test-key.pem"
    done = subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "2",
                           "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost"], capture_output=True, text=True)
    if done.returncode != 0:
        raise unittest.SkipTest(f"openssl could not make a certificate: {done.stderr.strip()[-200:]}")
    return str(cert), str(key)


SITE = "https://site.example.test"       # the stamped site base of the test build: texts must name it, never production


class Sandbox:
    """One scratch directory holding a clean user home, a Theodore home and a copy of the plugin wired to a fake API."""

    def __init__(self, api: FakeApi | None = None, home_name: str = "theodore home"):
        self.tmp = Path(tempfile.mkdtemp(prefix="theodore-test-")).resolve()
        self.api = api
        self.user_home = self.tmp / "user"
        self.home = self.tmp / home_name          # a space in the path on purpose
        self.user_home.mkdir()
        self.tree = self.tmp / "plugin copy"          # a marketplace tree, as the repository is
        self.plugin = self.tree / "plugins" / "theodore"
        shutil.copytree(PLUGIN, self.plugin)
        for rel in (".claude-plugin/marketplace.json", ".agents/plugins/marketplace.json"):
            (self.tree / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, self.tree / rel)
        self.launcher = self.plugin / "skills" / "pptx" / "scripts" / ("theodore.cmd" if IS_WINDOWS else "theodore")
        self.env = {
            "HOME": str(self.user_home), "USERPROFILE": str(self.user_home), "THEODORE_HOME": str(self.home),
            "CLAUDE_CONFIG_DIR": str(self.user_home / ".claude"), "CODEX_HOME": str(self.user_home / ".codex"),
            "XDG_CACHE_HOME": str(self.user_home / ".cache"), "TMPDIR": str(self.tmp / "tmp"),
            "THEODORE_CREDENTIAL_STORE": "file", "THEODORE_NO_BROWSER": "1", "THEODORE_BOOTSTRAP_WAIT": "60",
            "THEODORE_TEST_POLL_INTERVAL": "0.2",
            "PATH": os.pathsep.join(["/usr/bin", "/bin", "/usr/sbin", "/sbin"]) if not IS_WINDOWS else os.environ["PATH"],
            "LANG": "en_US.UTF-8",
        }
        if IS_WINDOWS:
            # The processor variables are how a batch file learns the architecture; every real session has them.
            for key in ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "TEMP", "TMP", "LOCALAPPDATA", "APPDATA",
                        "COMPUTERNAME", "PROCESSOR_ARCHITECTURE", "PROCESSOR_ARCHITEW6432", "NUMBER_OF_PROCESSORS"):
                if key in os.environ:
                    self.env[key] = os.environ[key]
        (self.tmp / "tmp").mkdir()
        if api:
            self.write_launcher_json()

    # --- configuration -----------------------------------------------------------------------------------------------
    def launcher_config(self, generation: int = 1) -> dict:
        api = self.api
        client_tar = build_client.build(self.tmp / "dist")
        client_bytes = client_tar.read_bytes()
        version = build_client.client_version()
        api.publish_client(version, client_bytes)
        config = {"launcher_generation": generation, "shim_version": json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())["version"], "api_base": api.base, "dl_base": f"{api.base}/dl",
                  "marketplace_url": f"{api.base}/marketplace.json", "site_base": SITE, "manifest_key_1_kid": api.MANIFEST_KID,
                  "manifest_key_1_x": api.manifest_public_key(), "stage1_runtime_id": (api.runtime or {}).get("runtime_id", RUNTIME_ID),
                  "stage1_client_version": version, "stage1_client_url": f"{api.base}/dl/client/client-{version}.tar",
                  "stage1_client_sha256": sha256_hex(client_bytes), "stage1_client_size": len(client_bytes)}
        python = next((p for p in (api.runtime or {}).get("parts", []) if p["part"] == "python"), None)
        if python:
            for key in ("darwin_arm64", "darwin_x64", "linux_x64", "linux_arm64", "win32_x64"):
                config[f"stage1_python_{key}_url"] = python["url"]
                config[f"stage1_python_{key}_sha256"] = python["sha256"]
                config[f"stage1_python_{key}_size"] = python["size"]
        return config

    def write_launcher_json(self, generation: int = 1, into: Path | None = None, **overrides) -> dict:
        config = self.launcher_config(generation)
        config.update(overrides)
        text = "{\n" + ",\n".join(f"  {json.dumps(k)}: {json.dumps(v)}" for k, v in config.items()) + "\n}\n"
        targets = [into] if into else [self.plugin / "skills" / s / "scripts" for s in ("pptx", "deck-design")]
        for folder in targets:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "launcher.json").write_text(text, encoding="utf-8")
        return config

    def serve_plugin_archive(self) -> dict:
        """Build the plugin archives from this sandbox's tree (test launcher.json inside) and serve them under /dl/plugin/,
        with latest.json signed by the fake's manifest key as the publish step signs it."""
        import build_plugin_archives
        out = self.tmp / "plugin-dist"
        record = build_plugin_archives.build(out, self.tree)
        for path in out.iterdir():
            self.api.files[f"plugin/{path.name}"] = path.read_bytes()
        self.api.files["plugin/latest.json"] = json.dumps(self.api.signed(json.loads((out / "latest.manifest.json").read_text()))).encode()
        return record

    def installer(self, **overrides) -> Path:
        """install.sh or install.cmd with its pins stamped for this sandbox (the fake's key, API and stage-1 files)."""
        import sync_launchers
        config = json.loads((self.plugin / "skills" / "pptx" / "scripts" / "launcher.json").read_text(encoding="utf-8"))
        config.update(overrides)
        kind = "cmd" if IS_WINDOWS else "sh"
        path = self.tmp / "installer" / f"install.{kind}"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(sync_launchers.stamped_installer(kind, config).encode("utf-8"))
        return path

    def install_client_directly(self) -> None:
        """Skip the launcher: put launcher.json where the client looks for it, so `run_client` works alone."""
        self.write_launcher_json(into=self.home / "bin")
        (self.home / "bin" / "theodore").write_text("#!/bin/sh\n", encoding="utf-8")

    # --- runners ---------------------------------------------------------------------------------------------------------
    def run_launcher(self, *args: str, env: dict | None = None, timeout: float = 180, launcher: Path | None = None):
        target = str(launcher or self.launcher)
        command = cmd_command(target, *args) if IS_WINDOWS else ["/bin/sh", target, *args]
        return subprocess.run(command, env={**self.env, **(env or {})}, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, cwd=str(self.tmp))

    def run_client(self, *args: str, env: dict | None = None, timeout: float = 180, cwd: Path | None = None):
        command = [sys.executable, "-I", str(CLIENT_DIR), *args]
        return subprocess.run(command, env={**self.env, **(env or {})}, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, cwd=str(cwd or self.tmp))

    def wait_jobs(self, timeout: float = 60.0) -> None:
        """Wait for detached jobs of this home to end, so a test can assert on their result and clean up."""
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            busy = False
            jobs = self.home / "state" / "jobs"
            for path in list(jobs.glob("*.json")) if jobs.exists() else []:
                try:
                    state = json.loads(path.read_text())
                except (OSError, ValueError):
                    busy = True
                    continue
                if not state.get("done") and not state.get("failed") and _alive(int(state.get("pid") or 0)):
                    busy = True
            pid_file = self.home / "state" / "bootstrap" / "pid"
            if pid_file.exists():
                try:
                    busy = busy or _alive(int(pid_file.read_text().strip() or 0))
                except (OSError, ValueError):
                    pass
            lock_file = self.home / "state" / "bootstrap" / "job.lock"       # Windows: the running job keeps it open
            if IS_WINDOWS and lock_file.exists():
                try:
                    open(lock_file, "a").close()
                except OSError:
                    busy = True
            if not busy:
                return
            time.sleep(0.2)

    def close(self) -> None:
        self.wait_jobs(20)

        def onerror(func, path, _exc):
            try:
                os.chmod(path, stat.S_IRWXU)
                func(path)
            except OSError:
                pass

        shutil.rmtree(self.tmp, onerror=onerror)


def cmd_command(script: str, *args: str) -> str:
    """The command line a shell gives cmd.exe for `script args...`: `cmd /d /s /c ""script" "arg" ..."`.

    /s makes cmd strip exactly the outer pair of quotes (without it, a line with more than two quotes loses its first
    and last quote and a path with a space breaks). An argument with a space or a character cmd treats specially is
    quoted, as PowerShell and Git Bash do; cmd cannot carry a literal double quote inside an argument."""
    def quote(arg: str) -> str:
        return f'"{arg}"' if not arg or any(c in arg for c in ' \t&|<>^()%!,;=') else arg
    return 'cmd /d /s /c "' + " ".join([f'"{script}"', *map(quote, args)]) + '"'


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def status_lines(output: str) -> tuple[str, str, str]:
    """The last three-line status in a command's output: (first line, NEXT line, TELL THE USER line)."""
    lines = [l for l in output.splitlines() if l.strip()]
    for index in range(len(lines) - 3, -1, -1):
        if lines[index].startswith("THEODORE ") and lines[index + 1].startswith("NEXT: ") and lines[index + 2].startswith("TELL THE USER: "):
            return lines[index], lines[index + 1], lines[index + 2]
    raise AssertionError(f"no three-line status in output:\n{output}")


class ClientCase(unittest.TestCase):
    """A fake API with a published runtime, engine and packs, and a clean home with the client wired to it."""

    ENGINE_VERSION = "1.0.0-rc.3"

    def setUp(self):
        self.api = FakeApi().start()
        self.addCleanup(self.api.stop)
        if IS_WINDOWS:
            raise unittest.SkipTest("the client suite is platform-independent Python and runs on macOS and Linux; Windows runs the launcher suite")
        self.runtime_parts = fake_runtime_parts()
        self.api.publish_runtime(RUNTIME_ID, self.runtime_parts)
        self.api.publish_engine(self.ENGINE_VERSION, engine_zip(self.ENGINE_VERSION))
        for pack in ("icons", "flags", "backgrounds", "maps"):
            self.api.publish_pack(pack, "1.0.0", pack_zip(pack, "1.0.0"))
        self.box = Sandbox(self.api)
        self.addCleanup(self.box.close)
        self.box.install_client_directly()

    def client(self, *args, **kwargs):
        return self.box.run_client(*args, **kwargs)

    def assertStatus(self, result, name: str, code: int):
        first, nxt, tell = status_lines(result.stdout)
        self.assertEqual(result.returncode, code, f"exit code for {name}:\n{result.stdout}\n{result.stderr}")
        self.assertTrue(first.startswith(f"THEODORE {name}: "), f"expected THEODORE {name}, got: {first}")
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        return first, nxt, tell

    def sign_in(self):
        code = self.api.issue_activation_code("ABCD-1234")
        result = self.client("login", "--code", code)
        self.assertStatus(result, "OK", 0)

    def ready(self):
        """Signed in, runtime and engine installed, packs fetched."""
        self.sign_in()
        result = self.client("setup", "--job")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.box.wait_jobs()

    def state(self, name: str) -> dict:
        path = self.box.home / name
        return json.loads(path.read_text()) if path.exists() else {}
