#!/usr/bin/env python3
"""Keep the generated parts of the plugin in step. Run after any change to the client, the launcher or the pins.

1. Stamps launcher.json: the client version and the sha256 of the reproducible client archive, and the stage-1
   Python pins (file, sha256, size per platform) and runtime id from runtime/locks/.
2. Copies the three launcher files from skills/pptx/scripts/ to skills/deck-design/scripts/ byte for byte
   (hosts copy single skill folders, so each skill carries its own launcher).

`--check` changes nothing and exits 1 when something is out of step (tests and CI use it).
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime" / "build"))
import build_client  # noqa: E402

SOURCE = ROOT / "plugins" / "theodore" / "skills" / "pptx" / "scripts"
COPIES = [ROOT / "plugins" / "theodore" / "skills" / "deck-design" / "scripts"]
FILES = ("theodore", "theodore.cmd", "launcher.json")
LOCKS = ROOT / "runtime" / "locks"
RUNTIME_ID_FILES = ("requirements.txt", "package-lock.json", "package.json", "runtime-extra.txt", "versions.json")


def runtime_id() -> str:
    digest = hashlib.sha256()
    for name in RUNTIME_ID_FILES:
        digest.update((LOCKS / name).read_bytes())
    return digest.hexdigest()[:16]


def stamped_launcher_json() -> str:
    """launcher.json stays flat (one "key": value per line) because sh and cmd read it with sed and findstr."""
    path = SOURCE / "launcher.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        archive = build_client.build(Path(tmp))
        sha, size = hashlib.sha256(archive.read_bytes()).hexdigest(), archive.stat().st_size
    version = build_client.client_version()
    dl = data["dl_base"].rstrip("/")
    rid = runtime_id()
    data["stage1_runtime_id"] = rid
    data["stage1_client_version"] = version
    data["stage1_client_url"] = f"{dl}/client/client-{version}.tar"
    data["stage1_client_sha256"] = sha
    data["stage1_client_size"] = size
    pins = json.loads((LOCKS / "versions.json").read_text(encoding="utf-8"))["python"]["assets"]
    for platform, asset in sorted(pins.items()):
        key = "stage1_python_" + platform.replace("-", "_")
        data[f"{key}_url"] = f"{dl}/runtime/{rid}/{asset['file']}"
        data[f"{key}_sha256"] = asset["sha256"]
        data[f"{key}_size"] = asset["size"]
    lines = [f"  {json.dumps(k)}: {json.dumps(v)}" for k, v in data.items()]
    return "{\n" + ",\n".join(lines) + "\n}\n"


def main() -> int:
    check = "--check" in sys.argv[1:]
    problems = []
    wanted = stamped_launcher_json()
    target = SOURCE / "launcher.json"
    if target.read_text(encoding="utf-8") != wanted:
        problems.append("launcher.json pins are stale")
        if not check:
            target.write_text(wanted, encoding="utf-8", newline="\n")
    for folder in COPIES:
        for name in FILES:
            source, copy = SOURCE / name, folder / name
            if not copy.exists() or copy.read_bytes() != source.read_bytes():
                problems.append(f"{copy.relative_to(ROOT)} differs from {source.relative_to(ROOT)}")
                if not check:
                    folder.mkdir(parents=True, exist_ok=True)
                    copy.write_bytes(source.read_bytes())
                    copy.chmod(source.stat().st_mode & 0o777)
    for line in problems:
        print(("stale: " if check else "updated: ") + line)
    return 1 if (check and problems) else 0


if __name__ == "__main__":
    sys.exit(main())
