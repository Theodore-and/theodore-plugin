#!/bin/sh
# Builds the native front (front/) for the systems Theodore runs on, into one folder that the plugin archives take
# (python3 tools/build_plugin_archives.py --front <out> ...):
#
#   theodore-darwin        macOS, one universal file (arm64 and x64); signed here ad hoc, with the Developer ID and
#                          notarized by .github/workflows/front.yml
#   theodore-linux-x64     static (musl), linked by the toolchain's own rust-lld: runs on any Linux x64
#   theodore-linux-arm64   the same for arm64
#   theodore.exe           Windows x64. On Windows: the MSVC build with its C runtime inside (the release form, signed by
#                          the workflow). Elsewhere: windows-gnu linked by zig (cargo-zigbuild), for tests on the rig
#
# Usage: sh ci/front-build.sh <out> [darwin] [linux] [windows]       (no system named: every one this host can build)
# Needs rustup (the toolchain front/rust-toolchain.toml names, and its targets: added here when missing); on macOS
# the Xcode command line tools (lipo, codesign); for Windows from macOS or Linux: zig and cargo-zigbuild
# (python3 -m pip install ziglang cargo-zigbuild). CARGO_BUILD_JOBS (default 2) bounds the cores it takes.
# It prints each file's size and sha256 and writes them to <out>/SHA256SUMS; a file over 3 MB fails it (PLAN lane C).
set -eu

[ $# -ge 1 ] || { echo "usage: sh ci/front-build.sh <out> [darwin] [linux] [windows]" >&2; exit 64; }
OUT=$1; shift
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
mkdir -p "$OUT"
OUT=$(CDPATH= cd -- "$OUT" && pwd -P)
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-2}"
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-$ROOT/.cache/front-target}"
cd "$ROOT/front"

host=$(uname -s 2>/dev/null || echo unknown)
case "$host" in MINGW*|MSYS*|CYGWIN*) host=Windows ;; esac
want=${*:-}
if [ -z "$want" ]; then
  case "$host" in
    Darwin) want="darwin linux windows" ;;
    Linux) want="linux windows" ;;
    Windows) want="windows" ;;
  esac
fi

target() { rustup target list --installed 2>/dev/null | grep -qx "$1" || rustup target add "$1"; }
release() { echo "$CARGO_TARGET_DIR/$1/release/$2"; }

for system in $want; do
  case "$system" in
    darwin)
      [ "$host" = Darwin ] || { echo "front-build: the macOS front is built on macOS" >&2; exit 69; }
      for t in aarch64-apple-darwin x86_64-apple-darwin; do target "$t"; cargo build --release --locked --target "$t"; done
      lipo -create -output "$OUT/theodore-darwin" "$(release aarch64-apple-darwin theodore)" "$(release x86_64-apple-darwin theodore)"
      codesign --force --sign - "$OUT/theodore-darwin"
      ;;
    linux)
      for t in x86_64-unknown-linux-musl aarch64-unknown-linux-musl; do
        target "$t"
        upper=$(echo "$t" | tr 'a-z-' 'A-Z_')
        env "CARGO_TARGET_${upper}_LINKER=rust-lld" cargo build --release --locked --target "$t"
      done
      cp "$(release x86_64-unknown-linux-musl theodore)" "$OUT/theodore-linux-x64"
      cp "$(release aarch64-unknown-linux-musl theodore)" "$OUT/theodore-linux-arm64"
      ;;
    windows)
      if [ "$host" = Windows ]; then
        target x86_64-pc-windows-msvc
        RUSTFLAGS="${RUSTFLAGS:-} -C target-feature=+crt-static" cargo build --release --locked --target x86_64-pc-windows-msvc
        cp "$(release x86_64-pc-windows-msvc theodore.exe)" "$OUT/theodore.exe"
      else
        target x86_64-pc-windows-gnu
        cargo zigbuild --release --locked --target x86_64-pc-windows-gnu
        cp "$(release x86_64-pc-windows-gnu theodore.exe)" "$OUT/theodore.exe"
      fi
      ;;
    *) echo "front-build: no system $system (darwin, linux, windows)" >&2; exit 64 ;;
  esac
done

cd "$OUT"
: > SHA256SUMS
for f in theodore-darwin theodore-linux-x64 theodore-linux-arm64 theodore.exe; do
  [ -f "$f" ] || continue
  size=$(wc -c < "$f" | tr -d ' ')
  sum=$( (shasum -a 256 "$f" 2>/dev/null || sha256sum "$f") | cut -d' ' -f1)
  echo "$sum  $f" >> SHA256SUMS
  echo "front-build: $f $size bytes $sum"
  [ "$size" -le 3145728 ] || { echo "front-build: $f is over 3 MB" >&2; exit 1; }
done
