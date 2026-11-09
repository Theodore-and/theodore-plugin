#!/usr/bin/env python3
"""Lifecycle in a clean home with the real runtime bundle and a real engine payload (gate G3).

install -> first run -> guide -> blank build -> update -> rollback -> offline grace -> uninstall, against the
fake API, with the real `claude` and `codex` CLIs registering the plugin in a throwaway home when they are on
PATH. Nothing is read from or written to the real home; the clean home is deleted at the end unless --keep.

  python3 tests/e2e/lifecycle.py --bundle <dir from build_bundle.py> --engine-zip <payload.zip> --out <evidence dir>
                                 [--packs-dir <dir with pack-<id>-*.zip>] [--host-path <dir:dir>] [--keep]

The engine payload is private and never enters this repository: pass a zip built elsewhere. Each step is
written to <out>/steps.log with its exit code and duration; <out>/summary.txt has one PASS/FAIL line per check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from support import ROOT, FakeApi, Sandbox, pack_zip, sha256_hex  # noqa: E402

DAY = 86400


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--engine-zip", required=True)
    parser.add_argument("--packs-dir")
    parser.add_argument("--out", required=True)
    parser.add_argument("--host-path", default="", help="extra PATH entries (where claude, codex, node, soffice, pdftoppm live)")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    log = open(out / "steps.log", "w", encoding="utf-8")
    summary: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> bool:
        line = f"{'PASS' if ok else 'FAIL'}  {name}{': ' + detail if detail else ''}"
        summary.append(line)
        print(line, flush=True)
        return ok

    bundle = Path(args.bundle)
    manifest = json.loads(next(bundle.glob("*.manifest.json")).read_text())
    api = FakeApi().start()
    api.publish_runtime(manifest["runtime_id"], {p["part"]: (p["file"], bundle / p["file"]) for p in manifest["parts"] if p["part"] != "client"})
    engine_bytes = Path(args.engine_zip).read_bytes()
    version_a, version_b = "1.0.0-rc.2", "1.0.0-rc.3"
    api.publish_engine(version_a, engine_bytes)
    for pack in ("icons", "flags", "backgrounds", "maps"):
        real = sorted(Path(args.packs_dir).glob(f"pack-{pack}-*.zip")) if args.packs_dir else []
        api.publish_pack(pack, "1.0.0", real[-1].read_bytes() if real else pack_zip(pack, "1.0.0"))
    api.guides["deck-design"][""] = "# Deck design\n\nLIFECYCLE-GUIDE-MARKER (fake guidance served by the fake API).\n"

    box = Sandbox(api, home_name=".theodore")
    box.home = box.user_home / ".theodore"                       # the default location inside the clean home
    box.env["THEODORE_HOME"] = str(box.home)
    box.env["PATH"] = os.pathsep.join([p for p in args.host_path.split(os.pathsep) if p] + [box.env["PATH"]])
    box.env["THEODORE_DL_BASE"] = f"{api.base}/dl"
    box.env["PPTX_RUNTIME_DAEMON"] = "0"
    (box.user_home / ".codex").mkdir()                            # Codex refuses a CODEX_HOME that does not exist
    box.write_launcher_json()
    box.serve_plugin_archive()
    theodore = box.home / "bin" / "theodore"
    foreign = box.home / "memory" / "notes.txt"                  # the folder exists before Theodore and holds something else
    foreign.parent.mkdir(parents=True)
    foreign.write_text("not Theodore's\n", encoding="utf-8")

    def run(label: str, command: list[str], env: dict | None = None, timeout: float = 1800):
        started = time.monotonic()
        done = subprocess.run(command, env={**box.env, **(env or {})}, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, cwd=str(box.tmp))
        elapsed = time.monotonic() - started
        log.write(f"\n$ [{label}] {' '.join(command)}\n{done.stdout}{done.stderr}[rc={done.returncode}] [{elapsed:.1f}s]\n")
        log.flush()
        done.elapsed = elapsed
        return done

    def launcher(label: str, *argv: str, env: dict | None = None):
        return run(label, ["/bin/sh", str(theodore), *argv], env)

    def tree(name: str) -> int:
        rows = []
        for path in sorted(box.user_home.rglob("*")):
            if path.is_symlink():
                rows.append(f"{'link':>12}  {'-':64}  {path.relative_to(box.user_home)} -> {os.readlink(path)}")
            elif path.is_file():
                rows.append(f"{path.stat().st_size:>12}  {hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(box.user_home)}")
        (out / name).write_text("\n".join(rows) + "\n", encoding="utf-8")
        return len(rows)

    def du(path: Path) -> int:
        return sum(p.lstat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink()) if path.exists() else 0

    real_home = Path(os.path.expanduser("~"))
    watched = [real_home / ".claude" / "plugins" / "installed_plugins.json", real_home / ".claude" / "plugins" / "known_marketplaces.json",
               real_home / ".claude" / "settings.json", real_home / ".codex" / "config.toml"]

    def real_state() -> dict:
        state = {str(p): (hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "absent") for p in watched}
        state["~/.theodore exists"] = (real_home / ".theodore").exists()
        if sys.platform == "darwin":  # a read-only look: the login keychain must hold no Theodore item
            found = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", "ai.theodoreand.theodore"], capture_output=True)
            state["login keychain item"] = found.returncode == 0
        return state

    before_real = real_state()
    rc = 1
    try:
        # --- install ---------------------------------------------------------------------------------------------
        api.issue_activation_code("TEST-0001")
        done = run("install", ["/bin/sh", str(ROOT / "install" / "install.sh"), "--code", "TEST-0001"])
        check("install: installer exits 0 and ends with doctor's line for the user",
              done.returncode == 0 and done.stdout.strip().splitlines()[-1].startswith("TELL THE USER: Theodore is ready"), f"{done.elapsed:.1f}s")
        check("install: the code is not echoed", "TEST-0001" not in done.stdout + done.stderr)
        box.wait_jobs(600)
        files = tree("tree-after-install.txt")
        sizes = {name: du(box.home / name) for name in ("runtime", "engines", "packs", "client", "plugin", "bin", "state")}
        (out / "sizes.json").write_text(json.dumps({"files": files, "bytes": sizes, "install_seconds": round(done.elapsed, 1)}, indent=2))
        check("install: runtime, engine and four packs are in place",
              (box.home / "runtime" / manifest["runtime_id"] / ".ready").exists()
              and json.loads((box.home / "engines" / "current.json").read_text())["version"] == version_a
              and {v["status"] for v in json.loads((box.home / "packs" / "state.json").read_text()).values()} == {"ready"})
        for host, probe in (("claude", ["plugin", "list", "--json"]), ("codex", ["plugin", "list"])):
            exe = shutil.which(host, path=box.env["PATH"])
            if not exe:
                summary.append(f"NOT RUN  {host}: not on PATH")
                continue
            listed = run(f"{host} plugin list", [exe, *probe])
            check(f"{host}: theodore is registered in the clean home", "theodore" in listed.stdout, listed.stdout.strip().replace("\n", " ")[:160])
            if host == "claude":
                details = run("claude plugin details", [exe, "plugin", "details", "theodore@theodore"])
                check("claude: the plugin's inventory lists pptx and deck-design", "pptx" in details.stdout and "deck-design" in details.stdout)

        # --- first run, guide, blank build ---------------------------------------------------------------------------
        guide = launcher("guide pptx", "guide", "pptx")
        check("first run: guide pptx prints the framed manual", guide.returncode == 0 and guide.stdout.startswith(f"=== THEODORE pptx {version_a} (beta), part 1/")
              and guide.stdout.strip().splitlines()[-1].startswith("=== END theodore pptx SKILL.md"), f"{guide.elapsed:.1f}s, {len(guide.stdout)} chars")
        design = launcher("guide deck-design", "guide", "deck-design")
        check("guide deck-design comes from the API", design.returncode == 0 and "LIFECYCLE-GUIDE-MARKER" in design.stdout)
        leaked = [str(p) for p in box.user_home.rglob("*") if p.is_file() and not p.is_symlink() and p.stat().st_size < 5_000_000
                  and b"LIFECYCLE-GUIDE-MARKER" in p.read_bytes()]
        check("deck-design text is nowhere on disk", not leaked, ", ".join(leaked))
        workspace = box.tmp / "deck one"
        workspace.mkdir()
        start = launcher("pptx start", "pptx", "start", "--blank", "--output", str(workspace / "output" / "smoke.pptx"), "--objective", "lifecycle smoke", str(workspace))
        apply = launcher("pptx apply", "pptx", "apply", str(workspace))
        deck = workspace / "output" / "smoke.pptx"
        check("blank build: start and apply exit 0 and the deck exists", start.returncode == 0 and apply.returncode == 0 and deck.is_file() and deck.read_bytes()[:2] == b"PK",
              f"start {start.elapsed:.1f}s, apply {apply.elapsed:.1f}s, rc {start.returncode}/{apply.returncode}")
        check("workspace is pinned to the engine that started it",
              json.loads((workspace / ".pptx-state" / "theodore-engine.json").read_text()).get("version") == version_a if (workspace / ".pptx-state" / "theodore-engine.json").exists() else False)
        doctor = launcher("doctor", "doctor")
        check("doctor: ready", doctor.returncode == 0, doctor.stdout.strip().splitlines()[-1][:200])

        # --- update ---------------------------------------------------------------------------------------------------
        api.publish_engine(version_b, engine_bytes + b"")       # same payload under a new version label
        api.engine["sha256"] = sha256_hex(engine_bytes)
        seven_h = {"THEODORE_TEST_CLOCK_OFFSET": str(7 * 3600)}
        upd = launcher("guide after publish", "guide", "pptx", env=seven_h)
        check("update: the next guide after six hours switches to the new engine",
              upd.stdout.startswith(f"=== THEODORE pptx {version_b} ") and json.loads((box.home / "engines" / "current.json").read_text()) == {"version": version_b, "previous": version_a},
              f"{upd.elapsed:.1f}s")
        old_ws = launcher("old workspace apply", "pptx", "apply", str(workspace), env=seven_h)
        check("update: the open workspace still builds on its own engine", old_ws.returncode == 0)

        # --- rollback -------------------------------------------------------------------------------------------------
        api.publish_engine(version_a, engine_bytes)
        api.clock_offset = 14 * 3600
        fourteen_h = {"THEODORE_TEST_CLOCK_OFFSET": str(14 * 3600)}
        back = launcher("guide after server rollback", "guide", "pptx", env=fourteen_h)
        check("rollback: the server moves the pointer back and the client follows",
              back.stdout.startswith(f"=== THEODORE pptx {version_a} ") and json.loads((box.home / "engines" / "current.json").read_text()) == {"version": version_a, "previous": version_b})
        manual = launcher("update --rollback", "update", "--rollback", env=fourteen_h)
        check("rollback: update --rollback returns to the previous engine", manual.returncode == 0
              and json.loads((box.home / "engines" / "current.json").read_text())["version"] == version_b)

        # --- offline grace (the clock moves through the test seam, not the system clock) ----------------------------------
        api.down = True
        six_d = {"THEODORE_TEST_CLOCK_OFFSET": str(6 * DAY)}
        offline_ok = launcher("offline day 6: apply", "pptx", "apply", str(workspace), env=six_d)
        offline_guide = launcher("offline day 6: guide pptx", "guide", "pptx", env=six_d)
        check("offline: on day 6 the engine and its manual still work", offline_ok.returncode == 0 and offline_guide.returncode == 0)
        design_off = launcher("offline: guide deck-design", "guide", "deck-design", env=six_d)
        check("offline: deck-design needs the network (71)", design_off.returncode == 71)
        eight_d = {"THEODORE_TEST_CLOCK_OFFSET": str(8 * DAY)}
        expired = launcher("offline day 8: apply", "pptx", "apply", str(workspace), env=eight_d)
        check("offline: on day 8 the client answers OFFLINE (71)", expired.returncode == 71 and expired.stdout.startswith("THEODORE OFFLINE: "))
        api.down = False
        api.clock_offset = 8 * DAY
        recovered = launcher("back online day 8: apply", "pptx", "apply", str(workspace), env=eight_d)
        check("offline: one refresh after reconnecting restores it", recovered.returncode == 0)

        # --- uninstall ---------------------------------------------------------------------------------------------------
        tree("tree-before-uninstall.txt")
        gone = run("uninstall", ["/bin/sh", str(ROOT / "install" / "install.sh"), "--uninstall"])
        box.wait_jobs(30)
        left = tree("tree-after-uninstall.txt")
        residue = sorted(str(p.relative_to(box.user_home)) for p in box.user_home.rglob("*") if p.is_file() or p.is_symlink())
        (out / "residue.txt").write_text("\n".join(residue) + "\n", encoding="utf-8")
        check("uninstall: exits 0 and removes every Theodore folder, leaving the foreign one untouched",
              gone.returncode == 0 and sorted(p.name for p in box.home.iterdir()) == ["memory"] and foreign.read_text() == "not Theodore's\n",
              f"{left} files left in the clean home")
        # What may remain is what README.md documents: the assistants' own state files, and Claude Code's cached copy of
        # the plugin, which it marks as orphaned and deletes itself after 14 days.
        cache = ".claude/plugins/cache/theodore/"
        outside = [r for r in residue if not r.startswith((".claude/", ".codex/", ".theodore/memory/"))]
        check("uninstall: nothing is left outside the assistants' own folders", not outside, "; ".join(outside[:8]))
        cached = [r for r in residue if r.startswith(cache)]
        check("uninstall: Claude Code's cached plugin copy is marked orphaned (it deletes it after 14 days)",
              not cached or any(r.endswith("/.orphaned_at") for r in cached), f"{len(cached)} files")
        mentions = []
        for rel in residue:
            path = box.user_home / rel
            if rel.startswith((".claude/", ".codex/")) and not rel.startswith(cache) and path.is_file() and path.stat().st_size < 1_000_000 \
                    and b"theodore" in path.read_bytes().lower():
                mentions.append(rel)
        with open(out / "residue-content.txt", "w", encoding="utf-8") as handle:
            for rel in residue:
                if rel.endswith((".json", ".toml")) and not rel.startswith(cache) and "/backups/" not in rel:
                    handle.write(f"--- {rel}\n{(box.user_home / rel).read_text(errors='replace')}\n")
        check("uninstall: no assistant state file still mentions theodore", not mentions, "; ".join(mentions))
        check("uninstall: the workspace keeps its deck and its pin file", deck.is_file() and (workspace / ".pptx-state" / "theodore-engine.json").exists())
        check("the real home was not touched", real_state() == before_real)
        rc = 0 if all(line.startswith(("PASS", "NOT RUN")) for line in summary) else 1
    finally:
        (out / "summary.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
        log.close()
        api.stop()
        if args.keep:
            print(f"kept: {box.tmp}")
        else:
            box.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
