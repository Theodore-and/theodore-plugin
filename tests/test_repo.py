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
        if path.is_file() and not (set(path.relative_to(ROOT).parts) & SKIP_DIRS) and path.name != ".DS_Store":
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
        for skill in ("pptx", "deck-design"):
            body = (PLUGIN / "skills" / skill / "SKILL.md").read_text(encoding="utf-8").split("\n---\n", 1)[1]
            self.assertIn(f'sh "<skill folder>/scripts/theodore" guide {skill}', body)
            self.assertIn(f'& "<skill folder>\\scripts\\theodore.cmd" guide {skill}', body)
            self.assertIn("`=== END`", body)
            self.assertIn("A first line starting `THEODORE ` is a status", body)
            self.assertIn("Never substitute another slide tool silently.", body)
            self.assertIn("Give sub-agents the same command.", body)
            self.assertLess(len(body), 2500, "the stub stays small")
        self.assertIn("use the\n   deck-design skill with the host's own slide tools", (PLUGIN / "skills" / "pptx" / "SKILL.md").read_text())
        self.assertIn("Customize > Plugins > Theodore > Connectors", (PLUGIN / "skills" / "deck-design" / "SKILL.md").read_text())

    def test_no_instruction_text_or_engine_code_is_in_the_repository(self):
        for path in repo_files(PLUGIN):
            self.assertNotIn(path.suffix, (".py", ".js", ".mjs", ".ps1"), f"{path}: the plugin carries no program but the launcher")
        self.assertEqual(sorted(p.name for p in (PLUGIN / "skills").iterdir()), ["deck-design", "pptx"])
        self.assertEqual(sorted(p.name for p in (PLUGIN / "skills" / "deck-design").iterdir()), ["LICENSE", "NOTICE", "SKILL.md", "scripts"])
        self.assertEqual(sorted(p.name for p in (PLUGIN / "skills" / "pptx").iterdir()), ["SKILL.md", "scripts"])
        self.assertEqual([p for p in repo_files() if p.suffix == ".ps1"], [], "no PowerShell script anywhere")


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
        self.assertEqual(json.loads((SCRIPTS / "launcher.json").read_text())["manifest_key_1_kid"], "manifest-unset",
                         "the repository carries the placeholder; keys are stamped per environment at release")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as refused:
                build_plugin_archives.build(Path(tmp) / "out", release=True)
            self.assertIn("placeholder manifest key", str(refused.exception))
            tree = Path(tmp) / "tree"
            for rel in (".claude-plugin", ".agents", "plugins"):
                shutil.copytree(ROOT / rel, tree / rel)
            sys.path.insert(0, str(ROOT / "tests"))
            from fake_api import b64u, public_key
            real = b64u(public_key(hashlib.sha256(b"release check").digest()))
            stamped = sync_launchers.stamped_launcher_json(None, {"keys": [{"kid": "manifest-test", "x": real, "purpose": "manifest"}]})
            for skill in ("pptx", "deck-design"):
                (tree / "plugins" / "theodore" / "skills" / skill / "scripts" / "launcher.json").write_text(stamped)
            self.assertEqual(build_plugin_archives.build(Path(tmp) / "out", tree, release=True)["version"],
                             json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())["version"])


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
            self.assertIn("--speed-limit 1024 --speed-time 60", text, path.name)
            self.assertIn("--proto", text, path.name)
        self.assertIn("--max-redirect=0", (SCRIPTS / "theodore").read_text())
        cmd = (SCRIPTS / "theodore.cmd").read_text()
        self.assertIn("MaximumRedirection = 0", cmd)
        self.assertIn("$a.Proxy = $p.GetProxy($u); $a.ProxyUseDefaultCredentials = $true", cmd, "-ProxyUseDefaultCredentials needs -Proxy")
        self.assertIn("/setsecurityflags %BJ% 0x0200", cmd)

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
        for name in (".mcp.json", "mcp.json"):
            self.assertEqual(json.loads((PLUGIN / name).read_text()), {"mcpServers": {}}, "the connector is switched on in phase 2")

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


class InstallPage(unittest.TestCase):
    def test_install_md_has_the_two_commands_and_the_substitution_point(self):
        text = (ROOT / "install" / "install.md").read_text()
        self.assertEqual(text.count("{{ACTIVATION_CODE}}"), 4, "in the three commands, once in the note for the Worker")
        # A new private folder each time: mktemp -d (0700) on POSIX, a random name created exclusively under %TEMP% on
        # Windows; never a fixed path another user could create first.
        self.assertIn('( d=$(mktemp -d) && curl --proto =https -fsS -o "$d/install.sh" https://theodoreand.ai/install.sh && sh "$d/install.sh" --code {{ACTIVATION_CODE}}; rc=$?; rm -rf "$d"; exit $rc )', text)
        self.assertIn('New-Item -ItemType Directory -Path $d -ErrorAction Stop', text)
        self.assertIn('mkdir "!D!" && curl.exe --proto =https -fsS -o "!D!\\install.cmd" https://theodoreand.ai/install.cmd && call "!D!\\install.cmd" --code {{ACTIVATION_CODE}}', text)
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
