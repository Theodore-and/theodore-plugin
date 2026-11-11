#!/bin/sh
# Theodore installer for macOS and Linux. No admin rights, no package manager; everything goes into
# ~/.theodore (or THEODORE_HOME). Written to be run by an AI assistant or by a person:
#
#   sh install.sh --code ABCD-1234        sign this computer in with a one-time code and set everything up
#   sh install.sh                         the same, ending with a sign-in link instead of a code (no browser opens)
#   sh install.sh --accept-settings ...   also turn on plugin auto-update and allow the launcher in Claude Code
#   sh install.sh --uninstall             remove Theodore from this computer
#
# What it does, in order: prints its own SHA-256; redeems the one-time code (it lasts ten minutes); downloads
# Theodore's Python and client and checks each against the SHA-256 pinned below; with them, fetches the signed
# plugin release document, checks its signature against the keys pinned below, then fetches the plugin archive it
# names and checks its SHA-256 and size; downloads the rest of the runtime and the engine (about 100 to 130 MB in all
# before the first deck); registers the plugin in Claude Code and Codex when they are installed; starts the asset
# packs (about 95 MB) and, on a Mac without LibreOffice, LibreOffice (about 300 MB) in the background; ends with
# `theodore doctor`. Running it again is safe.
#
# Trust: this file is the root. Everything it runs was checked first: Python and the client against the pinned
# SHA-256 values, the plugin against a document signed with a pinned key (verified by that Python, because a stock
# Mac cannot verify Ed25519 with its own tools), the runtime and engine by the client against signed manifests.
# Downloads never follow a redirect. Lines starting with "THEODORE" are statuses for the assistant: follow NEXT.

set -u

# --- pins: stamped by tools/sync_launchers.py from launcher.json; do not edit by hand ---------------------------
PIN_API_BASE="https://api.theodoreand.ai"
PIN_DL_BASE="https://dl.theodoreand.ai"
PIN_MANIFEST_KEYS="man-production-20261111-7792324a=LxgBIQ0n5McEVIMmiY-96H-YQErYK-OHpLBLLuscsbk"
PIN_RUNTIME_ID="b77f7d4ca2d43955"
PIN_CLIENT_VERSION="0.3.2"
PIN_CLIENT="client/client-0.3.2.tar ccadd734e59fbd91f3c84ac1951a483a787022eff30e8996ec7895ae20702b82"
PIN_PYTHON_darwin_arm64="runtime/b77f7d4ca2d43955/cpython-3.12.16+20261026-aarch64-apple-darwin-install_only_stripped.tar.gz 353d5f1b4fd2715e7053f12f068435d5bbad16b91e057006b5d01e0925709a7e"
PIN_PYTHON_darwin_x64="runtime/b77f7d4ca2d43955/cpython-3.12.16+20261026-x86_64-apple-darwin-install_only_stripped.tar.gz 37c38fda76fec1fbf310adcbafb7f0d96af13b5511bf8ea954c3aa8eec185734"
PIN_PYTHON_linux_arm64="runtime/b77f7d4ca2d43955/cpython-3.12.16+20261026-aarch64-unknown-linux-gnu-install_only_stripped.tar.gz bb4b6392670c5d3f93876996fcaa19858a4a413a0c7720144c18b0d6f1f0db2d"
PIN_PYTHON_linux_x64="runtime/b77f7d4ca2d43955/cpython-3.12.16+20261026-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz ce4f6d0a6cfdb3bed6e44c2f9affd85943c76a42e25ad3a259002f31d6fb8a20"
# --- end of pins --------------------------------------------------------------------------------------------------

API_BASE=$PIN_API_BASE
DL_BASE=${THEODORE_DL_BASE:-$PIN_DL_BASE}     # another mirror is safe: every file is checked against the pins
HOME_DIR=${THEODORE_HOME:-${HOME:-}/.theodore}
case "$HOME_DIR" in /*) ;; *) HOME_DIR="$(pwd -P)/$HOME_DIR" ;; esac
CODE=
UNINSTALL=
ACCEPT=

say() { printf 'theodore-install: %s\n' "$*"; }
status() { printf 'THEODORE %s: %s\nNEXT: %s\nTELL THE USER: %s\n' "$1" "$3" "$4" "$5"; exit "$2"; }
sha256_of() {
  if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | cut -d' ' -f1
  elif command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else openssl dgst -sha256 "$1" | sed 's/^.*= *//'; fi
}

while [ $# -gt 0 ]; do
  case "$1" in
    --code) CODE=${2:-}; shift ;;
    --code=*) CODE=${1#--code=} ;;
    --uninstall) UNINSTALL=1 ;;
    --accept-settings) ACCEPT=1 ;;
    *) status USAGE 64 "unknown installer option $1." "run the installer with --code ABCD-1234, or with no option for a browser sign-in." \
         "I called the Theodore installer with a wrong option; I am correcting it." ;;
  esac
  shift
done

[ -f "$0" ] && say "installer sha256 $(sha256_of "$0") (compare with https://theodoreand.ai/install)"

# A Theodore folder belongs to one service, the one it was set up for (state/environment): this installer does not
# reuse a folder of another. Uninstalling is the way out, so it is allowed.
home_api() {
  if [ -f "$HOME_DIR/state/environment" ]; then
    head -n 1 "$HOME_DIR/state/environment" 2>/dev/null | tr -d '\r'
  elif [ -f "$HOME_DIR/bin/launcher.json" ]; then
    sed -n 's/^[[:space:]]*"api_base"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$HOME_DIR/bin/launcher.json" 2>/dev/null | head -n 1
  fi
}
foreign() { HOME_API=$(home_api); HOME_API=${HOME_API%/}; [ -n "$HOME_API" ] && [ "$HOME_API" != "${API_BASE%/}" ]; }
refuse_foreign() {
  status USAGE 64 "the Theodore folder $HOME_DIR belongs to the Theodore service at $HOME_API, but this installer is for ${API_BASE%/}; a folder is never shared between two services." \
    "do not retry. Tell the user: to install this build, set THEODORE_HOME to a separate folder for it and run the installer again, or first remove the other installation with: \"$HOME_DIR/bin/theodore\" uninstall" \
    "Theodore found an installation for another Theodore service ($HOME_API) in $HOME_DIR. To install this one, give it its own folder (THEODORE_HOME) or uninstall the other first."
}
[ -z "$UNINSTALL" ] && foreign && refuse_foreign

# Specimen folders: every one the client made is a line "<created epoch> <path>" in state/specimens.txt. After a first
# line "#v2" the paths are encoded (%25, %0D, %0A for "%", CR and LF) and decoded here as the client does; a record
# Without that line (the first format) is read as it is. They go from that record and from the current roots, whatever TMPDIR
# was when they were made: the expired ones (an hour) at every run that reaches this launcher's own code, all of them
# at uninstall. Only folders named theodore-specimens-*, real (not a link) and this user's own are removed; the record
# itself is rewritten only by the client. Every variable below is set before use (the launcher runs under set -u).
left= specimens_left=
specimen_cache() {
  if [ "$(uname -s 2>/dev/null)" = Darwin ]; then echo "${HOME:-/nonexistent}/Library/Caches/Theodore/specimens"
  else echo "${XDG_CACHE_HOME:-${HOME:-/nonexistent}/.cache}/theodore/specimens"; fi
}
specimen_decode() { # an encoded path, decoded, then "x" (command substitution would drop a trailing line feed)
  printf '%s' "$1" | awk 'BEGIN { h = "0123456789ABCDEF"; ORS = "" }
    { s = $0; o = ""
      while ((i = index(s, "%")) > 0) {
        o = o substr(s, 1, i - 1); a = index(h, toupper(substr(s, i + 1, 1))); b = index(h, toupper(substr(s, i + 2, 1)))
        if (a && b) { o = o sprintf("%c", (a - 1) * 16 + b - 1); s = substr(s, i + 3) } else { o = o "%"; s = substr(s, i + 1) }
      }
      print o s }'
  printf x
}
remove_specimen() {
  case "${1##*/}" in theodore-specimens-?*) ;; *) return 0 ;; esac
  [ -d "$1" ] && [ ! -L "$1" ] && [ -O "$1" ] || return 0
  rm -rf "$1" 2>/dev/null
  if [ -e "$1" ]; then left="$left; the specimen folder $1"; specimens_left=1; fi
  return 0
}
remove_recorded() { # all | expired: the folders the record names
  reg="$HOME_DIR/state/specimens.txt"
  [ -f "$reg" ] || return 0
  now=$(date +%s 2>/dev/null) || now=
  encoded= first=1
  while IFS= read -r line || [ -n "$line" ]; do
    if [ -n "$first" ]; then
      first=
      if [ "$line" = "#v2" ]; then encoded=1; continue; fi
    fi
    t=${line%% *} path=${line#* }
    case "$t" in ''|*[!0-9]*) continue ;; esac
    if [ -n "$encoded" ]; then path=$(specimen_decode "$path"); path=${path%x}; fi
    if [ "$1" = expired ]; then
      [ -n "$now" ] || return 0
      [ $((now - t)) -gt 3600 ] || [ $((t - now)) -gt 60 ] || continue
    fi
    remove_specimen "$path"
  done < "$reg"
  return 0
}
remove_specimens() { # all of them (uninstall)
  remove_recorded all
  sc=$(specimen_cache)
  for d in "${TMPDIR:-/tmp}"/theodore-specimens-* "$sc"/theodore-specimens-*; do remove_specimen "${d%/}"; done
  rmdir "$sc" 2>/dev/null && rmdir "${sc%/*}" 2>/dev/null
  return 0
}
if [ -n "$UNINSTALL" ]; then
  if [ -f "$HOME_DIR/bin/theodore" ]; then
    exec sh "$HOME_DIR/bin/theodore" uninstall
  fi
  # No launcher left: the specimen folders the client recorded still go, and the record with them.
  left=
  remove_specimens
  [ -n "$left" ] || rm -f "$HOME_DIR/state/specimens.txt" 2>/dev/null
  rec="$HOME_DIR/state/host-settings.json"
  if grep -qE '"(rules|marketplace|created_marketplace|set_auto_update|created_keys)":' "$rec" 2>/dev/null; then
    left="$left; Theodore's changes to the Claude Code settings.json, which need Theodore's runtime to take back exactly: what it added and the values before are kept in $rec. To take them back, install Theodore again and run its uninstall, or edit that settings file by that record"
  fi
  rmdir "$HOME_DIR/state" "$HOME_DIR" 2>/dev/null
  [ -z "$left" ] || status RETRY 75 "Theodore is not installed in $HOME_DIR, but these could not be removed: ${left#; }." \
    "tell the user what is left; each item is a folder that can be deleted by hand." "A few of Theodore's temporary files could not be removed: ${left#; }."
  status OK 0 "Theodore is not installed in $HOME_DIR." "nothing to do." "Theodore was not installed on this computer."
fi

if [ -n "$CODE" ]; then
  CODE=$(printf '%s' "$CODE" | tr '[:lower:]' '[:upper:]')
  printf '%s' "$CODE" | grep -Eq '^[A-Z0-9]{4}-[A-Z0-9]{4}$' ||
    status USAGE 64 "the activation code must look like ABCD-1234." "use the code exactly as it appears in the sentence the user pasted." \
      "The activation code did not look right. Please copy the sentence again from theodoreand.ai."
fi

# A build without its release keys (the repository's placeholder) would trust any plugin: it stops here.
real_key=
for pair in $PIN_MANIFEST_KEYS; do
  kid=${pair%%=*}; x=${pair#*=}
  [ "$kid" != manifest-unset ] && [ ${#x} -eq 43 ] && [ "$x" != AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA ] && real_key=1
done
[ -n "$real_key" ] || status INTERNAL 78 "this installer carries no release signing key (it was built without its keys), so it cannot trust any download." \
  "do not retry. Tell the user to download the installer again from https://theodoreand.ai/install." \
  "This copy of the Theodore installer is incomplete. Please download it again from theodoreand.ai/install."

command -v curl >/dev/null 2>&1 || status SETUP_FAILED 68 "cause NETWORK: curl is not installed." \
  "ask the user to install curl, then run the installer again." "Theodore's installer needs curl, which is missing on this computer."
case "$(uname -s 2>/dev/null)" in Darwin) PLAT=darwin ;; Linux) PLAT=linux ;; *) PLAT= ;; esac
case "$(uname -m 2>/dev/null)" in arm64|aarch64) ARCH=arm64 ;; x86_64|amd64) ARCH=x64 ;; *) ARCH= ;; esac
if [ "$PLAT" = linux ] && (ldd --version 2>&1 | grep -qi musl); then PLAT=; fi
[ -n "$PLAT" ] && [ -n "$ARCH" ] || status UNSUPPORTED_PLATFORM 69 "Theodore has no build for $(uname -s 2>/dev/null) $(uname -m 2>/dev/null)." \
  "do not retry. Use the deck-design skill with the host's own slide tools, or the Theodore connector if one is connected." \
  "Theodore's PowerPoint engine does not run on this kind of computer yet (it supports macOS, Windows x64 and Linux x64 or arm64 with glibc)."
mkdir -p "$HOME_DIR/staging" "$HOME_DIR/state" 2>/dev/null && chmod 700 "$HOME_DIR/state" 2>/dev/null &&
  WORK=$(mktemp -d "$HOME_DIR/staging/install.XXXXXX" 2>/dev/null) ||
  status SETUP_FAILED 68 "cause DISK: $HOME_DIR is not writable." "make $HOME_DIR writable (or set THEODORE_HOME), then run the installer again." \
    "Theodore could not create its folder ($HOME_DIR)."
# Claim the folder for this service in one exclusive step (written whole beside its place, then hard-linked in, which
# fails when a record is there), then check: two installers of different services racing on one empty folder cannot
# both pass.
if [ ! -e "$HOME_DIR/state/environment" ]; then
  env_file="$HOME_DIR/state/environment" claim="$HOME_DIR/state/.environment.claim" tmp="$HOME_DIR/state/.environment.$$"
  if printf '%s\n' "${API_BASE%/}" > "$tmp" 2>/dev/null && ! ln "$tmp" "$env_file" 2>/dev/null; then
    # No hard links: a claim directory decides who publishes, by rename; the others wait (never a half-written record).
    if mkdir "$claim" 2>/dev/null; then
      [ -e "$env_file" ] || mv -f "$tmp" "$env_file" 2>/dev/null
      rmdir "$claim" 2>/dev/null
    else
      n=0
      while [ -d "$claim" ] && [ ! -e "$env_file" ] && [ "$n" -lt 10 ]; do sleep 1; n=$((n + 1)); done
    fi
  fi
  rm -f "$tmp"
fi
foreign && { rm -rf "$WORK"; refuse_foreign; }
# The installer's own files live in one private folder (mktemp -d makes it 0700) and go when it ends, whatever happens.
trap 'rm -rf "$WORK"' EXIT
trap 'rm -rf "$WORK"; exit 130' INT TERM HUP

# curl: HTTPS only (plain HTTP only for a loopback test server), no redirect followed (no -L), 20 s to connect,
# abort when slower than 1 KB/s for 60 s, 30 minutes at most.
proto() { case "$1" in http://127.0.0.1[:/]*|http://localhost[:/]*) echo =https,http ;; *) echo =https ;; esac; }
fetch() { # url dest what
  curl -q -fsS --proto "$(proto "$1")" --connect-timeout 20 --speed-limit 1024 --speed-time 60 -m 1800 --retry 2 -o "$2" -w '%{http_code}' "$1" \
    >"$WORK/http" 2>"$WORK/err" && [ "$(cat "$WORK/http")" = 200 ] && return 0
  rc=$?
  cause=NETWORK
  case "$rc" in 35|51|53|58|59|60|77|83|90|91) cause=TLS_INTERCEPT ;; 23) cause=DISK ;; esac
  # a 407 status, never the digits of a port in the URL curl quotes (127.0.0.1:64071)
  { grep -qx 407 "$WORK/http" || grep -Eq '(^|[^0-9:])407([^0-9]|$)' "$WORK/err"; } 2>/dev/null && cause=PROXY_AUTH
  case "$(cat "$WORK/http" 2>/dev/null)" in 3??) detail="it answered with a redirect, which is not followed" ;; *) detail="curl exit $rc, HTTP $(cat "$WORK/http" 2>/dev/null)" ;; esac
  status SETUP_FAILED 68 "cause $cause: $3 could not be downloaded from $DL_BASE ($detail)." \
    "if the download host is blocked, register the plugin directly instead (step 2 of the install page); otherwise check the connection and run the installer again." \
    "Theodore's installer could not download its files. I will try the direct route."
}
checked() { # file sha what: delete and stop on a mismatch
  [ "$(sha256_of "$1")" = "$2" ] && return 0
  rm -f "$1"
  status SETUP_FAILED 68 "cause HASH_MISMATCH: $3 did not match the SHA-256 this installer carries." \
    "run the installer again; if it fails twice, tell the user to write to support@theodoreand.ai." "A Theodore download arrived damaged. I am trying again."
}

# --- 1. redeem the one-time code first: it lasts ten minutes, the downloads may take longer --------------------------
if [ -n "$CODE" ]; then
  name=$(hostname 2>/dev/null | tr -cd 'A-Za-z0-9._-' | cut -c1-60)
  body=$(printf '{"code":"%s","device_name":"%s","platform":"%s","arch":"%s","host":"installer","client_version":"%s"}' \
    "$CODE" "${name:-computer}" "$PLAT" "$ARCH" "$PIN_CLIENT_VERSION")
  pending="$HOME_DIR/state/activation.pending"
  ( umask 077; : > "$pending" )
  http=$(printf '%s' "$body" | curl -q -sS --proto "$(proto "$API_BASE")" --connect-timeout 20 -m 60 -o "$pending" -w '%{http_code}' -H 'Content-Type: application/json' --data-binary @- "$API_BASE/v1/device/activate" 2>/dev/null) || http=000
  if [ "$http" = 200 ]; then
    say "this computer is signed in (the one-time code was accepted)"
  else
    rm -f "$pending"
    say "the one-time code was not accepted (it may have expired or been used); a browser sign-in will be offered instead"
    CODE=
  fi
fi

# --- 2. Python and the client, each against its pinned SHA-256 (the launcher's stage 1, the same files) ----------------
eval "PY_PIN=\$PIN_PYTHON_${PLAT}_${ARCH}"
PY_PATH=${PY_PIN% *}; PY_SHA=${PY_PIN#* }
C_PATH=${PIN_CLIENT% *}; C_SHA=${PIN_CLIENT#* }
RT=$PIN_RUNTIME_ID; CV=$PIN_CLIENT_VERSION
PY="$HOME_DIR/runtime/$RT/python/bin/python3"
if [ ! -x "$PY" ] || [ "$(cat "$HOME_DIR/runtime/$RT/.parts/python" 2>/dev/null)" != "$PY_SHA" ]; then
  say "downloading Theodore's Python (checked against its pinned SHA-256)"
  fetch "$DL_BASE/$PY_PATH" "$WORK/python.tar.gz" "Theodore's Python"
  checked "$WORK/python.tar.gz" "$PY_SHA" "Theodore's Python"
  rm -rf "$HOME_DIR/runtime/$RT/python" "$WORK/python"
  mkdir -p "$WORK/python" "$HOME_DIR/runtime/$RT/.parts" && tar -xzf "$WORK/python.tar.gz" -C "$WORK/python" &&
    mv "$WORK/python/python" "$HOME_DIR/runtime/$RT/python" ||
    status SETUP_FAILED 68 "cause DISK: Theodore's Python could not be unpacked into $HOME_DIR." "free disk space, then run the installer again." "Theodore could not write its files."
  printf '%s\n' "$PY_SHA" > "$HOME_DIR/runtime/$RT/.parts/python"
  rm -f "$WORK/python.tar.gz"
fi
if [ ! -f "$HOME_DIR/client/$CV/__main__.py" ]; then
  fetch "$DL_BASE/$C_PATH" "$WORK/client.tar" "the Theodore client"
  checked "$WORK/client.tar" "$C_SHA" "the Theodore client"
  mkdir -p "$WORK/client" "$HOME_DIR/client" && tar -xf "$WORK/client.tar" -C "$WORK/client" && rm -rf "$HOME_DIR/client/$CV" &&
    mv "$WORK/client/$CV" "$HOME_DIR/client/$CV" ||
    status SETUP_FAILED 68 "cause DISK: the Theodore client could not be unpacked into $HOME_DIR." "free disk space, then run the installer again." "Theodore could not write its files."
fi
[ -s "$HOME_DIR/runtime/current.txt" ] || printf '%s\n' "$RT" > "$HOME_DIR/runtime/current.txt"
[ -s "$HOME_DIR/client/current.txt" ] || printf '%s\n' "$CV" > "$HOME_DIR/client/current.txt"

# --- 3. the plugin: the signed release document, then the archive it names --------------------------------------------
say "downloading the Theodore plugin (checked against its signed release document)"
THEODORE_HOME=$HOME_DIR "$PY" -I "$HOME_DIR/client/$CV" install-plugin --dl-base "$DL_BASE" --keys "$PIN_MANIFEST_KEYS"
rc=$?
[ "$rc" -eq 0 ] || exit "$rc"
SCRIPTS="$HOME_DIR/plugin/plugins/theodore/skills/pptx/scripts"
LAUNCHER="$SCRIPTS/theodore"

# --- 4. runtime and engine now, so the first deck request does not wait --------------------------------------------
say "downloading the rest of Theodore's runtime and its engine (about 100 to 130 MB in all); this is the long step"
THEODORE_HOME=$HOME_DIR THEODORE_BOOTSTRAP_WAIT=1800 sh "$LAUNCHER" setup --wait 1800
rc=$?
[ "$rc" -eq 0 ] || exit "$rc"
T="$HOME_DIR/bin/theodore"
[ -f "$T" ] || T=$LAUNCHER
theodore() { THEODORE_HOME=$HOME_DIR sh "$T" "$@"; }

# --- 5. the assistants on this computer ----------------------------------------------------------------------------------
say "registering Theodore in the assistants found on this computer"
theodore hosts register
HOSTS_RC=$?
[ -n "$ACCEPT" ] && theodore hosts settings --apply
[ -z "$ACCEPT" ] && theodore hosts settings

# --- 6. background downloads -----------------------------------------------------------------------------------------------
if theodore whoami >/dev/null 2>&1; then
  theodore guide pptx >/dev/null 2>&1          # installs the engine if the setup job has not done it yet
  say "asset packs (icons, flags, maps, backgrounds; about 95 MB), when published for this channel, download in the background"
  theodore packs install --all --background >/dev/null 2>&1
fi
if [ "$(uname -s)" = Darwin ] && ! command -v soffice >/dev/null 2>&1 && [ ! -d /Applications/LibreOffice.app ] && [ ! -d "$HOME_DIR/tools/libreoffice" ]; then
  say "LibreOffice (about 300 MB, from The Document Foundation) is downloading in the background into $HOME_DIR/tools; Theodore uses it to preview slides"
  theodore tools install libreoffice --background >/dev/null 2>&1
fi

# --- 7. the check the assistant reports: the last line says what failed, if anything did ------------------------------
say "the stable launcher is $T"
if ! theodore whoami >/dev/null 2>&1; then
  # Not signed in (no code, or it was not accepted): the last block carries the sign-in link for the assistant to
  # relay. The installer never opens a browser; only `theodore login --open`, asked for by the user, does.
  theodore login --start
  exit $?
fi
theodore doctor
rc=$?
[ "$rc" -eq 0 ] || exit "$rc"
if [ "$HOSTS_RC" -ne 0 ]; then
  status RETRY 75 "Theodore is installed, but it could not be added to every assistant on this computer (see the claude-code and codex lines above)." \
    "register by hand with the commands under 'Other ways' at https://theodoreand.ai/install, then start a new session." \
    "Theodore is installed, but I could not add it to one of your assistants automatically. I will add it by hand."
fi
exit 0
