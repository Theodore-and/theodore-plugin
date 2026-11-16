"""The shim job's test suite in several processes: what `python -m unittest discover -s tests -v` runs, each test once.

    python ci/par_unittest.py <processes> [--split module ...]

Every test module under tests/ (test_*.py) is one unit of work; a module named with --split is listed test by test and
dealt round-robin into <processes> units of its own (a long module would otherwise be the tail). Units run in a pool of
<processes> processes, longest first by their module's size, each process with its own TMPDIR, TEMP and TMP (the suites
already point HOME and the Theodore folders into temporary folders: tests/support.py). The output of every unit is
printed whole, in a fixed order (by name), after the pool ends, then one line that sums them:

    par_unittest: <n> units, ran <tests> tests in <s> s (wall), failures <f>, errors <e>, skipped <k>: OK|FAILED

Exit 0 only when every unit exited 0 and the units together ran as many tests as discovery lists (none lost, none twice).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def ids(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from ids(t)
        else:
            yield t.id()


def main() -> int:
    args = sys.argv[1:]
    procs = max(1, int(args[0])) if args else 4
    split = set(args[args.index("--split") + 1:]) if "--split" in args else set()
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    discovered = list(ids(unittest.defaultTestLoader.discover("tests", top_level_dir=str(ROOT))))
    # a module that fails to import shows up as unittest.loader._FailedTest: run discovery itself then, serially
    if any(i.startswith("unittest.loader") for i in discovered):
        print("par_unittest: a module did not import: the suite runs whole, in one process", flush=True)
        return subprocess.call([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"])
    modules = sorted({i.split(".")[1] for i in discovered})  # tests.<module>.<class>.<test>
    units: list[tuple[str, int, list[str]]] = []
    for m in modules:
        size = (ROOT / "tests" / f"{m}.py").stat().st_size
        names = [i for i in discovered if i.split(".")[1] == m]
        if m in split and len(names) > 1:
            for k in range(procs):
                part = names[k::procs]
                if part:
                    units.append((f"{m}#{k}", size // procs, part))
        else:
            units.append((m, size, [f"tests.{m}"]))
    order = sorted(units, key=lambda u: -u[1])
    out: dict[str, tuple[int, str, float]] = {}
    base = Path(tempfile.mkdtemp(prefix="par-unittest-"))

    def run(unit):
        name, _, names = unit
        tmp = base / re.sub(r"[^A-Za-z0-9_]", "_", name)
        tmp.mkdir()
        env = dict(os.environ, TMPDIR=str(tmp), TEMP=str(tmp), TMP=str(tmp))
        t0 = time.monotonic()
        p = subprocess.run([sys.executable, "-m", "unittest", "-v", *names], cwd=ROOT, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out[name] = (p.returncode, p.stdout.decode("utf-8", "backslashreplace"), time.monotonic() - t0)

    t0 = time.monotonic()
    with ThreadPoolExecutor(procs) as pool:
        list(pool.map(run, order))
    wall = time.monotonic() - t0
    ran = fails = errors = skipped = 0
    bad = []
    for name in sorted(out):
        code, text, secs = out[name]
        print(f"===== {name}: exit {code}, {secs:.0f} s", flush=True)
        sys.stdout.write(text)
        m = re.search(r"^Ran (\d+) tests? in", text, re.M)
        ran += int(m.group(1)) if m else 0
        tail = text[text.rfind("\nRan "):] if "\nRan " in text else ""
        for word, var in (("failures", "f"), ("errors", "e"), ("skipped", "s")):
            n = sum(int(x) for x in re.findall(rf"{word}=(\d+)", tail))
            if var == "f":
                fails += n
            elif var == "e":
                errors += n
            else:
                skipped += n
        if code != 0:
            bad.append(name)
    ok = not bad and ran == len(discovered)
    if ran != len(discovered):
        print(f"par_unittest: the units ran {ran} tests, discovery lists {len(discovered)}", flush=True)
    print(f"par_unittest: {len(out)} units, ran {ran} tests in {wall:.0f} s (wall), failures {fails}, errors {errors}, "
          f"skipped {skipped}: {'OK' if ok else 'FAILED ' + ' '.join(bad)}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
