# Runtime bundle recipe

The runtime bundle is what lets the Theodore engine run on a machine with nothing preinstalled: a Python, a
Node, and the open-source packages the engine imports. The launcher and the client unpack it into
`<home>/runtime/<runtime-id>/` without administrator rights. Nothing proprietary is built here; the inputs are
the public lock files in `runtime/locks/`.

## Inputs (`runtime/locks/`)

| File | What it pins |
| --- | --- |
| `requirements.txt` | The engine's Python packages, exact versions with sha256 of every released file |
| `runtime-extra.txt` | What the bundle adds: `pypdfium2`, `truststore` (same format) |
| `package.json`, `package-lock.json` | The engine's npm packages |
| `versions.json` | CPython version and python-build-standalone release, Node version, sha256 and size of both downloads for the five platforms, sha256 of the licence texts taken from python-build-standalone, the oldest system the wheels must support (`wheel_floor`), and `recipe_version` |

## Outputs (`--out`)

| Part | File | Contents |
| --- | --- | --- |
| python | `cpython-<version>+<release>-<triple>-install_only_stripped.tar.gz` | The upstream python-build-standalone asset, byte for byte: `python/` |
| wheels | `runtime-<id>-<platform>-<arch>-wheels.tar.xz` | What pip added to `python/lib/python3.12/site-packages/` (Windows: `python/Lib/site-packages/`), `licenses/python-packages/<dist>/`, `licenses/python-build-standalone/` |
| node | `runtime-<id>-<platform>-<arch>-node.tar.xz` | `node/bin/node` (Windows: `node/node.exe`), `licenses/node/LICENSE` |
| node_modules | `runtime-<id>-<platform>-<arch>-node_modules.tar.xz` | `node_modules/`, `licenses/npm-packages.txt` |
| | `runtime-<id>-<platform>-<arch>.manifest.json` | Schema `theodore-runtime-bundle-v1`: one entry per part with file, sha256, size, unpacked size and file count |

Unpacking every part into one directory gives `python/`, `node/`, `node_modules/` and `licenses/`. The python
part is the launcher's first stage and is unpacked with plain `tar` before any Python exists, which is why it
stays a `.tar.gz`. The other parts are unpacked with the Python it delivers.

If `runtime/build/build_client.py` exists, the recipe runs it and adds the archive it prints to the manifest as
part `client`. The recipe does not build the client itself.

## Running it

```sh
python3 runtime/build/build_bundle.py --platform darwin-arm64 --out out [--work work] [--cache cache]
python3 runtime/build/smoke_test.py   --bundle out --platform darwin-arm64
```

Both scripts need Python 3.11 or newer and nothing else. Platforms: `darwin-arm64`, `darwin-x64`, `win32-x64`,
`linux-x64`, `linux-arm64`. The build is native only: it installs the wheels with the downloaded Python and
runs `npm ci` with the downloaded Node, so `--platform` must match the machine. `--cache` keeps the downloads
between runs. `--work` is kept when you pass it and is a removed temporary directory when you do not.

The build writes nothing outside `--out`, `--work` and `--cache`. It ignores the developer's pip and npm
configuration.

What the recipe does, in order:

1. Reads the locks and computes the runtime id.
2. Downloads the Python and Node archives and the licence texts, and checks each sha256 against
   `versions.json`. A mismatch stops the build.
3. Unpacks Python and installs the two requirement files into its own `site-packages` with
   `--require-hashes --only-binary :all: --no-deps --no-compile`, then runs `pip check`. Every dependency is
   already in the two files; if `pip check` reports one missing, add the pin to the lock.
4. Takes what pip added to `site-packages` as the wheels part. `__pycache__` is not shipped. Files pip put
   outside `site-packages` (console scripts in `python/bin`, one man page) are not shipped, because the upstream
   tarball must stay untouched and a console script embeds the build directory.
5. Takes the `node` binary and `LICENSE` from the Node archive, and runs
   `npm ci --omit=dev --ignore-scripts` with the npm inside that archive on a copy of the two npm files.
6. Prunes `node_modules` (below), lists every package with its licence, and writes the archives and the
   manifest.

The smoke test verifies each part against the manifest, unpacks into a fresh directory whose path contains a
space, imports every wheel from the bundled Python and from a venv created over it
(`python -m venv --system-site-packages --without-pip`), requires every npm dependency and encodes a PNG with
sharp. Then it moves the runtime directory to a second path and repeats. `--engine <payload dir>` adds a blank
deck build from an unpacked engine payload; the engine is private, so that option is for local runs only.

## Runtime id

The first 16 hexadecimal characters of the sha256 over the bytes of, in this order: `requirements.txt`,
`package-lock.json`, `package.json`, `runtime-extra.txt`, `versions.json`. It is the same on every platform.
Any change to a lock, a pin or `recipe_version` gives a new id.

## Changing a pin

- **Python or Node:** edit `versions.json`. Take the python-build-standalone sha256 values from the release's
  `SHA256SUMS` asset and the Node values from `SHASUMS256.txt` on nodejs.org, for all five platforms. A new
  python-build-standalone release also needs the sha256 of each `LICENSE*` file at that tag.
- **A Python or npm package of the engine:** the three engine files are copies of the engine's own locks. Replace
  them together; do not edit them here.
- **`runtime-extra.txt`:** take the version and the sha256 of every released file from
  `https://pypi.org/pypi/<name>/<version>/json`.
- **The recipe itself:** when a change to `build_bundle.py` changes what is inside the archives, raise
  `recipe_version` in `versions.json`.

## Reproducible archives

Entries are sorted, carry one fixed timestamp (2024-01-01, the date the upstream Python tarball uses; not 1970,
because ZIP writers refuse dates before 1980), numeric owner 0 and no user names. Compression is xz preset 9
extreme through Python's `lzma`. The archives are written by the bundle's own Python, so the bytes do not depend
on the Python that runs the recipe. Two builds of the same locks on the same platform give the same sha256;
only `built_at` in the manifest differs.

pip is told which system to select wheels for (`wheel_floor`: macOS 13.0, glibc 2.28) instead of using the
build machine's own version. Without that, a build on a newer macOS would take numpy's macOS 14 wheel and the
bundle would fail on macOS 13. The floors follow what Node 24 supports (macOS 13.5, glibc 2.28).

## What is pruned from `node_modules`, and why

Only files no program loads, to cut the download:

- `@img/sharp-*` packages built for another platform, if npm installed any.
- `*.map` (source maps).
- `*.md` whose name and text do not mention a licence. A README that states the licence is kept: for some
  packages, sharp's libvips package among them, it is the only licence notice.
- `test`, `tests`, `__tests__`, `docs`, `example`, `examples` and `.github` directories directly inside a
  package, unless the package's own `package.json` points into one or it holds a licence file.

Never removed: any file whose name contains licence, notice, copying, copyright, authors or patents. TypeScript
sources and declarations are left alone.

## skia-canvas

`skia-canvas` is an optional dependency whose native binary is downloaded by an install script. The recipe runs
no install scripts, so the bundle has the package without its binary and `require('skia-canvas')` fails with
`MODULE_NOT_FOUND`. The engine treats that as the package being absent. The smoke test reports it as `SKIP`.
