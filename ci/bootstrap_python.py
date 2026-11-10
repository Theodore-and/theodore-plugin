#!/usr/bin/env python3
"""Fetch the pinned python-build-standalone CPython for one platform and unpack it: the interpreter the local
runner uses on that platform (the same file users get as Theodore's stage 1). Stdlib only; runs on Python 3.6+.

Usage: python3 ci/bootstrap_python.py <platform> <dest dir> <cache dir>
Prints the path of the interpreter. The archive is checked against the sha256 and size in runtime/locks/versions.json.
"""
import hashlib
import json
import os
import shutil
import sys
import tarfile
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    platform, dest, cache = sys.argv[1], sys.argv[2], sys.argv[3]
    pins = json.load(open(os.path.join(ROOT, "runtime", "locks", "versions.json")))["python"]
    asset = pins["assets"][platform]
    exe = os.path.join(dest, "python", "python.exe" if platform.startswith("win32") else "bin/python3")
    stamp = os.path.join(dest, ".sha256")
    if os.path.exists(exe) and os.path.exists(stamp) and open(stamp).read().strip() == asset["sha256"]:
        print(exe)
        return 0
    os.makedirs(cache, exist_ok=True)
    archive = os.path.join(cache, asset["file"])
    if not os.path.exists(archive) or hashlib.sha256(open(archive, "rb").read()).hexdigest() != asset["sha256"]:
        url = "https://github.com/astral-sh/python-build-standalone/releases/download/%s/%s" % (
            pins["pbs_release"], urllib.parse.quote(asset["file"]))
        with urllib.request.urlopen(url, timeout=300) as resp, open(archive + ".part", "wb") as out:
            shutil.copyfileobj(resp, out, 1 << 20)
        os.replace(archive + ".part", archive)
    data = open(archive, "rb").read()
    if len(data) != asset["size"] or hashlib.sha256(data).hexdigest() != asset["sha256"]:
        os.remove(archive)
        sys.exit("%s does not match its pinned sha256/size" % asset["file"])
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest)
    with tarfile.open(archive) as tar:
        tar.extractall(dest)
    open(stamp, "w").write(asset["sha256"] + "\n")
    print(exe)
    return 0


if __name__ == "__main__":
    sys.exit(main())
