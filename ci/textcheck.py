#!/usr/bin/env python3
"""The light check of a release that changes only text: the two questions ci/run.sh asks that sh does not say plainly.

  textcheck.py version-only <ancestor> <path>...
      prints each path whose change from <ancestor> to HEAD is only the release's version bump: every changed line is a
      line of VERSION_LINES for that file, with the client's version (client/theodore_client/__init__.py) going from the
      ancestor's to HEAD's and, for the client tar's pins, any new sha256 or size (tools/sync_launchers.py --check, in
      the shim job, proves those equal the built tar). Same number of lines, same line endings, nothing else.
  textcheck.py shipped <ancestor build> <head build> <allowed path>...
      exit 0 when the client tar, the plugin archives and the install files built at HEAD (ci/out/archives) differ from
      those built at the ancestor only in members that are the allowed paths: member by member, by sha256

Run by ci/run.sh plan and the `shipped` target with the system python3 (3.8 or later); it only reads.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

CLIENT_INIT = "client/theodore_client/__init__.py"
# The lines a version bump changes, per file. {V}: the client's version; {SHA} and {N}: the client tar's sha256 and
# size, which follow from its bytes; {BASE}: a download base, which must be the same on both sides.
VERSION_LINES = {
    CLIENT_INIT: ['__version__ = "{V}"'],
    "plugins/*/.claude-plugin/plugin.json": ['  "version": "{V}",'],
    "plugins/*/plugin.json": ['  "version": "{V}",'],
    "plugins/*/skills/*/scripts/launcher.json": [
        '  "shim_version": "{V}",',
        '  "stage1_client_version": "{V}",',
        '  "stage1_client_url": "{BASE}/client/client-{V}.tar",',
        '  "stage1_client_sha256": "{SHA}",',
        '  "stage1_client_size": {N},',
    ],
    "install/install.sh": ['PIN_CLIENT_VERSION="{V}"', 'PIN_CLIENT="client/client-{V}.tar {SHA}"'],
    "install/install.cmd": ['set "PIN_CLIENT_VERSION={V}"', 'set "PIN_CLIENT=client/client-{V}.tar {SHA}"'],
}
FREE = {"SHA": "[0-9a-f]{64}", "N": "[0-9]+"}
SAME = {"BASE": 'https://[^"/ ]+'}


def templates_for(path: str) -> list:
    for pattern, lines in VERSION_LINES.items():
        if fnmatch.fnmatchcase(path, pattern):
            return lines
    return []


def line_regex(template: str, version: str):
    out = ""
    for part in re.split(r"(\{[A-Z]+\})", template):
        name = part[1:-1] if part.startswith("{") else None
        if name == "V":
            out += re.escape(version)
        elif name in FREE:
            out += "(?:%s)" % FREE[name]
        elif name in SAME:
            out += "(?P<%s>%s)" % (name, SAME[name])
        else:
            out += re.escape(part)
    return re.compile(out)


def split_eol(line: str):
    body = line.rstrip("\r\n")
    return body, line[len(body):]


def version_only(path: str, old_text: str, new_text: str, old_version: str, new_version: str) -> bool:
    """True when old_text becomes new_text by the version bump alone (see the module's docstring)."""
    templates = templates_for(path)
    if not templates or old_version == new_version or old_text == new_text:
        return False
    old_lines, new_lines = old_text.splitlines(True), new_text.splitlines(True)
    if len(old_lines) != len(new_lines):
        return False
    for old, new in zip(old_lines, new_lines):
        if old == new:
            continue
        (old_body, old_eol), (new_body, new_eol) = split_eol(old), split_eol(new)
        if old_eol != new_eol:
            return False
        for template in templates:
            a = line_regex(template, old_version).fullmatch(old_body)
            b = line_regex(template, new_version).fullmatch(new_body)
            if a and b and a.groupdict() == b.groupdict():
                break
        else:
            return False
    return True


def client_version(text: str) -> str:
    found = re.search(r'^__version__ = "([^"]+)"$', text, re.M)
    return found.group(1) if found else ""


def git_show(rev: str, path: str):
    done = subprocess.run(["git", "show", f"{rev}:{path}"], capture_output=True)
    return done.stdout.decode("utf-8") if done.returncode == 0 else None


def cmd_version_only(ancestor: str, paths: list) -> int:
    old_init, new_init = git_show(ancestor, CLIENT_INIT), git_show("HEAD", CLIENT_INIT)
    if old_init is None or new_init is None:
        return 0
    old_version, new_version = client_version(old_init), client_version(new_init)
    if not old_version or not new_version:
        return 0
    for path in paths:
        if not templates_for(path):
            continue
        old_text, new_text = git_show(ancestor, path), git_show("HEAD", path)
        if old_text is not None and new_text is not None and version_only(path, old_text, new_text, old_version, new_version):
            print(path)
    return 0


def members(build: Path) -> dict:
    """{(archive, repository path): sha256} of what a build made (ci/run.sh run_archives' layout)."""
    found = {}

    def one(pattern: str) -> Path:
        hits = sorted(build.glob(pattern))
        if len(hits) != 1:
            raise SystemExit(f"{build}: {len(hits)} files match {pattern}, one expected")
        return hits[0]

    with tarfile.open(one("client/client-*.tar")) as tar:
        for info in tar.getmembers():
            if info.isfile():
                found[("client tar", "client/" + info.name.split("/", 1)[1])] = hashlib.sha256(tar.extractfile(info).read()).hexdigest()
    with zipfile.ZipFile(one("plugin/theodore-*.zip")) as bundle:
        for name in bundle.namelist():
            found[("plugin zip", "plugins/theodore/" + name)] = hashlib.sha256(bundle.read(name)).hexdigest()
    with tarfile.open(one("plugin/theodore-plugin-*.tar.gz")) as tar:
        for info in tar.getmembers():
            if info.isfile():
                found[("plugin tar", info.name)] = hashlib.sha256(tar.extractfile(info).read()).hexdigest()
    for path in sorted((build / "install").iterdir()):
        found[("install", "install/" + path.name)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def shipped_differences(base: dict, head: dict) -> list:
    return sorted(key for key in set(base) | set(head) if base.get(key) != head.get(key))


def cmd_shipped(base_dir: str, head_dir: str, allowed: list) -> int:
    differ = shipped_differences(members(Path(base_dir)), members(Path(head_dir)))
    outside = [f"{archive}: {path}" for archive, path in differ if path not in set(allowed)]
    for archive, path in differ:
        print(f"differs: {archive}: {path}")
    if outside:
        print("shipped bytes differ outside the files the plan allowed: " + ", ".join(outside))
        return 1
    print(f"shipped bytes differ only in allowed files ({len(differ)} members)")
    return 0


def main(argv: list) -> int:
    if len(argv) >= 2 and argv[0] == "version-only":
        return cmd_version_only(argv[1], argv[2:])
    if len(argv) >= 3 and argv[0] == "shipped":
        return cmd_shipped(argv[1], argv[2], argv[3:])
    print(__doc__)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
