"""What must stay true of the repository itself: the stubs, the generated copies, the pins, the contract, and
the limits the hosts put on a plugin (no bin/, no lockfile at the plugin root, no hooks, text files only)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "theodore"
SCRIPTS = PLUGIN / "skills" / "pptx" / "scripts"
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT / "client"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "runtime" / "build"))

SKIP_DIRS = {".git", "__pycache__", "dist", ".cache", ".scratch"}


def repo_files(base: Path = ROOT) -> list[Path]:
    found = []
    for path in base.rglob("*"):
        rel = path.relative_to(ROOT).parts
        if rel[:2] == ("ci", "out"):          # the local runner's outputs (git-ignored)
            continue
        if path.is_file() and not (set(rel) & SKIP_DIRS) and path.name != ".DS_Store":
            found.append(path)
    return found


def frontmatter(text: str) -> dict[str, str]:
    lines = text.split("\n")
    assert lines[0] == "---"
    end = lines.index("---", 1)
    return {line.split(":", 1)[0]: line for line in lines[1:end]}


class Stubs(unittest.TestCase):
    def test_name_and_description_are_byte_identical_to_the_source_skills(self):
        for skill in ("pptx", "deck-design"):
            stub = frontmatter((PLUGIN / "skills" / skill / "SKILL.md").read_text(encoding="utf-8"))
            source = frontmatter((FIXTURES / "frontmatter" / f"{skill}.txt").read_text(encoding="utf-8"))
            self.assertEqual(stub["name"].encode(), source["name"].encode(), skill)
            self.assertEqual(stub["description"].encode(), source["description"].encode(), skill)
            self.assertEqual(sorted(stub), ["allowed-tools", "description", "name"])

    def test_fixtures_match_the_engine_repository_when_it_is_checked_out(self):
        source_root = Path(os.environ.get("THEODORE_PRESENTATIONS") or ROOT.parent / "theodore-presentations")
        if not (source_root / "skills" / "pptx" / "SKILL.md").exists():
            self.skipTest("the engine repository is not checked out beside this one")
        for skill in ("pptx", "deck-design"):
            live = frontmatter((source_root / "skills" / skill / "SKILL.md").read_text(encoding="utf-8"))
            mine = frontmatter((FIXTURES / "frontmatter" / f"{skill}.txt").read_text(encoding="utf-8"))
            self.assertEqual((live["name"], live["description"]), (mine["name"], mine["description"]), skill)

    def test_body_is_the_loader_text(self):
        flat = lambda text: " ".join(text.split())
        for skill in ("pptx", "deck-design"):
            body = (PLUGIN / "skills" / skill / "SKILL.md").read_text(encoding="utf-8").split("\n---\n", 1)[1]
            # The host injects the instructions as the skill's body (Claude Code's !`...`), first thing in the body.
            self.assertTrue(body.startswith(f'\n!`sh "${{CLAUDE_SKILL_DIR}}/scripts/theodore" skill {skill}`\n'), body[:120])
            self.assertEqual(body.count("!`"), 1)
            self.assertIn(f'sh "<skill folder>/scripts/theodore" guide {skill}', body)
            self.assertIn(f'- Windows PowerShell:       & "<skill folder>\\scripts\\theodore.cmd" guide {skill}', body)
            self.assertIn(f'- Windows cmd:              "<skill folder>\\scripts\\theodore.cmd" guide {skill}', body,
                          "cmd has no call operator: its line must not start with &")
            self.assertNotIn("PowerShell or cmd", body)
            self.assertIn(f"If no {skill} instructions appear above this line", flat(body))
            self.assertIn("`=== END`", body)
            self.assertIn("A first line starting `THEODORE ` is a status", flat(body))
            self.assertIn("Never substitute another slide tool silently.", flat(body))
            self.assertIn("Give sub-agents the same command.", flat(body))
            self.assertLess(len(body), 2500, "the stub stays small")
        self.assertIn("use the deck-design skill with the host's own slide tools", flat((PLUGIN / "skills" / "pptx" / "SKILL.md").read_text()))
        deck = (PLUGIN / "skills" / "deck-design" / "SKILL.md").read_text()
        self.assertIn("Customize > Plugins > Theodore > Connectors", deck)
        self.assertIn('call the Theodore `guide` tool with skill "deck-design"', flat(deck))
        allowed = frontmatter(deck)["allowed-tools"]
        for tool in ("mcp__plugin_theodore_theodore__guide", "mcp__plugin_theodore_theodore__specimen"):
            self.assertIn(tool, allowed, "the names Claude Code gives the plugin's tools")

    def test_the_client_compiles_without_a_warning(self):
        """A warning goes to standard error, which a host that injects a command's output puts into the skill's text."""
        import warnings
        for path in sorted((ROOT / "client").rglob("*.py")):
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                compile(path.read_text(encoding="utf-8"), str(path), "exec")

    def test_no_instruction_text_or_engine_code_is_in_the_repository(self):
        for path in repo_files(PLUGIN):
            self.assertNotIn(path.suffix, (".py", ".js", ".mjs", ".ps1"), f"{path}: the plugin carries no program but the launcher")
        self.assertEqual(sorted(p.name for p in (PLUGIN / "skills").iterdir()), ["deck-design", "pptx"])
        self.assertEqual(sorted(p.name for p in (PLUGIN / "skills" / "deck-design").iterdir()), ["LICENSE", "NOTICE", "SKILL.md", "scripts"])
        self.assertEqual(sorted(p.name for p in (PLUGIN / "skills" / "pptx").iterdir()), ["SKILL.md", "scripts"])
        self.assertEqual([p for p in repo_files() if p.suffix == ".ps1" and p.relative_to(ROOT).parts[0] != "ci"], [],
                         "no PowerShell script anywhere a user's machine runs (ci/ is the developer's runner)")


class GeneratedFiles(unittest.TestCase):
    def test_launcher_copies_and_pins_are_in_step(self):
        done = subprocess.run([sys.executable, str(ROOT / "tools" / "sync_launchers.py"), "--check"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, "run tools/sync_launchers.py:\n" + done.stdout)

    def test_deck_design_launcher_files_are_byte_identical(self):
        for name in ("theodore", "theodore.cmd", "launcher.json"):
            self.assertEqual((SCRIPTS / name).read_bytes(), (PLUGIN / "skills" / "deck-design" / "scripts" / name).read_bytes(), name)

    def test_launcher_json_is_flat_and_complete(self):
        text = (SCRIPTS / "launcher.json").read_text(encoding="utf-8")
        lines = text.splitlines()
        self.assertEqual((lines[0], lines[-1]), ("{", "}"))
        for line in lines[1:-1]:       # sh reads it with sed and cmd with findstr: one "key": value per line
            self.assertRegex(line, r'^  "[a-z0-9_]+": ("[^"\\]*"|\d+),?$')
        config = json.loads(text)
        versions = json.loads((ROOT / "runtime" / "locks" / "versions.json").read_text())
        for platform, asset in versions["python"]["assets"].items():
            key = "stage1_python_" + platform.replace("-", "_")
            self.assertEqual(config[key + "_sha256"], asset["sha256"])
            self.assertEqual(config[key + "_size"], asset["size"])
            self.assertTrue(config[key + "_url"].endswith("/" + asset["file"]))
        import sync_launchers
        self.assertEqual(config["stage1_runtime_id"], sync_launchers.runtime_id())
        for key in ("launcher_generation", "shim_version", "api_base", "dl_base", "marketplace_url", "manifest_key_1_kid", "manifest_key_1_x",
                    "stage1_client_version", "stage1_client_url", "stage1_client_sha256"):
            self.assertIn(key, config)
        self.assertEqual(config["shim_version"], json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())["version"])

    def test_deploy_stamping_of_environment_and_keys(self):
        import sync_launchers
        staged = json.loads(sync_launchers.stamped_launcher_json("staging", {"keys": [
            {"kty": "OKP", "crv": "Ed25519", "kid": "ent-1", "x": "A" * 43, "purpose": "entitlement"},
            {"kty": "OKP", "crv": "Ed25519", "kid": "manifest-2026-11", "x": "B" * 43, "purpose": "manifest"},
            {"kty": "OKP", "crv": "Ed25519", "kid": "manifest-next", "x": "C" * 43, "purpose": "manifest"}]}))
        self.assertEqual((staged["api_base"], staged["dl_base"], staged["marketplace_url"]),
                         ("https://staging-api.theodoreand.ai", "https://staging-dl.theodoreand.ai", "https://staging.theodoreand.ai/marketplace.json"))
        self.assertEqual({k: v for k, v in staged.items() if k.startswith("manifest_key_")},
                         {"manifest_key_1_kid": "manifest-2026-11", "manifest_key_1_x": "B" * 43, "manifest_key_2_kid": "manifest-next", "manifest_key_2_x": "C" * 43})
        self.assertTrue(staged["stage1_client_url"].startswith("https://staging-dl.theodoreand.ai/client/"))
        self.assertTrue(all(v.startswith("https://staging-dl.theodoreand.ai/runtime/") for k, v in staged.items() if k.startswith("stage1_python_") and k.endswith("_url")))
        with self.assertRaises(SystemExit):
            sync_launchers.stamped_launcher_json(None, {"keys": [{"kid": "ent-1", "x": "A" * 43, "purpose": "entitlement"}]})
        snapshot = json.loads((FIXTURES / "contract-v1.json").read_text())["origins"]
        for env, origins in sync_launchers.ORIGINS.items():
            self.assertEqual(origins, {k: snapshot[env][k] for k in ("public", "api", "dl")})
        unchanged = json.loads(sync_launchers.stamped_launcher_json())
        self.assertEqual(unchanged, json.loads((SCRIPTS / "launcher.json").read_text()), "without options nothing but the pins is touched")

    def test_client_archive_is_reproducible_and_pinned(self):
        import build_client
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            first, second = build_client.build(Path(one)), build_client.build(Path(two))
            self.assertEqual(first.read_bytes(), second.read_bytes())
            digest = hashlib.sha256(first.read_bytes()).hexdigest()
            import tarfile
            with tarfile.open(first) as tar:
                names = tar.getnames()
        config = json.loads((SCRIPTS / "launcher.json").read_text())
        self.assertEqual(config["stage1_client_sha256"], digest)
        version = build_client.client_version()
        self.assertIn(f"{version}/__main__.py", names)
        self.assertIn(f"{version}/theodore_client/cli.py", names)
        self.assertIn(f"{version}/truststore/LICENSE", names)
        self.assertTrue(all(n.startswith(version + "/") for n in names))

    def test_plugin_archives_are_reproducible(self):
        import build_plugin_archives
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            first, second = build_plugin_archives.build(Path(one)), build_plugin_archives.build(Path(two))
            self.assertEqual(first, second)
            with zipfile.ZipFile(Path(one) / first["zip"]["file"]) as bundle:
                names = bundle.namelist()
                self.assertIn(".claude-plugin/plugin.json", names)
                self.assertIn("skills/pptx/scripts/theodore", names)
                self.assertEqual((bundle.getinfo("skills/pptx/scripts/theodore").external_attr >> 16) & 0o777, 0o755)
                self.assertIn(b"\r\n", bundle.read("skills/pptx/scripts/theodore.cmd"))
            latest = (Path(one) / "latest.manifest.json").read_text()
            self.assertEqual(json.loads(latest), {"schema": "theodore-plugin-latest@1", "version": first["version"], "archive": first["tar"]["file"],
                                                  "sha256": first["tar"]["sha256"], "size": first["tar"]["size"]})
            self.assertEqual(latest, json.dumps(json.loads(latest), sort_keys=True, separators=(",", ":")), "canonical JSON: the bytes to sign")
            self.assertFalse((Path(one) / "latest.txt").exists(), "nothing unsigned is published for the installer")


    def test_a_release_build_refuses_the_placeholder_key_and_takes_stamped_keys(self):
        import build_plugin_archives
        import sync_launchers
        config = json.loads((SCRIPTS / "launcher.json").read_text())
        production = json.loads((FIXTURES / "production-keys.json").read_text())
        manifest = [k for k in production["keys"] if k["purpose"] == "manifest"][0]
        self.assertEqual((config["manifest_key_1_kid"], config["manifest_key_1_x"]), (manifest["kid"], manifest["x"]),
                         "the repository is the production build: it carries the production manifest public key")
        self.assertEqual((config["api_base"], config["dl_base"], config["site_base"]),
                         ("https://api.theodoreand.ai", "https://dl.theodoreand.ai", "https://theodoreand.ai"))
        self.assertEqual(build_plugin_archives.build(Path(tempfile.mkdtemp()) / "out", release=True)["version"],
                         json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())["version"])
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "tree"
            for rel in (".claude-plugin", ".agents", "plugins"):
                shutil.copytree(ROOT / rel, tree / rel)
            for skill in ("pptx", "deck-design"):          # a tree with the placeholder key is refused by a release build
                path = tree / "plugins" / "theodore" / "skills" / skill / "scripts" / "launcher.json"
                text = path.read_text().replace(manifest["kid"], "manifest-unset").replace(manifest["x"], "A" * 43)
                path.write_text(text)
            with self.assertRaises(SystemExit) as refused:
                build_plugin_archives.build(Path(tmp) / "out", tree, release=True)
            self.assertIn("placeholder manifest key", str(refused.exception))
            sys.path.insert(0, str(ROOT / "tests"))
            from fake_api import b64u, public_key
            real = b64u(public_key(hashlib.sha256(b"release check").digest()))
            stamped = sync_launchers.stamped_launcher_json(None, {"keys": [{"kid": "manifest-test", "x": real, "purpose": "manifest"}]})
            for skill in ("pptx", "deck-design"):
                (tree / "plugins" / "theodore" / "skills" / skill / "scripts" / "launcher.json").write_text(stamped)
            self.assertEqual(build_plugin_archives.build(Path(tmp) / "out", tree, release=True)["version"],
                             json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())["version"])


class Installers(unittest.TestCase):
    def test_stamping_keeps_crlf_in_install_cmd_and_changes_only_the_pins(self):
        import sync_launchers
        config = json.loads((SCRIPTS / "launcher.json").read_text())
        config["api_base"], config["manifest_key_1_kid"] = "https://api.example.test", "k-test"
        stamped = sync_launchers.stamped_installer("cmd", config)
        original = (ROOT / "install" / "install.cmd").read_bytes().decode()
        self.assertNotIn("\n", stamped.replace("\r\n", ""), "CRLF only")
        self.assertIn('set "PIN_API_BASE=https://api.example.test"', stamped)
        self.assertEqual(len(stamped.split("\r\n")), len(original.split("\r\n")))


class Workflows(unittest.TestCase):
    def test_the_secret_scan_runs_for_every_commit(self):
        text = (ROOT / ".github" / "workflows" / "secrets.yml").read_text()
        on = text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertNotIn("paths", on, "no path filter: a commit touching only Markdown or locks is scanned too")
        self.assertIn("gitleaks\" git . ", text)
        self.assertNotIn("gitleaks", (ROOT / ".github" / "workflows" / "shim.yml").read_text())

    def test_the_combined_runtime_index_keeps_lock_sha256(self):
        import runtime_index
        with tempfile.TemporaryDirectory() as tmp:
            for platform in runtime_index.ALL:
                folder = Path(tmp) / platform
                folder.mkdir()
                (folder / "python.tar.gz").write_bytes(platform.encode())
                system, arch = platform.split("-")
                (folder / f"runtime-{platform}.manifest.json").write_text(json.dumps({
                    "schema": "theodore-runtime-bundle-v1", "runtime_id": "rt1", "lock_sha256": "c" * 64, "platform": system, "arch": arch,
                    "recipe_version": 2, "python_version": "3.12.16", "node_version": "24.0.0", "built_at": "2026-11-10T00:00:00Z",
                    "parts": [{"part": "python", "file": "python.tar.gz", "size": len(platform), "sha256": hashlib.sha256(platform.encode()).hexdigest()}]}))
            index = runtime_index.build_index(Path(tmp))
            self.assertEqual(index["lock_sha256"], "c" * 64)
            self.assertTrue(all(p["lock_sha256"] == "c" * 64 for p in index["platforms"].values()))
            (Path(tmp) / "linux-x64" / "python.tar.gz").write_bytes(b"tampered")
            with self.assertRaises(SystemExit):
                runtime_index.build_index(Path(tmp))
        self.assertIn("tools/runtime_index.py", (ROOT / ".github" / "workflows" / "runtime.yml").read_text())


class HostLimits(unittest.TestCase):
    def test_no_bin_no_lockfile_no_hooks_at_the_plugin_root(self):
        names = {p.name for p in PLUGIN.iterdir()}
        for banned in ("bin", "hooks", "package.json", "package-lock.json", "npm-shrinkwrap.json", "bun.lock", "requirements.txt"):
            self.assertNotIn(banned, names)
        self.assertFalse((ROOT / "bin").exists(), "no top-level bin/ either")
        for manifest in (PLUGIN / ".claude-plugin" / "plugin.json", PLUGIN / "plugin.json"):
            data = json.loads(manifest.read_text())
            self.assertNotIn("hooks", data)
            self.assertNotIn("userConfig", data)

    def test_every_file_is_text_and_under_256_kib_and_there_are_few(self):
        files = repo_files()
        self.assertLess(len(files), 5000)
        self.assertLess(len(repo_files(PLUGIN)), 100)
        for path in files:
            data = path.read_bytes()
            self.assertLess(len(data), 256 * 1024, f"{path.relative_to(ROOT)} is {len(data)} bytes")
            self.assertNotIn(b"\x00", data, f"{path.relative_to(ROOT)} is not text")
            data.decode("utf-8")

    def test_line_endings(self):
        for path in (SCRIPTS / "theodore", ROOT / "install" / "install.sh"):
            self.assertNotIn(b"\r", path.read_bytes(), path.name)
        for path in (SCRIPTS / "theodore.cmd", ROOT / "install" / "install.cmd"):
            data = path.read_bytes()
            self.assertEqual(data.count(b"\n"), data.count(b"\r\n"), f"{path.name}: every line ends CRLF")
            data.decode("ascii")
        attributes = (ROOT / ".gitattributes").read_text()
        self.assertIn("*.cmd text eol=crlf", attributes)
        self.assertIn("plugins/theodore/skills/*/scripts/theodore text eol=lf", attributes)

    def test_shell_syntax(self):
        for script in (SCRIPTS / "theodore", ROOT / "install" / "install.sh"):
            for shell in ("sh", "dash", "bash"):
                exe = shutil.which(shell)
                if exe:
                    done = subprocess.run([exe, "-n", str(script)], capture_output=True, text=True)
                    self.assertEqual(done.returncode, 0, f"{shell} -n {script.name}: {done.stderr}")

    def test_downloads_follow_no_redirect_and_cannot_stall(self):
        for path in (SCRIPTS / "theodore", SCRIPTS / "theodore.cmd", ROOT / "install" / "install.sh", ROOT / "install" / "install.cmd"):
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                if re.search(r"curl(\.exe)?\"? ", line) and not line.lstrip().startswith(("#", "rem ")):
                    self.assertNotRegex(line, r"\s-[a-zA-Z]*L|--location", f"{path.name}: curl must not follow redirects: {line}")
                for call in re.findall(r"curl(?:\.exe)?\"? +(-\S*)", line):
                    if not line.lstrip().startswith(("#", "rem ")):
                        self.assertEqual(call, "-q", f"{path.name}: -q must be curl's first argument (no .curlrc): {line}")
            self.assertIn("--speed-limit 1024 --speed-time 60", text, path.name)
            self.assertIn("--proto", text, path.name)
        sh = (SCRIPTS / "theodore").read_text()
        self.assertIn("--max-redirect=0", sh)
        self.assertIn("WGETRC=/dev/null wget", sh)
        self.assertIn("( sleep 1800; kill \"$wpid\" 2>/dev/null )", sh, "wget has a total time bound")
        for path in (ROOT / "install" / "install.md", ROOT / "README.md"):
            for match in re.findall(r"curl(?:\.exe)? (-\S*)", path.read_text()):
                self.assertEqual(match, "-q", path.name)
        cmd = (SCRIPTS / "theodore.cmd").read_text()
        self.assertIn("MaximumRedirection = 0", cmd)
        self.assertIn("$a.Proxy = $p.GetProxy($u); $a.ProxyUseDefaultCredentials = $true", cmd, "-ProxyUseDefaultCredentials needs -Proxy")
        self.assertIn("/setsecurityflags %BJ% 0x0200", cmd)

    def test_batch_files_have_each_label_once_and_every_jump_lands(self):
        for path in (SCRIPTS / "theodore.cmd", ROOT / "install" / "install.cmd"):
            text = path.read_text(encoding="utf-8")
            labels = re.findall(r"^:(\w+)", text, re.M)
            self.assertEqual(sorted({l for l in labels if labels.count(l) > 1}), [], f"{path.name}: a label twice")
            jumps = set(re.findall(r"(?:goto|call) :(\w+)", text, re.I)) - {"eof"}
            self.assertEqual(sorted(jumps - set(labels)), [], f"{path.name}: a jump to no label")
            for label in ("no_conf", "no_keys"):
                if label in labels:
                    body = text.split(f"\n:{label}\n", 1)[1].split("\n:", 1)[0]
                    self.assertIn("exit /b 78", body, label)
        cmd = (SCRIPTS / "theodore.cmd").read_text(encoding="utf-8")
        self.assertIn('if not exist "%CONF%" goto :no_conf', cmd)
        self.assertIn("launcher.json is missing", cmd.split("\n:no_conf\n", 1)[1].split("\n:", 1)[0])

    def test_no_powershell_script_in_the_bootstrap_path(self):
        for path in (SCRIPTS / "theodore.cmd", ROOT / "install" / "install.cmd"):
            text = path.read_text(encoding="ascii")
            self.assertNotRegex(text, r"(?i)\.ps1")
            self.assertNotRegex(text, r"(?i)-File\b")

    def test_manifests_agree(self):
        claude = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
        portable = json.loads((PLUGIN / "plugin.json").read_text())
        self.assertEqual((claude["name"], portable["name"]), ("theodore", "theodore"))
        self.assertEqual(claude["version"], portable["version"])
        market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
        self.assertEqual(market["name"], "theodore")
        self.assertEqual([(p["name"], p["source"]) for p in market["plugins"]], [("theodore", "./plugins/theodore")])
        codex = json.loads((ROOT / ".agents" / "plugins" / "marketplace.json").read_text())
        self.assertEqual(codex["name"], "theodore")
        self.assertEqual(codex["plugins"][0]["source"], {"source": "local", "path": "./plugins/theodore"})
        self.assertEqual(set(codex["plugins"][0]["policy"]), {"installation", "authentication"})
        # The local MCP server (theodore mcp), registered for Claude Code (.mcp.json) and for Codex (mcp.json, named by plugin.json).
        self.assertEqual(json.loads((PLUGIN / ".mcp.json").read_text()),
                         {"mcpServers": {"theodore": {"command": "sh", "args": ["${CLAUDE_PLUGIN_ROOT}/skills/pptx/scripts/theodore", "mcp"]}}})
        self.assertEqual(json.loads((PLUGIN / "mcp.json").read_text()),
                         {"mcpServers": {"theodore": {"command": "sh", "args": ["./skills/pptx/scripts/theodore", "mcp"], "cwd": "."}}})
        self.assertEqual(portable["mcpServers"], "./mcp.json")

    @unittest.skipUnless(shutil.which("claude"), "the claude CLI is not installed")
    def test_claude_plugin_validate(self):
        with tempfile.TemporaryDirectory() as home:
            env = {**os.environ, "HOME": home, "CLAUDE_CONFIG_DIR": str(Path(home) / ".claude")}
            done = subprocess.run(["claude", "plugin", "validate", str(PLUGIN)], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("Validation passed", done.stdout)


class Locks(unittest.TestCase):
    def test_locks_are_the_recorded_copies(self):
        source = (ROOT / "runtime" / "locks" / "SOURCE.md").read_text()
        for name in ("requirements.txt", "package.json", "package-lock.json"):
            digest = hashlib.sha256((ROOT / "runtime" / "locks" / name).read_bytes()).hexdigest()
            self.assertIn(f"| `{name}` | `{digest}` |", source, f"{name} drifted from SOURCE.md")

    def test_extras_are_pinned_with_hashes(self):
        text = (ROOT / "runtime" / "locks" / "runtime-extra.txt").read_text()
        pins = re.findall(r"^([a-z0-9_-]+)==([0-9.]+)", text, re.M)
        self.assertEqual([p[0] for p in pins], ["pypdfium2", "comtypes", "truststore"])
        self.assertGreaterEqual(text.count("--hash=sha256:"), len(pins))
        vendored = (ROOT / "THIRD-PARTY-NOTICES.md").read_text()
        self.assertIn(f"truststore {dict(pins)['truststore']}", vendored, "the vendored truststore is the pinned version")


class Contract(unittest.TestCase):
    """client/theodore_client/contract.py against the snapshot of the backend's frozen contract."""

    def setUp(self):
        self.snapshot = json.loads((FIXTURES / "contract-v1.json").read_text())
        from theodore_client import contract
        self.contract = contract

    def test_snapshot_is_current_when_the_backend_is_checked_out(self):
        openapi = Path(os.environ.get("THEODORE_OPENAPI") or ROOT.parent / "theodore" / "packages" / "contracts" / "openapi.json")
        if not openapi.exists():
            self.skipTest("the backend repository is not checked out beside this one")
        done = subprocess.run([sys.executable, str(ROOT / "tools" / "snapshot_contract.py"), str(openapi), "--check"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, "run tools/snapshot_contract.py:\n" + done.stdout)

    def test_every_route_the_client_calls_is_in_the_contract(self):
        published = {(r["method"], r["path"]): r for r in self.snapshot["routes"].values()}
        for name, (method, template, needs_bearer) in self.contract.ROUTES.items():
            path = re.sub(r"\{[a-z_]+\}", lambda m: m.group(0), template)
            if name in self.contract.PENDING_ROUTES:        # being added by the backend: not in its published contract yet
                self.assertNotIn((method, path), published, f"{name} is in the contract now: take it out of PENDING_ROUTES")
                continue
            self.assertIn((method, path), published, f"{name}: {method} {path}")
            self.assertEqual(published[(method, path)]["auth"] == "installation", needs_bearer, f"{name}: bearer or public")

    def test_error_codes_and_statuses(self):
        for code, (status, launcher_status) in self.contract.API_ERRORS.items():
            self.assertEqual(self.snapshot["error_codes"].get(code), status, code)
            self.assertIn(launcher_status, self.contract.EXIT)
        self.assertEqual(sorted(self.contract.DEVICE_ERRORS), self.snapshot["device_errors"]["codes"])
        self.assertEqual(self.snapshot["device_errors"]["status"], 400)

    def test_launcher_constants(self):
        launcher = self.snapshot["launcher"]
        self.assertEqual({k: v for k, v in self.contract.EXIT.items() if k != "OK"}, launcher["exit_codes"])
        self.assertEqual(list(self.contract.SETUP_FAILED_CAUSES), launcher["setup_failed_causes"])
        self.assertEqual(self.contract.GUIDE_PART_CHARS, launcher["guide_part_max_chars"])
        self.assertEqual(self.contract.UPDATE_CHECK_INTERVAL_S, launcher["update_check_interval_seconds"])
        from theodore_client import cli
        commands = {a.dest: a for a in cli._parser()._subparsers._group_actions}["command"].choices
        for command in launcher["client_commands"]:
            self.assertTrue(command in commands or command in ("pptx", "python"), command)
        from theodore_client.core import Status
        self.assertRegex(Status("RETRY", "a", "b", "c").render(), launcher["message_pattern"].strip("/").replace("\\n", "\n"))

    def test_entitlement_limits_and_names(self):
        ent = self.snapshot["entitlement"]
        self.assertEqual((self.contract.ENTITLEMENT_ALG, self.contract.ENTITLEMENT_TYP, self.contract.ENTITLEMENT_AUD, self.contract.ENTITLEMENT_ENV),
                         (ent["alg"], ent["typ"], ent["aud"], ent["env_var"]))
        limits = self.snapshot["limits"]
        self.assertEqual(self.contract.PREFERENCE_MAX_CHARS, limits["preference_max_length"])
        self.assertEqual(self.contract.PREFERENCE_MAX_COUNT, limits["preference_max_count"])
        self.assertEqual(self.contract.REPORT_MAX_BYTES, limits["report_body_max_bytes"])
        self.assertEqual(self.contract.DEVICE_CLIENT_ID, self.snapshot["codes"]["device_client_id"])
        self.assertEqual((self.contract.CHANNEL_MANIFEST_SCHEMA, self.contract.RUNTIME_MANIFEST_SCHEMA),
                         (self.snapshot["manifests"]["channel"], self.snapshot["manifests"]["runtime"]))
        self.assertEqual(list(self.contract.KNOWN_HOSTS), self.snapshot["known"]["hosts"])
        self.assertEqual(list(self.contract.SKILLS), self.snapshot["known"]["skills"])
        for header in (self.contract.HEADER_GUIDE_PARTS, self.contract.HEADER_GUIDE_PART, self.contract.HEADER_GUIDE_VERSION):
            self.assertIn(header, self.snapshot["guide_headers"])
        config = json.loads((SCRIPTS / "launcher.json").read_text())
        production = self.snapshot["origins"]["production"]
        self.assertEqual((config["api_base"], config["dl_base"]), (production["api"], production["dl"]))
        self.assertEqual(config["marketplace_url"], production["public"] + "/marketplace.json")

    def test_one_time_code_examples(self):
        pattern = re.compile(self.contract.ACTIVATION_CODE_PATTERN)
        for typed in ("ABCD-1234", "abcd 1234", "ABCD1234", "wxyz-0r95"):
            self.assertTrue(pattern.match(typed), typed)
        for typed in ("ABCD-123", "ABCD-12345", "AB-CD-1234", ""):
            self.assertFalse(pattern.match(typed), typed)

    def test_all_http_goes_through_one_module(self):
        for path in (ROOT / "client" / "theodore_client").glob("*.py"):
            if path.name != "api.py":
                text = path.read_text()
                self.assertNotRegex(text, r"\burllib\.request\b|\bhttp\.client\b|\bimport requests\b|\bsocket\.create_connection\b",
                                    f"{path.name} talks to the network outside api.py")


class Documents(unittest.TestCase):
    def test_sizes_are_the_measured_ones_everywhere(self):
        for path in [ROOT / "README.md", ROOT / "install" / "install.md", ROOT / "install" / "install.sh", ROOT / "install" / "install.cmd",
                     PLUGIN / "README.md", SCRIPTS / "theodore", SCRIPTS / "theodore.cmd"]:
            text = path.read_text(encoding="utf-8")
            for stale in ("120 MB", "82 MB", "380 MB", "about 80 MB"):
                self.assertNotIn(stale, text, f"{path.name}: {stale}")
        readme = (ROOT / "README.md").read_text()
        self.assertIn("about 100 to 130 MB is downloaded before the first deck", readme)
        self.assertIn("about 560 to 670 MB on disk", readme)
        for path in (ROOT / "install" / "install.md", ROOT / "install" / "install.sh", PLUGIN / "README.md", SCRIPTS / "theodore"):
            self.assertNotIn("90 MB", path.read_text(encoding="utf-8"), path.name)
        rows = {}
        for platform in ("macOS, Apple silicon", "macOS, Intel", "Windows x64", "Linux x64", "Linux arm64"):
            self.assertIn(f"| {platform} |", readme)
            row = re.search(rf"\| {re.escape(platform)} \| ([\d.]+) MB \([\d.]+ MiB\) \| ([\d.]+) MB \([\d.]+ MiB\) \|", readme)
            rows[platform] = (float(row.group(1)), float(row.group(2)))
        # The summary row agrees with the table, in the same unit (MB, rounded).
        summary = re.search(r"about (\d+) to (\d+) MB \((\d+) to (\d+) MB to download; table below\)", readme)
        low_disk, high_disk, low_dl, high_dl = (int(g) for g in summary.groups())
        self.assertEqual((low_dl, high_dl), (round(min(d for d, _ in rows.values())), round(max(d for d, _ in rows.values()))))
        self.assertEqual((low_disk, high_disk), (round(min(u for _, u in rows.values())), round(max(u for _, u in rows.values()))))

    def test_security_describes_reports_off_as_off(self):
        text = (ROOT / "SECURITY.md").read_text()
        self.assertNotIn("the assistant then asks you first", text)
        self.assertIn("the assistant does not ask you about\nreports and files one only when you ask it to", text)
        self.assertIn("redact.py", text)


class StampedSite(unittest.TestCase):
    """A build stamped for an environment names that environment's site everywhere it prints or serves a link."""

    PRODUCTION = re.compile(r"(?<![A-Za-z0-9.@-])theodoreand\.ai|https://(?:api|dl|mcp)\.theodoreand\.ai")

    def test_a_staging_stamped_tree_names_no_production_host(self):
        import sync_launchers
        tree = sync_launchers.stamped_tree("staging")
        for rel in ("install/install.md", "install/install.sh", "install/install.cmd", "plugins/theodore/skills/pptx/scripts/theodore",
                    "plugins/theodore/skills/deck-design/scripts/theodore.cmd", "plugins/theodore/skills/pptx/scripts/launcher.json",
                    "plugins/theodore/.claude-plugin/plugin.json", "plugins/theodore/skills/pptx/SKILL.md", "client/theodore_client/commands.py"):
            self.assertIn(rel, tree)
        self.assertFalse([rel for rel in tree if rel.startswith(("tests/", "tools/", "runtime/", "ci/", "client/truststore/"))])
        for rel, data in sorted(tree.items()):
            if rel.rsplit("/", 1)[-1] in ("LICENSE", "NOTICE"):        # licence text names the terms page as it is
                continue
            text = data.decode("utf-8")
            hits = [line.strip()[:160] for line in text.splitlines() if self.PRODUCTION.search(line)]
            self.assertEqual(hits, [], rel)
        page = tree["install/install.md"].decode("utf-8")
        self.assertEqual(page.count("https://staging.theodoreand.ai/install.sh"), 1)
        self.assertEqual(page.count("https://staging.theodoreand.ai/install.cmd"), 2)
        self.assertIn("claude plugin install theodore --marketplace https://staging.theodoreand.ai/marketplace.json", page)
        self.assertIn("the same value is shown on https://staging.theodoreand.ai/install.", page)
        self.assertEqual(page.count("{{ACTIVATION_CODE}}"), 6)
        self.assertIn("(compare with https://staging.theodoreand.ai/install)", tree["install/install.sh"].decode("utf-8"))
        cmd = tree["install/install.cmd"]
        self.assertIn(b"(compare with https://staging.theodoreand.ai/install)", cmd)
        self.assertEqual(cmd.count(b"\r\n"), cmd.count(b"\n"), "install.cmd keeps its CRLF line ends")
        self.assertIn(b"support@theodoreand.ai", tree["install/install.sh"], "the support address is the same everywhere")
        config = json.loads(tree["plugins/theodore/skills/pptx/scripts/launcher.json"])
        self.assertEqual(config["site_base"], "https://staging.theodoreand.ai")
        for name in ("theodore", "theodore.cmd", "launcher.json"):
            self.assertEqual(tree[f"plugins/theodore/skills/pptx/scripts/{name}"], tree[f"plugins/theodore/skills/deck-design/scripts/{name}"])

    def test_the_repository_itself_is_the_production_build(self):
        import sync_launchers
        tree = sync_launchers.stamped_tree(None)
        for rel, data in tree.items():
            self.assertEqual(data, (ROOT / rel).read_bytes(), f"{rel}: without --env nothing but the pins changes")
        self.assertEqual(json.loads((SCRIPTS / "launcher.json").read_text())["site_base"], "https://theodoreand.ai")
        self.assertEqual(sync_launchers.stamp_site(sync_launchers.stamp_site("see theodoreand.ai/x", "staging"), "staging"), "see staging.theodoreand.ai/x")

    def test_codex_is_documented_by_the_route_that_works_today(self):
        for path in (ROOT / "install" / "install.md", ROOT / "README.md"):
            text = path.read_text()
            github = [line for line in text.splitlines() if "codex plugin marketplace add Theodore-and/theodore-plugin" in line]
            self.assertTrue(github, path.name)
            for line in github:          # the repository is public and is the production build: the GitHub form is plain
                self.assertNotIn("nce the plugin repository is public", line, path.name)
            self.assertIn("plugin", text)
            self.assertRegex(text, r"codex plugin marketplace add [\"~%]", f"{path.name}: the local marketplace the installer keeps")

    def test_readme_sizes_and_residue(self):
        text = (ROOT / "README.md").read_text()
        self.assertIn("43 to 58 MB each (15 to 21 MB to download)", text)
        self.assertNotIn("about 2.3 MB", text, "the engine download is the compiled build's, measured")
        self.assertNotIn("github.com", (PLUGIN / "README.md").read_text(), "the plugin's README sends no one to the private repository")
        for word in ("pptx-soffice-profile", "theodore-pptx-locks", "theodore-specimens-"):
            self.assertIn(word, text)


class InstallPage(unittest.TestCase):
    def test_install_md_has_the_two_commands_and_the_substitution_point(self):
        text = (ROOT / "install" / "install.md").read_text()
        self.assertEqual(text.count("{{ACTIVATION_CODE}}"), 6, "in the three commands, twice in the first line's address, once in the note for the Worker")
        # The first line says how to read the page word for word, with the address quoted for a shell (zsh globs "?").
        first = text.splitlines()[0]
        self.assertIn("not through a tool that summarises web pages", first)
        self.assertIn("curl -q --proto '=https' -fsS 'https://theodoreand.ai/install.md?code={{ACTIVATION_CODE}}'", first)
        self.assertIn('curl.exe -q --proto =https -fsS "https://theodoreand.ai/install.md?code={{ACTIVATION_CODE}}"', first)
        self.assertIn("exactly as written", first)
        self.assertIn("This may run in a cloud sandbox session", text)
        # A new private folder each time: mktemp -d (0700) on POSIX, a random name created exclusively under %TEMP% on
        # Windows; never a fixed path another user could create first.
        self.assertIn('( d=$(mktemp -d) && curl -q --proto \'=https\' -fsS -o "$d/install.sh" https://theodoreand.ai/install.sh && sh "$d/install.sh" --code {{ACTIVATION_CODE}}; rc=$?; rm -rf "$d"; exit $rc )', text)
        self.assertIn('New-Item -ItemType Directory -Path $d -ErrorAction Stop', text)
        self.assertIn('mkdir "!D!" && curl.exe -q --proto =https -fsS -o "!D!\\install.cmd" https://theodoreand.ai/install.cmd && call "!D!\\install.cmd" --code {{ACTIVATION_CODE}}', text)
        for path in (ROOT / "install" / "install.md", ROOT / "README.md"):
            page = path.read_text()
            self.assertNotIn("/tmp/theodore-install", page, path.name)
            self.assertNotIn("%TEMP%\\theodore-install.cmd", page, path.name)
            self.assertNotIn("curl -fsSL", page, f"{path.name}: no redirect is followed (-L)")
        self.assertIn("claude plugin install theodore --marketplace https://theodoreand.ai/marketplace.json", text)
        self.assertIn("codex plugin add theodore@theodore", text)
        self.assertIn("If a command prints a line starting with THEODORE, follow its NEXT line.", text)
        self.assertIn("Never ask the user for a password or\nAPI key.", text)


if __name__ == "__main__":
    unittest.main()
