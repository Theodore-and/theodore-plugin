#!/usr/bin/env python3
"""Times what the native front itself costs (PLAN lane C, DESIGN §2.2), against the stand-in service of its tests, so
the numbers are the front's own and not a verb's: the service answers each call at once.

  front alone          `theodore __front version`: the program starting and ending (no connection)
  warm call            `theodore pptx echo x` with the service running: start, connect, hello, call, answer, exit
  bash, in one shell   the same two inside one running bash, each by `time` (the shell's start not counted), beside
                       /usr/bin/true (or Windows' where.exe) as the floor of any program started from that shell
  bash -c, end to end  a new bash per command, as an assistant's Bash tool runs one: `bash -c true` (the shell's floor),
                       `bash -c '<front> pptx echo x'`
  powershell           inside one running PowerShell, Measure-Command per command (Windows)
  relay initialize     `theodore mcp`: from the start of the process to its answer to `initialize`
  cold start           no service: the first command starts it (the stand-in starts in about the time of one Python)
  zero programs        macOS: a warm call under a sandbox profile that forbids creating any process (it must answer)

Usage: python3 tools/front_measure.py --bin <front> [--runs 30] [--out <file.json>]
Windows: run it with a full Python (or the embeddable one without its ._pth); bash is Git Bash's when found.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IS_WINDOWS = os.name == "nt"


def summary(values: list[float]) -> dict:
    values = sorted(values)
    p95 = values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))]
    return {"n": len(values), "median_ms": round(statistics.median(values) * 1000, 2), "p95_ms": round(p95 * 1000, 2),
            "min_ms": round(values[0] * 1000, 2)}


def load() -> str:
    try:
        return " ".join(f"{x:.1f}" for x in os.getloadavg())
    except (AttributeError, OSError):
        return "n/a"


class Bench:
    def __init__(self, binary: Path, runs: int):
        self.bin, self.runs = binary.resolve(), runs
        base = "/tmp" if not IS_WINDOWS and Path("/tmp").is_dir() else None
        self.tmp = Path(tempfile.mkdtemp(prefix="tfm", dir=base)).resolve()
        self.home = self.tmp / "h"
        client = self.tmp / "client"
        client.mkdir(parents=True)
        (client / "__main__.py").write_text(
            f"import sys\nsys.path.insert(0, {str(ROOT / 'tests')!r})\nimport front_fake\nraise SystemExit(front_fake.main(sys.argv[1:]))\n",
            encoding="utf-8")
        (self.home / "bin").mkdir(parents=True)
        (self.home / "bin" / "launch.json").write_text(json.dumps({
            "v": 1, "home": str(self.home), "runtime_python": sys.executable, "client_dir": str(client), "client_version": "0.0.0-measure",
            "engine_dir": str(self.home / "engines" / "x"), "engine_version": "x"}), encoding="utf-8")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("THEODORE_", "CLAUDE", "CODEX_", "FAKE_"))}
        self.env.update({"THEODORE_HOME": str(self.home), "FAKE_SERVICE_IDLE": "120"})
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.service: subprocess.Popen | None = None

    def start_service(self) -> None:
        self.stop_service()
        self.service = subprocess.Popen([sys.executable, "-I", str(self.tmp / "client"), "service", "--home", str(self.home)],
                                        env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 20
        while not (self.home / "state" / "run" / "service.json").exists():
            if time.monotonic() > deadline:
                raise SystemExit("the stand-in service did not start")
            time.sleep(0.01)

    def stop_service(self) -> None:
        if self.service is not None and self.service.poll() is None:
            self.service.terminate()
            self.service.wait(timeout=10)
        self.service = None
        for event in self.events("service-started"):
            try:
                os.kill(event["pid"], signal.SIGTERM)
            except OSError:
                pass
        for name in ("service.json", "start-failed"):
            try:
                (self.home / "state" / "run" / name).unlink()
            except OSError:
                pass

    def events(self, kind: str) -> list[dict]:
        path = self.home / "state" / "run" / "events.jsonl"
        if not path.exists():
            return []
        return [e for e in (json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()) if e.get("e") == kind]

    def timed(self, argv: list[str], **kw) -> float:
        started = time.perf_counter()
        done = subprocess.run(argv, env=self.env, cwd=str(self.work), capture_output=True, **kw)
        elapsed = time.perf_counter() - started
        if done.returncode != 0:
            raise SystemExit(f"{argv} answered {done.returncode}: {done.stderr[-400:]!r}")
        return elapsed

    def repeat(self, argv: list[str]) -> dict:
        self.timed(argv)                      # one first, not counted (file caches)
        return summary([self.timed(argv) for _ in range(self.runs)])

    def bash(self) -> str | None:
        if IS_WINDOWS:
            for candidate in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
                if Path(candidate).exists():
                    return candidate
            return None
        return shutil.which("bash")

    def in_bash(self, bash: str) -> dict:
        """One bash, many commands, each timed by the shell itself (TIMEFORMAT %R, milliseconds)."""
        front = self.bin.as_posix() if not IS_WINDOWS else subprocess.run([bash, "-c", f"cygpath -u '{self.bin}'"], capture_output=True,
                                                                            text=True).stdout.strip()
        floor = "/usr/bin/true" if not IS_WINDOWS else "/c/Windows/System32/where.exe /?"
        script = f"""TIMEFORMAT=%3R
for i in $(seq 0 {self.runs}); do
  {{ time "{front}" __front version >/dev/null; }} 2>>"$1.version"
  {{ time "{front}" pptx echo x >/dev/null; }} 2>>"$1.call"
  {{ time {floor} >/dev/null; }} 2>>"$1.floor"
done
"""
        stem = self.tmp / "bash-times"
        (self.tmp / "loop.sh").write_text(script, encoding="utf-8")
        subprocess.run([bash, str(self.tmp / "loop.sh").replace("\\", "/"), str(stem).replace("\\", "/")], env=self.env, cwd=str(self.work),
                       check=True, capture_output=True)
        out = {}
        for name in ("version", "call", "floor"):
            values = [float(v) for v in Path(f"{stem}.{name}").read_text().split()[1:]]
            out[name] = summary(values)
        return out

    def powershell(self) -> dict | None:
        exe = shutil.which("powershell") or shutil.which("pwsh")
        if not IS_WINDOWS or not exe:
            return None
        script = f"""$front = '{self.bin}'
$where = "$env:SystemRoot\\System32\\where.exe"
& $front pptx echo x | Out-Null
$rows = @()
for ($i = 0; $i -lt {self.runs}; $i++) {{
  $v = (Measure-Command {{ & $front __front version | Out-Null }}).TotalMilliseconds
  $c = (Measure-Command {{ & $front pptx echo x | Out-Null }}).TotalMilliseconds
  $f = (Measure-Command {{ & $where /? | Out-Null }}).TotalMilliseconds
  $rows += "$v $c $f"
}}
$rows -join "`n"
"""
        (self.tmp / "loop.ps1").write_text(script, encoding="utf-8")
        done = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(self.tmp / "loop.ps1")],
                              env=self.env, cwd=str(self.work), capture_output=True, text=True, check=True)
        rows = [[float(x) / 1000 for x in line.split()] for line in done.stdout.split("\n") if line.strip()]
        return {"version": summary([r[0] for r in rows]), "call": summary([r[1] for r in rows]), "floor": summary([r[2] for r in rows])}

    def relay_initialize(self) -> dict:
        values = []
        message = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18",
                              "capabilities": {}, "clientInfo": {"name": "claude-code", "version": "measure"}}}).encode() + b"\n"
        for i in range(self.runs + 1):
            started = time.perf_counter()
            proc = subprocess.Popen([str(self.bin), "mcp"], env=self.env, cwd=str(self.work), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
            proc.stdin.write(message)
            proc.stdin.flush()
            line = proc.stdout.readline()
            elapsed = time.perf_counter() - started
            assert json.loads(line)["id"] == 1, line
            proc.stdin.close()
            proc.wait(timeout=20)
            proc.stdout.close()
            if i:
                values.append(elapsed)
        return summary(values)

    def cold(self, times: int) -> dict:
        values = []
        for _ in range(times):
            self.stop_service()
            time.sleep(0.2)
            values.append(self.timed([str(self.bin), "pptx", "echo", "cold"]))
        return summary(values)

    def zero_programs(self) -> str:
        if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").exists():
            return "not measured here (macOS sandbox profiles; Windows: process-creation audit, lanes/C-rig.md)"
        done = subprocess.run(["/usr/bin/sandbox-exec", "-p", "(version 1)(allow default)(deny process-fork)", str(self.bin), "pptx", "echo", "z"],
                              env=self.env, cwd=str(self.work), capture_output=True, text=True)
        served = done.returncode == 0 and json.loads(done.stdout.strip().splitlines()[-1]).get("args") == ["echo", "z"]
        return "pass: answered by the service with process creation forbidden" if served else f"FAIL: {done.returncode} {done.stderr[-300:]}"

    def run(self) -> dict:
        result = {"binary": str(self.bin), "size_bytes": self.bin.stat().st_size, "platform": sys.platform, "runs": self.runs,
                  "load_before": load(), "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        self.start_service()
        result["front_alone"] = self.repeat([str(self.bin), "__front", "version"])
        result["warm_call"] = self.repeat([str(self.bin), "pptx", "echo", "x"])
        bash = self.bash()
        if bash:
            result["bash_in_one_shell"] = self.in_bash(bash)
            result["bash_c_floor"] = self.repeat([bash, "-c", "true"])
            front = self.bin.as_posix() if not IS_WINDOWS else str(self.bin)
            result["bash_c_warm_call"] = self.repeat([bash, "-c", f"'{front}' pptx echo x"])
        result["powershell_in_one_shell"] = self.powershell()
        result["relay_initialize"] = self.relay_initialize()
        result["zero_programs"] = self.zero_programs()
        result["cold_start"] = self.cold(max(3, self.runs // 6))
        self.stop_service()
        result["load_after"] = load()
        return result

    def close(self) -> None:
        self.stop_service()
        shutil.rmtree(self.tmp, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bin", required=True)
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--out")
    args = parser.parse_args()
    bench = Bench(Path(args.bin), args.runs)
    try:
        result = bench.run()
    finally:
        bench.close()
    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
