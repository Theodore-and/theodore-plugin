# Third-party notices

## In this repository

| Component | Where | Licence |
| --- | --- | --- |
| truststore 0.10.5, by Seth Michael Larson | `client/truststore/` (unmodified copy of the PyPI wheel's package; shipped inside the client archive) | MIT, text in `client/truststore/LICENSE` |
| deck-design loader | `plugins/theodore/skills/deck-design/` | The guidance it loads is a derivative of Impeccable by Paul Bakaus (https://github.com/pbakaus/impeccable), Apache-2.0; `LICENSE` and `NOTICE` are in that folder |
| Ed25519 verification | `client/theodore_client/ed25519.py` | Written for this project after the algorithm description in RFC 8032 (section 5.1); no third-party code |

Fetched on request and never redistributed by Theodore: LibreOffice (MPL-2.0), downloaded unmodified from The
Document Foundation with a pinned SHA-256 when a computer has no LibreOffice. Poppler (GPL) is never bundled or
fetched.

## Runtime bundle

The runtime bundle is the Python, the Node and the open-source packages that Theodore downloads into
`<home>/runtime/<runtime-id>/` so the engine can run. Every component is shipped unmodified, as its project
published it. This section was written from the darwin-arm64 build of runtime `f0c496e365236046` and updated for
`43be74ecf52f9321`, which adds skia-canvas's native binary and nothing else (recipe in
`runtime/build/`, pins in `runtime/locks/`). The other four platforms carry the same packages at the same
versions with that platform's binaries.

The licence texts travel with the bundle, in `<home>/runtime/<runtime-id>/licenses/`:

| Path | Contents |
| --- | --- |
| `python/lib/python3.12/LICENSE.txt` (Windows: `python/LICENSE.txt`) | CPython licence, inside the upstream Python archive |
| `licenses/python-build-standalone/` | python-build-standalone's own licence and the licence of each library it links into CPython |
| `licenses/python-packages/<name>-<version>/` | Licence files of each PyPI package, copied from its wheel |
| `licenses/node/LICENSE` | Node.js licence, which includes the licences of V8, ICU, OpenSSL, libuv and its other bundled components |
| `licenses/npm-packages.txt` | Name, version and licence of every npm package; each package keeps its own licence file in `node_modules/` |
| `licenses/skia/LICENSE` | Skia's licence, for the Skia code inside skia-canvas's native binary |
| `licenses/vc-redist/license.rtf` (Windows only) | Microsoft's licence terms for the Visual C++ Redistributable, from the installer the DLLs were taken from |

### Microsoft Visual C++ runtime (Windows bundle only, recipe version 3)

`node_modules/skia-canvas/lib/` holds `msvcp140.dll`, `vcruntime140.dll` and `vcruntime140_1.dll`, version
14.44.35211.0, next to `skia.node`, which imports the first two (the third is imported by `msvcp140.dll`); the
import lists were read from the binaries, not assumed. They are taken unmodified from Microsoft's
`VC_redist.x64.exe` (Visual C++ 2015-2022 Redistributable, x64, 14.44.35211.0, 25,635,768 bytes, sha256
`cc0ff0eb1dc3f5188ae6300faef32bf5beeba4bdd6e8e445a9184072096b713b`, from
`https://download.visualstudio.microsoft.com/download/pr/990c720a-46ce-4dc7-87b9-c2eb615d34b9/CC0FF0EB1DC3F5188AE6300FAEF32BF5BEEBA4BDD6E8E445A9184072096B713B/VC_redist.x64.exe`),
each checked against the sha256 in `runtime/locks/versions.json`. These three files are on Microsoft's list of
redistributable Visual C++ files, which may be deployed app-local beside the program that uses them; nothing is
installed system-wide. Microsoft's licence terms are in `licenses/vc-redist/license.rtf` (sha256 pinned too).

### Licences that are not plain permissive

Nothing in the bundle is under the AGPL, nothing is under the GPL without a linking exception, and no package
has an unknown or missing licence. These are the components whose terms go beyond attribution:

| Component | Where | Licence | Note |
| --- | --- | --- | --- |
| libvips and the libraries built into it (glib, pango, librsvg, libheif, fribidi, libexif, proxy-libintl; cairo is MPL-1.1) | npm `@img/sharp-libvips-<platform>` 1.3.4; on Windows inside `@img/sharp-win32-x64` | LGPL-3.0-or-later | A separate shared library that sharp loads at run time; it can be replaced. The package README lists every library and is kept in the bundle |
| GNU libiconv | Inside the `lxml` wheel | LGPL-2.1 | Linked into lxml's extension modules by the lxml project (stated in lxml's `LICENSES.txt`) |
| libquadmath | `numpy` wheel (macOS and Linux x64), separate shared library | LGPL-2.1-or-later | Shipped by the NumPy project beside OpenBLAS |
| GCC runtime (libgfortran, libgcc) | `numpy` wheel on every platform; separate shared libraries on macOS and Linux | GPL-3.0-or-later WITH GCC-exception-3.1 | The GCC Runtime Library Exception permits distribution with independent programs |
| Berkeley DB 6.0.19 | Linux builds of CPython only: `python/lib/python3.12/lib-dynload/_dbm.*.so` | Sleepycat | Redistribution must say where the source of Berkeley DB and of software that uses it can be obtained. The engine and the client do not use `dbm`. python-build-standalone names the source archive in its `pythonbuild/downloads.json` and builds this one module as a separate file so that it can be removed |
| certifi | PyPI | MPL-2.0 | Unmodified; source at https://github.com/certifi/python-certifi |
| python-build-standalone build scripts | Produced the Python archive | MPL-2.0 | Unmodified archive; source at https://github.com/astral-sh/python-build-standalone |
| jszip | npm 3.10.1 | MIT OR GPL-3.0-or-later | Used under the MIT licence |

### Python

| Component | Version | Licence |
| --- | --- | --- |
| CPython | 3.12.16 | PSF-2.0 (Python-2.0, with the CNRI and other historical licences in the text) |
| python-build-standalone | release 20261026, `install_only_stripped` archive | MPL-2.0 |
| pip (inside the upstream archive) | 26.2.2 | MIT; its vendored packages keep their licences in `pip-26.2.2.dist-info/licenses/` |

Libraries python-build-standalone links into CPython, from its `downloads.json` and `extension-modules.yml` at
tag 20261026. Which ones are present depends on the platform: on macOS, zlib, libedit and ncurses come from
the operating system (checked with `otool -L`), and the X11 libraries and Berkeley DB are Linux only.

| Library | Version | Licence | Text in `licenses/python-build-standalone/` |
| --- | --- | --- | --- |
| OpenSSL | 3.5.9 | Apache-2.0 | `LICENSE.openssl-3.txt` |
| SQLite | 3.53.1 | Public domain | `LICENSE.sqlite.txt` |
| xz (liblzma) | 5.8.4 | 0BSD | `LICENSE.liblzma.txt` |
| bzip2 | 1.0.8 | bzip2-1.0.6 | `LICENSE.bzip2.txt` |
| libffi | 3.8.0 | MIT | `LICENSE.libffi.txt` |
| expat | 2.9.0 | MIT | `LICENSE.expat.txt` |
| mpdecimal | 4.0.0 | BSD-2-Clause | `LICENSE.mpdecimal.txt` |
| zlib / zlib-ng | 1.3.2 / 2.2.4 | Zlib | `LICENSE.zlib.txt`, `LICENSE.zlib-ng.txt` |
| libedit (not GNU readline) | 20240808-3.1 | BSD-3-Clause | `LICENSE.libedit.txt` |
| ncurses | 6.5 | X11 | `LICENSE.ncurses.txt` |
| libuuid | 1.0.3 | BSD-3-Clause | `LICENSE.libuuid.txt` |
| Tcl/Tk (present in the stripped build) | 9.0.4; Windows 8.6.15 | TCL | `LICENSE.tcl.txt` |
| libX11, libXau, libxcb (Linux) | 1.8.13, 1.0.12, 1.17.0 | MIT, X11 | `LICENSE.libX11.txt`, `LICENSE.libXau.txt`, `LICENSE.libxcb.txt` |
| Berkeley DB (Linux) | 6.0.19 | Sleepycat | `LICENSE.bdb.txt` |

python-build-standalone builds against libedit instead of GNU readline and disables the `_gdbm` module, so
neither GPL library is present (its `docs/running.rst`, section Licensing). The Windows archive also contains
Microsoft's Visual C++ runtime (`vcruntime140.dll`, `vcruntime140_1.dll`), redistributed under Microsoft's
terms. The directory also holds `LICENSE.zstd.txt` and `LICENSE.tix.txt`, which upstream publishes for
components that CPython 3.12 does not use.

### PyPI packages

Installed from `runtime/locks/requirements.txt` and `runtime/locks/runtime-extra.txt`, wheels only.

| Package | Version | Licence | Bundled in the wheel |
| --- | --- | --- | --- |
| certifi | 2026.10.23 | MPL-2.0 | Mozilla's CA certificate list |
| charset-normalizer | 3.5.2 | MIT | |
| defusedxml | 0.7.1 | PSF-2.0 | |
| fonttools | 4.68.0 | MIT | |
| idna | 3.20 | BSD-3-Clause | |
| lxml | 6.1.3 | BSD-3-Clause | libxml2, libxslt and libexslt (MIT), zlib (Zlib), GNU libiconv (LGPL-2.1) |
| numpy | 2.4.6 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | OpenBLAS (BSD-3-Clause), LAPACK (BSD-3-Clause-Open-MPI), libgfortran and libgcc (GPL-3.0-or-later WITH GCC-exception-3.1), libquadmath (LGPL-2.1-or-later); on Windows OpenBLAS and Microsoft's `msvcp140.dll` |
| pillow | 13.0.0 | MIT-CMU | libjpeg-turbo, libpng, libtiff, libwebp, libavif with aom, dav1d and libyuv, FreeType, HarfBuzz, Raqm, Little CMS, OpenJPEG, Brotli, zlib-ng, xz, zstd, libxcb, libXau and others; all permissive, texts in the wheel's `LICENSE` |
| pypdfium2 | 5.14.0 | BSD-3-Clause OR Apache-2.0 | PDFium (BSD-3-Clause, Apache-2.0) and its third-party libraries (Abseil, AGG, FreeType, ICU, Little CMS, libjpeg-turbo, OpenJPEG, libpng, libtiff, zlib and others); texts under `BUILD_LICENSES/` |
| python-pptx | 1.0.2 | MIT | |
| requests | 2.34.2 | Apache-2.0 | `NOTICE` included |
| truststore | 0.10.5 | MIT | |
| typing-extensions | 4.16.0 | PSF-2.0 | |
| urllib3 | 2.8.0 | MIT | |
| xlsxwriter | 3.3.1 | BSD-2-Clause | |

pypdfium2 is the PDF rasteriser. Poppler (GPL) is not part of the bundle.

### Node.js

| Component | Version | Licence |
| --- | --- | --- |
| Node.js (the `node` binary only; npm is not shipped) | 24.22.2 | MIT, with the licences of its bundled components (V8, ICU, OpenSSL, libuv, zlib, c-ares, nghttp2 and others) in `licenses/node/LICENSE` |

### npm packages

Installed with `npm ci --omit=dev --ignore-scripts` from `runtime/locks/package-lock.json`: 156 package
versions (150 names) on darwin-arm64.

| Licence | Packages |
| --- | --- |
| MIT | 141 |
| Apache-2.0 | 8: `sharp`, `@img/sharp-<platform>`, `mathjax-full`, `mhchemparser`, `mj-context-menu`, `speech-rule-engine`, `@swc/helpers`, `detect-libc` |
| ISC | 3: `semver`, `inherits`, `https` |
| 0BSD | 1: `tslib` |
| LGPL-3.0-or-later | 1: `@img/sharp-libvips-<platform>` |
| MIT OR GPL-3.0-or-later | 1: `jszip` (used under MIT) |
| MIT AND Zlib | 1: `pako` |

| Notable package | Version | Licence | Note |
| --- | --- | --- | --- |
| pptxgenjs | 4.0.1 (and 3.12.0 inside pptx-automizer) | MIT | |
| pptx-automizer | 0.9.4 | MIT | |
| sharp | 0.35.5 | Apache-2.0 | Loads the prebuilt `@img/sharp-<platform>` binary |
| `@img/sharp-libvips-<platform>` | 1.3.4 | LGPL-3.0-or-later | libvips 8.18.7 as a separate shared library; see the table at the top. On Windows it is inside `@img/sharp-win32-x64` (Apache-2.0 AND LGPL-3.0-or-later) |
| mathjax-full | 3.2.2 | Apache-2.0 | |
| fontkit | 2.0.4 | MIT | |
| linebreak | 1.1.0 | MIT | |
| prismjs | 1.30.0 | MIT | |
| acorn, acorn-walk | 8.16.0, 8.3.5 | MIT | |
| skia-canvas | 3.0.8 | MIT | With its prebuilt native binary `lib/skia.node`, taken unmodified from the project's GitHub release `v3.0.8` and checked against the sha256 the package itself lists (see below) |

`skia-canvas`'s `lib/skia.node` statically links Google's Skia (BSD-3-Clause; text in `licenses/skia/LICENSE`, taken
from the Skia repository at commit `563b559a`), Rust crates, and libraries Skia builds in: HarfBuzz, expat and
libjpeg-turbo on every platform, and on Linux also FreeType (used under the FreeType Licence, which asks for credit:
"Portions of this software are copyright © The FreeType Project (www.freetype.org). All rights reserved.") and
Fontconfig. The binary's own list of embedded licences is not yet reproduced in the bundle beyond Skia's.

The full list for a given bundle is its `licenses/npm-packages.txt`.
