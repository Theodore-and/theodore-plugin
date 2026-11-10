#!/usr/bin/env python3
"""Keep the generated parts of the plugin in step. Run after any change to the client, the launcher or the pins.

1. Stamps launcher.json: the client version and the sha256 of the reproducible client archive, and the stage-1
   Python pins (file, sha256, size per platform) and runtime id from runtime/locks/.
2. Copies the three launcher files from skills/pptx/scripts/ to skills/deck-design/scripts/ byte for byte
   (hosts copy single skill folders, so each skill carries its own launcher).
3. Stamps the pins block of install/install.sh and install/install.cmd from that launcher.json: the API and
   download bases, the manifest keys, and the stage-1 Python and client (path under the download base and
   sha256). The installer trusts nothing it has not checked against these.

`--check` changes nothing and exits 1 when something is out of step (tests and CI use it).

At deploy, one command points a checkout at an environment and stamps the manifest keys:

    python3 tools/sync_launchers.py --env staging|production --keys <jwks.json>

`--env` writes api_base, dl_base and marketplace_url for that environment (and with them the stage-1 URLs).
`--keys` takes the key set as GET /v1/keys serves it ({"keys": [{"kid", "x", "purpose", ...}]}) and writes every
key whose purpose is "manifest" as manifest_key_<n>_kid / manifest_key_<n>_x; a file with no manifest key is
refused. The repository itself carries the production addresses and a placeholder key: stamp a staging build
in a scratch checkout, then build the archives from it (tools/build_plugin_archives.py).
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


ORIGINS = {     # the contract's origins (x-theodore.origins in the backend's openapi.json)
    "production": {"public": "https://theodoreand.ai", "api": "https://api.theodoreand.ai", "dl": "https://dl.theodoreand.ai"},
    "staging": {"public": "https://staging.theodoreand.ai", "api": "https://staging-api.theodoreand.ai", "dl": "https://staging-dl.theodoreand.ai"},
}


def apply_env(data: dict, env: str) -> dict:
    origins = ORIGINS[env]
    data["api_base"], data["dl_base"] = origins["api"], origins["dl"]
    data["marketplace_url"] = origins["public"] + "/marketplace.json"
    return data


def apply_keys(data: dict, jwks: dict) -> dict:
    keys = [k for k in jwks.get("keys", []) if k.get("purpose") == "manifest" and k.get("kid") and k.get("x")]
    if not keys:
        raise SystemExit("the key file holds no key with purpose \"manifest\"")
    for name in [k for k in data if k.startswith("manifest_key_")]:
        del data[name]
    for index, key in enumerate(keys, 1):
        if len(key["x"]) != 43:
            raise SystemExit(f"key {key['kid']}: x is not a 32-byte Ed25519 public key in base64url")
        data[f"manifest_key_{index}_kid"] = key["kid"]
        data[f"manifest_key_{index}_x"] = key["x"]
    return data


def stamped_launcher_json(env: str | None = None, jwks: dict | None = None) -> str:
    """launcher.json stays flat (one "key": value per line) because sh and cmd read it with sed and findstr."""
    path = SOURCE / "launcher.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if env:
        apply_env(data, env)
    if jwks:
        apply_keys(data, jwks)
    for name in [k for k in data if k.startswith("stage1_")]:      # rebuilt below, in one place at the end
        del data[name]
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


INSTALLERS = {"sh": ROOT / "install" / "install.sh", "cmd": ROOT / "install" / "install.cmd"}
PIN_BEGIN, PIN_END = "--- pins: stamped by tools/sync_launchers.py", "--- end of pins"


def installer_pins(config: dict) -> dict[str, str]:
    """The installer's pins, from a launcher.json: every value the installer checks a download against."""
    dl = config["dl_base"].rstrip("/") + "/"

    def rel(url: str) -> str:
        if not url.startswith(dl):
            raise SystemExit(f"{url} is not under the download base {dl}")
        return url[len(dl):]

    keys = [(config[k], config.get(k[:-4] + "_x", "")) for k in sorted(config) if k.startswith("manifest_key_") and k.endswith("_kid")]
    pins = {"PIN_API_BASE": config["api_base"], "PIN_DL_BASE": config["dl_base"],
            "PIN_MANIFEST_KEYS": " ".join(f"{kid}={x}" for kid, x in keys),
            "PIN_RUNTIME_ID": config["stage1_runtime_id"], "PIN_CLIENT_VERSION": config["stage1_client_version"],
            "PIN_CLIENT": f"{rel(config['stage1_client_url'])} {config['stage1_client_sha256']}"}
    for platform in ("darwin_arm64", "darwin_x64", "linux_arm64", "linux_x64", "win32_x64"):
        if f"stage1_python_{platform}_url" in config:
            pins[f"PIN_PYTHON_{platform}"] = f"{rel(config[f'stage1_python_{platform}_url'])} {config[f'stage1_python_{platform}_sha256']}"
    return pins


def stamped_installer(kind: str, config: dict, text: str | None = None) -> str:
    """install.sh or install.cmd with its pins block rewritten from `config` (a launcher.json)."""
    text = INSTALLERS[kind].read_bytes().decode("utf-8") if text is None else text     # bytes: keep install.cmd's CRLF
    eol = "\r\n" if kind == "cmd" else "\n"
    lines = text.split(eol)
    start = next(i for i, line in enumerate(lines) if PIN_BEGIN in line)
    end = next(i for i, line in enumerate(lines) if PIN_END in line)
    pins = installer_pins(config)
    if kind == "cmd":
        body = [f'set "{k}={v}"' for k, v in pins.items() if not k.startswith("PIN_PYTHON_") or k == "PIN_PYTHON_win32_x64"]
    else:
        body = [f'{k}="{v}"' for k, v in pins.items() if k != "PIN_PYTHON_win32_x64"]
    return eol.join(lines[:start + 1] + body + lines[end:])


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Stamp launcher.json and refresh the generated launcher copies.")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--env", choices=sorted(ORIGINS))
    parser.add_argument("--keys", help="key set as GET /v1/keys serves it")
    args = parser.parse_args()
    check = args.check
    problems = []
    jwks = json.loads(Path(args.keys).read_text(encoding="utf-8")) if args.keys else None
    wanted = stamped_launcher_json(args.env, jwks)
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
    config = json.loads(wanted)
    for kind, path in INSTALLERS.items():
        current = path.read_bytes().decode("utf-8")
        stamped = stamped_installer(kind, config, current)
        if stamped != current:
            problems.append(f"{path.relative_to(ROOT)} pins are stale")
            if not check:
                path.write_bytes(stamped.encode("utf-8"))
    for line in problems:
        print(("stale: " if check else "updated: ") + line)
    return 1 if (check and problems) else 0


if __name__ == "__main__":
    sys.exit(main())
