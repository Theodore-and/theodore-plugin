"""The shim job's test suite in several processes: what `python -m unittest discover -s tests -v` runs, each test once.

    python ci/par_unittest.py <processes> [--only module ...] [--split module ...]

Every test module under tests/ (test_*.py) is one unit of work (with --only, only the modules named there: the runtime
job's launcher and installer suites, what `python -m unittest -v tests.<module> ...` runs); a module named with --split
is listed test by test and
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
# What a unit runs: `python -m unittest -v <names>`, with the tests folder first on the path as discover puts it there
RUNNER = """import os, sys, unittest
mode, names = sys.argv[1], sys.argv[2:]
if mode == "discover":
    sys.path.insert(0, os.path.abspath("tests"))
unittest.main(module=None, argv=["python -m unittest", "-v", *names])
"""


def ids(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from ids(t)
        else:
            yield t.id()


def main() -> int:
    args = sys.argv[1:]
    procs = max(1, int(args[0])) if args else 4
    lists: dict[str, set[str]] = {"--only": set(), "--split": set()}
    cur = None
    for a in args[1:]:
        if a in lists:
            cur = a
        elif cur:
            lists[cur].add(a)
    only, split = lists["--only"], lists["--split"]
    os.chdir(ROOT)
    # The names each way of running gives its tests: `unittest discover -s tests` imports the modules from tests/ as
    # top-level modules (test_client.Class.test); `unittest tests.test_launcher` from the repository (tests.test_launcher.
    # Class.test). A unit runs its names the same way (RUNNER), so every test sees what it sees in the serial run.
    if only:
        sys.path.insert(0, str(ROOT))
        prefix, mode = "tests.", "names"
        discovered = list(ids(unittest.defaultTestLoader.loadTestsFromNames([f"tests.{m}" for m in sorted(only)])))
    else:
        prefix, mode = "", "discover"
        discovered = list(ids(unittest.defaultTestLoader.discover("tests")))
    # a module that fails to import shows up as unittest.loader._FailedTest: run the suite itself then, serially
    if any(i.startswith("unittest.loader") for i in discovered):
        print("par_unittest: a module did not import: the suite runs whole, in one process", flush=True)
        whole = [f"tests.{m}" for m in sorted(only)] if only else ["discover", "-s", "tests"]
        return subprocess.call([sys.executable, "-m", "unittest", "-v", *whole])
    def module_of(test_id: str) -> str:
        return test_id[len(prefix):].split(".")[0]
    modules = sorted({module_of(i) for i in discovered})
    units: list[tuple[str, int, list[str]]] = []
    for m in modules:
        size = (ROOT / "tests" / f"{m}.py").stat().st_size
        names = [i for i in discovered if module_of(i) == m]
        if m in split and len(names) > 1:
            for k in range(procs):
                part = names[k::procs]
                if part:
                    units.append((f"{m}#{k}", size // procs, part))
        else:
            units.append((m, size, [f"{prefix}{m}"]))
    order = sorted(units, key=lambda u: -u[1])
    out: dict[str, tuple[int, str, float]] = {}
    base = Path(tempfile.mkdtemp(prefix="par-"))

    def run(unit):
        name, _, names = unit
        # a neutral name: a test's own output contains its temporary paths, and one asserts that a word (its module's
        # name, "usage") is absent from it
        tmp = Path(tempfile.mkdtemp(prefix="u", dir=base))
        env = dict(os.environ, TMPDIR=str(tmp), TEMP=str(tmp), TMP=str(tmp))
        t0 = time.monotonic()
        p = subprocess.run([sys.executable, "-c", RUNNER, mode, *names], cwd=ROOT, env=env,
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
