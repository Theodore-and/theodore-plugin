#!/bin/sh
# Theodore installer for macOS and Linux. No admin rights, no package manager; everything goes into
# ~/.theodore (or THEODORE_HOME). Written to be run by an AI assistant or by a person:
#
#   sh install.sh --code ABCD-1234        sign this computer in with a one-time code and set everything up
#   sh install.sh                         the same, ending with a sign-in link instead of a code (no browser opens)
#   sh install.sh --no-settings ...       leave out Theodore's host settings (on by default: plugin auto-update, and
#                                         Theodore's own commands allowed in Claude Code and Codex); --accept-settings
#                                         is still accepted and changes nothing
#   sh install.sh --uninstall             remove Theodore from this computer
#
# What it does, in order: prints its own SHA-256; redeems the one-time code (it lasts an hour); downloads
# Theodore's Python and client and checks each against the SHA-256 pinned below; with them, fetches the signed
# plugin release document, checks its signature against the keys pinned below, then fetches the plugin archive it
# names and checks its SHA-256 and size; starts the rest of the runtime and the engine (about 100 to 130 MB in all
# before the first deck) as a background job and waits for it a few minutes at most, so that a host's time limit on
# the assistant's command never stops it; registers the plugin in Claude Code and Codex when they are installed; starts
# the asset packs (about 95 MB) and, on a Mac without LibreOffice, LibreOffice (about 300 MB) in the background; ends
# with `theodore doctor`, or with SETUP_IN_PROGRESS while the background download goes on. Running it again is safe:
# a code that was already used never removes the sign-in an earlier run saved.
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
PIN_CLIENT_VERSION="0.4.6"
PIN_CLIENT="client/client-0.4.6.tar 2febd58ae73a44bca68e354b063d30ce13cb292997ee1ca7a6b316cbafae4ae0"
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
NO_SETTINGS=

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
    --accept-settings) ;;                   # the settings are on by default now
    --no-settings) NO_SETTINGS=1 ;;
    *) status USAGE 64 "unknown installer option $1." "run the installer with --code ABCD-1234, or with no option for a browser sign-in." \
         "I called the Theodore installer with a wrong option; I am correcting it." ;;
  esac
  shift
done

[ -f "$0" ] && say "installer sha256 $(sha256_of "$0") (compare with https://theodoreand.ai/install)"
# install.md carries a one-time code in its first line. A copy the agent saved is removed: in this installer's own
# folder, and directly in the temporary folder or in a private folder there (tmp.*, theodore-*), only a file of this
# user whose first line is install.md's own; such a folder goes too once empty.
MD_MARK='Read this file as downloaded, not through a tool that summarises web pages'
remove_install_md() {
  own=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P) || own=
  roots="${TMPDIR:-/tmp} /tmp"
  case "$API_BASE" in http://127.0.0.1*|http://localhost*) roots=${TMPDIR:-/tmp} ;; esac      # a test: its own folder only
  for t in $roots; do
    for f in ${own:+"$own/install.md"} "${t%/}/install.md" "${t%/}"/tmp.*/install.md "${t%/}"/theodore-*/install.md; do
      [ -f "$f" ] && [ ! -L "$f" ] && [ -O "$f" ] || continue
      head -n 1 "$f" 2>/dev/null | grep -qF "$MD_MARK" || continue
      rm -f "$f"
      case "${f%/*}" in "${t%/}") ;; *) [ "${f%/*}" = "$own" ] || rmdir "${f%/*}" 2>/dev/null ;; esac
    done
  done
  return 0
}
remove_install_md

# The download folder of the install line (mktemp -d) goes when the installer ends, also when the agent ran the line
# split in two and dropped its cleanup (Codex does): only a folder named tmp.* or theodore-* that holds nothing but
# this file. The file stays on disk while the installer runs.
SELF_DIR=
case "$0" in */install.sh)
  self_dir=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd -P) || self_dir=
  case "${self_dir##*/}" in tmp.*|theodore-*) [ "$(ls -A "$self_dir" 2>/dev/null)" = install.sh ] && SELF_DIR=$self_dir ;; esac ;;
esac
cleanup_self() {
  [ -n "$SELF_DIR" ] && [ "$(ls -A "$SELF_DIR" 2>/dev/null)" = install.sh ] && rm -f "$SELF_DIR/install.sh" && rmdir "$SELF_DIR" 2>/dev/null
  return 0
}
trap cleanup_self EXIT
trap 'cleanup_self; exit 130' INT TERM HUP

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
# The engine's caches before 1.0.0-rc.7 (the client's _remove_old_engine_caches): ~/.cache/pptx-slide-raster (images of
# the user's slides), ~/.cache/pptx-icons, ~/.cache/pptx-flags and the icons' fallback in Node's temporary folder. Only
# files of the names the engine writes go; a folder goes once nothing else is in it, else it is named in $left.
remove_old_engine_caches() {
  old_removed=
  for d in "${HOME:-/nonexistent}/.cache/pptx-slide-raster" "${HOME:-/nonexistent}/.cache/pptx-icons" \
           "${HOME:-/nonexistent}/.cache/pptx-flags" "${TMPDIR:-${TMP:-${TEMP:-/tmp}}}/pptx-icons"; do
    [ -d "$d" ] && [ ! -L "$d" ] && [ -O "$d" ] || continue
    for e in "$d"/*; do
      [ -e "$e" ] && [ ! -L "$e" ] || continue
      case "${d##*/}" in
        pptx-slide-raster)
          [ -d "$e" ] && printf '%s\n' "${e##*/}" | grep -Eqx '[0-9a-f]{64}' || continue
          for f in "$e"/*; do
            [ -f "$f" ] && [ ! -L "$f" ] && printf '%s\n' "${f##*/}" | grep -Eqx 'slide-[0-9]{4}-[0-9]+\.jpg' && rm -f "$f"
          done
          rmdir "$e" 2>/dev/null ;;
        *) [ -f "$e" ] && printf '%s\n' "${e##*/}" | grep -Eqx '[0-9a-f]{40}\.(png|uri)' && rm -f "$e" ;;
      esac
    done
    if rmdir "$d" 2>/dev/null; then old_removed="$old_removed, $d"
    else left="$left; the folder $d (a cache of the engine before 1.0.0-rc.7: its files of the engine's were removed, and what else it holds is not Theodore's and was left)"; fi
  done
  old_removed=${old_removed#, }
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
    cleanup_self
    exec sh "$HOME_DIR/bin/theodore" uninstall
  fi
  # No launcher left: the specimen folders the client recorded still go, and the record with them.
  left=
  remove_specimens
  [ -n "$left" ] || rm -f "$HOME_DIR/state/specimens.txt" 2>/dev/null
  remove_old_engine_caches
  rec="$HOME_DIR/state/host-settings.json"
  if grep -qE '"(rules|marketplace|created_marketplace|set_auto_update|created_keys|git_bash|instructions)":' "$rec" 2>/dev/null; then
    left="$left; Theodore's changes to the Claude Code settings.json and its marked lines in CLAUDE.md or AGENTS.md (theodore:begin to theodore:end), which need Theodore's runtime to take back exactly: what it added and the values before are kept in $rec. To take them back, install Theodore again and run its uninstall, or edit that settings file by that record"
  fi
  rmdir "$HOME_DIR/state" "$HOME_DIR" 2>/dev/null
  [ -z "$left" ] || status RETRY 75 "Theodore is not installed in $HOME_DIR, but these could not be removed: ${left#; }." \
    "tell the user what is left; each item is a folder that can be deleted by hand." "A few of Theodore's temporary files could not be removed: ${left#; }."
  done_old=
  if [ -n "$old_removed" ]; then done_old="; the engine's former caches were removed ($old_removed)"; fi
  status OK 0 "Theodore is not installed in $HOME_DIR$done_old." "nothing to do." "Theodore was not installed on this computer."
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
trap 'rm -rf "$WORK"; cleanup_self' EXIT
trap 'rm -rf "$WORK"; cleanup_self; exit 130' INT TERM HUP

# curl: HTTPS only (plain HTTP only for a loopback test server), no redirect followed (no -L), 20 s to connect,
# abort when slower than 1 KB/s for 60 s, 30 minutes at most.
proto() { case "$1" in http://127.0.0.1[:/]*|http://localhost[:/]*) echo =https,http ;; *) echo =https ;; esac; }
# --- every address of Theodore's hosts (begin) ---
# Theodore's hosts resolve to several addresses, and from some networks one of them does not answer, or completes the
# TCP connection but never the TLS handshake (or resets it), while another works. curl moves to the next address only
# when the TCP connection fails: after a handshake that hangs or is reset it gives up (exit 28 or 35), and --retry starts
# again from the same first address. So, without a proxy, each address is tried in turn (curl --resolve: the name, its
# SNI and its certificate check stay those of the host), 4 s each to connect and finish the handshake; the address that
# worked is tried first for the rest of this run. When every address only ran out of time (a slow link, not a dead
# address), the first is tried once more with the caller's own connect timeout. With a proxy (which chooses the address itself), for a loopback test
# server, or when the addresses cannot be listed, curl runs as before.
host_addresses() { # host: its addresses, IPv4 first, one per line, at most six
  if command -v getent >/dev/null 2>&1; then
    { getent ahostsv4 "$1" 2>/dev/null; getent ahostsv6 "$1" 2>/dev/null | grep -iv '^::ffff:'; } | awk '{ print $1 }'
  elif command -v dscacheutil >/dev/null 2>&1; then
    dscacheutil -q host -a name "$1" 2>/dev/null | sed -n 's/^ip_address: *//p'
    dscacheutil -q host -a name "$1" 2>/dev/null | sed -n 's/^ipv6_address: *//p'
  fi | grep -E '^[0-9A-Fa-f.:]+$' | awk '!seen[$0]++' | head -n 6
}
# curl_host tries url [curl options]: prints the HTTP status (000 when nothing answered) and returns curl's exit status.
# The options carry -o; CURL_DATA, when set, is the body (sent on stdin, so it is never on a command line). `tries`
# rounds at most (the earlier `--retry`): a round ends at the first address that answered.
CURL_DATA=
curl_once() { # url [curl options]
  ch_u=$1; shift
  if [ -n "$CURL_DATA" ]; then printf '%s' "$CURL_DATA" | curl "$@" --data-binary @- -w '%{http_code} %{time_appconnect}' "$ch_u"
  else curl "$@" -w '%{http_code} %{time_appconnect}' "$ch_u"; fi
}
curl_host() {
  ch_tries=$1 ch_url=$2; shift 2
  ch_hostport=${ch_url#https://}; ch_hostport=${ch_hostport%%/*}; ch_host=${ch_hostport%%:*}; ch_port=443
  case "$ch_hostport" in *:*) ch_port=${ch_hostport##*:} ;; esac
  ch_addrs=
  case "$ch_url" in https://*)
    if [ -z "${https_proxy:-}${HTTPS_PROXY:-}${all_proxy:-}${ALL_PROXY:-}" ]; then
      case "$ch_host$ch_port" in ''|*[!A-Za-z0-9.-]*) ;; *) ch_addrs=$(host_addresses "$ch_host") ;; esac
    fi ;;
  esac
  if [ -z "$ch_addrs" ]; then
    ch_out=$(curl_once "$ch_url" "$@" --retry $((ch_tries - 1))); ch_rc=$?
    printf '%s' "${ch_out%% *}"; return "$ch_rc"
  fi
  ch_key=$(printf '%s' "$ch_host" | tr '.-' '__')
  eval "ch_good=\${GOOD_ADDR_$ch_key:-}"
  [ -n "$ch_good" ] && ch_addrs=$(printf '%s\n%s\n' "$ch_good" "$ch_addrs" | awk '!seen[$0]++')
  ch_round=1
  while :; do
    ch_slow=1
    for ch_a in $ch_addrs; do
      case "$ch_a" in *:*) ch_r="[$ch_a]" ;; *) ch_r=$ch_a ;; esac
      ch_out=$(curl_once "$ch_url" "$@" --retry 0 --connect-timeout 4 --resolve "$ch_host:$ch_port:$ch_r"); ch_rc=$?
      ch_http=${ch_out%% *} ch_appc=${ch_out#* }
      if [ "$ch_rc" -ne 0 ] && [ "$ch_http" = 000 ] && [ "${ch_appc%%[1-9]*}" = "$ch_appc" ]; then
        [ "$ch_rc" = 28 ] || ch_slow=
        case "$ch_rc" in 7|28|35|52|55|56) continue ;; esac      # this address did not connect or finish the handshake
      fi
      ch_slow=
      eval "GOOD_ADDR_$ch_key=\$ch_a"
      break
    done
    [ "$ch_round" -lt "$ch_tries" ] || break
    case "$ch_http:$ch_rc" in 408:*|429:*|5??:*|000:7|000:18|000:28|000:35|000:52|000:55|000:56) ;; *) break ;; esac  # as --retry
    sleep "$ch_round"; ch_round=$((ch_round + 1))
  done
  if [ -n "$ch_slow" ]; then      # every address only ran out of its 4 s: a slow link, given the caller's own time once
    for ch_a in $ch_addrs; do break; done
    case "$ch_a" in *:*) ch_r="[$ch_a]" ;; *) ch_r=$ch_a ;; esac
    ch_out=$(curl_once "$ch_url" "$@" --retry 0 --resolve "$ch_host:$ch_port:$ch_r"); ch_rc=$?
    ch_http=${ch_out%% *}
    [ "$ch_rc" -eq 0 ] && eval "GOOD_ADDR_$ch_key=\$ch_a"
  fi
  printf '%s' "$ch_http"; return "$ch_rc"
}
# --- every address of Theodore's hosts (end) ---
fetch() { # url dest what
  curl_host 3 "$1" -q -fsS --proto "$(proto "$1")" --connect-timeout 20 --speed-limit 1024 --speed-time 60 -m 1800 -o "$2" \
    >"$WORK/http" 2>"$WORK/err" && [ "$(cat "$WORK/http")" = 200 ] && return 0
  rc=$?
  cause=NETWORK
  case "$rc" in 35|51|53|58|59|60|77|83|90|91) cause=TLS_INTERCEPT ;; 23) cause=DISK ;; esac
  # a 407 status, never the digits of a port in the URL curl quotes (127.0.0.1:64071)
  { grep -qx 407 "$WORK/http" || grep -Eq '(^|[^0-9:])407([^0-9]|$)' "$WORK/err"; } 2>/dev/null && cause=PROXY_AUTH
  case "$(cat "$WORK/http" 2>/dev/null)" in 3??) detail="it answered with a redirect, which is not followed" ;; *) detail="curl exit $rc, HTTP $(cat "$WORK/http" 2>/dev/null)" ;; esac
  if [ "$cause" = NETWORK ] && in_sandbox; then
    status SETUP_FAILED 68 "cause $cause: $3 could not be downloaded from $DL_BASE ($detail); this cloud workspace's network refuses or cannot resolve it." \
      "tell the user the setting below; once they have changed it, running the same installer line again is right (not before)." \
      "This workspace's network does not let Theodore reach its sites. In Claude, open Settings > Capabilities, and under \"Use the internet while making files\" > Allowed websites add theodoreand.ai and *.theodoreand.ai, then try again. ChatGPT's ordinary chat has no internet access; ChatGPT Work mode does."
  fi
  status SETUP_FAILED 68 "cause $cause: $3 could not be downloaded from $DL_BASE ($detail)." \
    "if the download host is blocked, register the plugin directly instead (step 2 of the install page); otherwise check the connection and run the installer again." \
    "Theodore's installer could not download its files. I will try the direct route."
}
# The client's truth rule for a flag (core._flag): empty, 0, false and no (any case) are false, anything else is true.
truthy() {
  case "$(printf '%s' "${1:-}" | tr 'A-Z' 'a-z' | tr -d ' ')" in ''|0|false|no) return 1 ;; *) return 0 ;; esac
}
# A Claude cloud session (core._claude_cloud).
claude_cloud() {
  truthy "${CLAUDE_CODE_REMOTE:-}" || [ "${CLAUDE_CODE_SESSION_ORIGIN:-}" = claude_ai_chat ] ||
    case "${CLAUDE_CODE_ENTRYPOINT:-}" in remote*) true ;; *) false ;; esac
}
# An assistant's cloud sandbox (the launcher's in_sandbox and the client's core.cloud_sandbox make the same test).
in_sandbox() {
  [ -n "${THEODORE_EPHEMERAL:-}" ] || [ -d /mnt/skills ] || [ -d /mnt/user-data ] || [ -d /home/oai ] || claude_cloud
}
# The assistant running this installer, as the client's core.host_name() tells it ("installer" when unknown).
host_of() {
  explicit=$(printf '%s' "${THEODORE_HOST:-}" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9-' | cut -c1-32 | sed 's/^-*//')
  if [ -n "$explicit" ]; then echo "$explicit"
  elif claude_cloud; then echo claude-web
  elif [ -n "${CLAUDECODE:-}${CLAUDE_CODE_ENTRYPOINT:-}" ]; then echo claude-code
  elif [ "$PLAT" = linux ] && truthy "${CODEX_NETWORK_PROXY_ACTIVE:-}" && { [ -n "${CODEX_PROXY_CERT:-}" ] || [ -d /opt/codex/runtimes ]; }; then echo chatgpt
  # before CLAUDE_PLUGIN_ROOT: Codex sets it for the plugins it runs
  elif [ -n "${CODEX_THREAD_ID:-}${CODEX_SESSION_ID:-}${CODEX_SANDBOX:-}${CODEX_HOME:-}${CODEX_MANAGED_BY_NPM:-}${CODEX_VERSION:-}" ]; then echo codex
  elif [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then echo claude-code
  else echo installer; fi
}
# fetch, then check against the pinned SHA-256; a damaged download (a flaky network) is fetched afresh, three times at most
fetch_checked() { # url dest sha what
  for try in 1 2; do
    fetch "$1" "$2" "$4"
    [ "$(sha256_of "$2")" = "$3" ] && return 0
    rm -f "$2"; say "$4 arrived damaged; downloading it again"; sleep $((try * 2))
  done
  fetch "$1" "$2" "$4"
  checked "$2" "$3" "$4"
}
checked() { # file sha what: delete and stop on a mismatch
  [ "$(sha256_of "$1")" = "$2" ] && return 0
  rm -f "$1"
  status SETUP_FAILED 68 "cause HASH_MISMATCH: $3 did not match the SHA-256 this installer carries." \
    "run the installer again; if it fails twice, tell the user to write to support@theodoreand.ai." "A Theodore download arrived damaged. I am trying again."
}

# --- 1. redeem the one-time code first: it lasts an hour, the downloads may take longer ------------------------------
RATE_WAIT= LIMIT_REFUSED=
if [ -n "$CODE" ]; then
  name=$(hostname 2>/dev/null | tr -cd 'A-Za-z0-9._-' | cut -c1-60)
  body=$(printf '{"code":"%s","device_name":"%s","platform":"%s","arch":"%s","host":"%s","client_version":"%s"}' \
    "$CODE" "${name:-computer}" "$PLAT" "$ARCH" "$(host_of)" "$PIN_CLIENT_VERSION")
  # The answer goes to a file of this installer's private folder and replaces state/activation.pending only when the
  # code was accepted: a refused code (spent by an earlier run of the same line) never touches the sign-in that run saved.
  pending="$HOME_DIR/state/activation.pending" answer="$WORK/activation.answer"
  ( umask 077; : > "$answer" )
  CURL_DATA=$body
  http=$(curl_host 1 "$API_BASE/v1/device/activate" -q -sS --proto "$(proto "$API_BASE")" --connect-timeout 20 -m 60 -o "$answer" -H 'Content-Type: application/json' 2>/dev/null) || http=000
  CURL_DATA=
  if [ "$http" = 200 ] && [ -s "$answer" ] && mv -f "$answer" "$pending"; then
    say "this computer is signed in (the one-time code was accepted)"
  elif [ "$http" = 200 ] && [ -s "$answer" ]; then
    rm -f "$answer"
    status SETUP_FAILED 68 "cause DISK: the one-time code was accepted but this computer could not save the login in $HOME_DIR/state." \
      "the one-time code was accepted but this computer could not save the login (cause DISK); fix the folder's permissions and run the same line again within 30 minutes: the same code works again on this computer until it has been used." \
      "Theodore could not save its login on this computer. Its folder may be protected; once that is fixed, the same install line works again for 30 minutes."
  elif [ "$http" = 429 ] && grep -q '"rate_limited"' "$answer" 2>/dev/null; then
    # The service's limit on sign-ins per account (or network) in an hour (W-16): the code was not used and is not
    # wrong. Everything else installs; the last status says when to run the same line again.
    RATE_WAIT=$(sed -n 's/.*Try again in \([0-9][0-9]*\) seconds.*/\1/p' "$answer" | head -n 1)
    RATE_WAIT=$(( (${RATE_WAIT:-3600} + 59) / 60 ))
    rm -f "$answer"
    say "too many log-ins on this Theodore account in the last hour, so this computer is not signed in yet; the one-time code was not used (try again in $RATE_WAIT minutes)"
    CODE=
  elif [ "$http" = 409 ] && grep -q '"computer_limit"' "$answer" 2>/dev/null; then
    # W-17: the account already has its maximum number of computers logged in. The code was not used (it works until
    # it expires) and a browser sign-in would be refused the same way: no link. The answer holds no secret; the client
    # turns it into the last status (login --refused), with the service's own message.
    mv -f "$answer" "$WORK/activation.refused" && LIMIT_REFUSED=$WORK/activation.refused
    say "the Theodore service did not log this computer in: this account already has its maximum number of computers logged in (the one-time code was not used); what to do follows at the end"
    CODE=
  elif [ "$http" = 000 ] || [ "${http#5}" != "$http" ]; then
    rm -f "$answer"
    case "$http" in 000) why="the Theodore service could not be reached" ;; *) why="the Theodore service answered HTTP $http" ;; esac
    say "$why, so the one-time code was not used; if this computer is not logged in already, a login link follows at the end"
    CODE=
  else
    rm -f "$answer"
    if [ -s "$pending" ]; then
      say "the one-time code was not accepted (it was used already, by an earlier run of this installer); the login that run saved is kept"
    else
      say "the one-time code was not accepted (it may have expired or been used); if this computer is not logged in already, a login link follows at the end"
    fi
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
  fetch_checked "$DL_BASE/$PY_PATH" "$WORK/python.tar.gz" "$PY_SHA" "Theodore's Python"
  rm -rf "$HOME_DIR/runtime/$RT/python" "$WORK/python"
  mkdir -p "$WORK/python" "$HOME_DIR/runtime/$RT/.parts" && tar -xzf "$WORK/python.tar.gz" -C "$WORK/python" &&
    mv "$WORK/python/python" "$HOME_DIR/runtime/$RT/python" ||
    status SETUP_FAILED 68 "cause DISK: Theodore's Python could not be unpacked into $HOME_DIR." "free disk space, then run the installer again." "Theodore could not write its files."
  printf '%s\n' "$PY_SHA" > "$HOME_DIR/runtime/$RT/.parts/python"
  rm -f "$WORK/python.tar.gz"
fi
if [ ! -f "$HOME_DIR/client/$CV/__main__.py" ]; then
  fetch_checked "$DL_BASE/$C_PATH" "$WORK/client.tar" "$C_SHA" "the Theodore client"
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
# Until step 5 has registered the plugin, this marker says so: if this installer is stopped before then, the next
# Theodore command registers it (as `theodore hosts register` does, which removes the marker).
[ -e "$HOME_DIR/state/hosts.pending" ] || ( umask 077; : > "$HOME_DIR/state/hosts.pending" )

# --- 4. runtime and engine: a background job, so the first deck request does not wait -------------------------------
# The job outlives this installer. It is waited for a few minutes at most: an assistant's command has a time limit
# (Claude Code stops a command after 10 minutes), and the job must not be what that limit stops.
# Codex (CODEX_THREAD_ID: its CLI, the ChatGPT app's Codex mode, ChatGPT's Work mode) lets a command run longer and asks
# before each command of the session that runs this installer (its rules load at the next session): wait longer here,
# so the assistant has nothing to poll.
SETUP_WAIT_DEFAULT=180
[ -n "${CODEX_THREAD_ID:-}" ] && SETUP_WAIT_DEFAULT=540
SETUP_WAIT=${THEODORE_INSTALL_WAIT:-$SETUP_WAIT_DEFAULT}
case "$SETUP_WAIT" in ''|*[!0-9]*) SETUP_WAIT=$SETUP_WAIT_DEFAULT ;; esac
say "downloading the rest of Theodore's runtime and its engine (about 100 to 130 MB in all) in the background; this is the long step"
if [ -n "${CODEX_THREAD_ID:-}" ]; then
  # Codex shows a long command's output as it comes: a line a minute says the download is moving, so the assistant has
  # no reason to inspect processes (each such command asks first in this session).
  setup_dir=$(mktemp -d "${TMPDIR:-/tmp}/theodore-setup.XXXXXX") || exit 70
  ( THEODORE_HOME=$HOME_DIR THEODORE_BOOTSTRAP_WAIT=$SETUP_WAIT sh "$LAUNCHER" setup --wait "$SETUP_WAIT" >"$setup_dir/out"
    echo $? >"$setup_dir/rc" ) &
  setup_pid=$!
  waited=0
  while kill -0 "$setup_pid" 2>/dev/null; do
    sleep 5; waited=$((waited + 5))
    if [ $((waited % 60)) -eq 0 ] && kill -0 "$setup_pid" 2>/dev/null; then
      job=$(cat "$HOME_DIR/state/jobs/setup.json" 2>/dev/null)
      phase=$(printf '%s' "$job" | sed -n 's/.*"phase": *"\([^"]*\)".*/\1/p')
      pct=$(printf '%s' "$job" | sed -n 's/.*"percent": *\([0-9]*\).*/\1/p')
      say "still downloading (${phase:-starting}, ${pct:-0}%); this installer waits at most $((SETUP_WAIT - waited > 0 ? SETUP_WAIT - waited : 0)) s more, then the download goes on by itself"
    fi
  done
  wait "$setup_pid" 2>/dev/null
  setup_out=$(cat "$setup_dir/out" 2>/dev/null)
  rc=$(cat "$setup_dir/rc" 2>/dev/null); rc=${rc:-70}
  rm -rf "$setup_dir"
else
  setup_out=$(THEODORE_HOME=$HOME_DIR THEODORE_BOOTSTRAP_WAIT=$SETUP_WAIT sh "$LAUNCHER" setup --wait "$SETUP_WAIT")
  rc=$?
fi
SETUP_FAILED_OUT=
case "$rc" in
  0) ;;
  67) say "the download goes on in the background after this installer has ended" ;;
  68) SETUP_FAILED_OUT=$setup_out ;;          # reported after the plugin is registered: the next command resumes it
  *) printf '%s\n' "$setup_out"; exit "$rc" ;;
esac
T="$HOME_DIR/bin/theodore"
[ -f "$T" ] || T=$LAUNCHER
theodore() { THEODORE_HOME=$HOME_DIR sh "$T" "$@"; }

# --- 5. the assistants on this computer ----------------------------------------------------------------------------------
say "registering Theodore in the assistants found on this computer"
theodore hosts register
HOSTS_RC=$?
# The person asked their assistant to set Theodore up: its host settings come with it (Theodore's own commands run
# without a prompt, the plugin updates itself); --no-settings leaves them out, and an organisation policy always wins.
if [ -n "$NO_SETTINGS" ]; then
  theodore hosts settings --remove >/dev/null 2>&1
  say "Theodore's host settings were left out (--no-settings): Claude Code and Codex ask before each Theodore command"
else
  theodore hosts settings --apply
  say "Theodore's own commands, and the assistant's edits of a deck's files in .theodore/workspaces, run without asking each time; to turn that off: \"$T\" hosts settings --remove"
fi
if [ -n "$SETUP_FAILED_OUT" ]; then
  say "the stable launcher is \"$T\""
  printf '%s\n' "$SETUP_FAILED_OUT"
  exit 68
fi

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
say "the stable launcher is \"$T\""
if [ -n "$RATE_WAIT" ] && ! theodore whoami >/dev/null 2>&1; then
  status RETRY 75 "Theodore is installed, but this computer is not signed in: there have been too many log-ins on this Theodore account in the last hour, and the one-time code was not used. Try again in $RATE_WAIT minutes." \
    "do not retry now. Tell the user, and run the same install line again in $RATE_WAIT minutes (with a fresh sentence from the Theodore site if the code has expired by then)." \
    "There have been too many Theodore log-ins on your account in the last hour. Please try again in about $RATE_WAIT minutes; your code was not used."
fi
if [ -n "$LIMIT_REFUSED" ] && ! theodore whoami >/dev/null 2>&1; then
  # W-17: the computer limit, never a sign-in link (it would be refused the same way)
  theodore login --refused "$LIMIT_REFUSED"
  exit $?
fi
if ! theodore whoami >/dev/null 2>&1; then
  # Not signed in (no code, or it was not accepted): the last block carries the sign-in link for the assistant to
  # relay. The installer never opens a browser; only `theodore login --open`, asked for by the user, does.
  theodore login --start
  exit $?
fi
doctor_out=$(theodore doctor)
rc=$?
checks_only() { printf '%s\n' "$doctor_out" | sed -e :a -e '$d;N;2,3ba' -e 'P;D'; }
# The last status is the client's welcome (lane ONBOARD): a few short lines for the assistant to give in this
# conversation, the Studio question once, and what this host needs (a new session, the download still going on).
welcome() {
  welcome_out=$(theodore welcome "$@")
  welcome_rc=$?
  case "$welcome_rc" in 0|67) return 0 ;; esac
  return 1
}
if [ "$rc" -eq 67 ] && printf '%s\n' "$doctor_out" | grep -q '^NEXT: run the same command again'; then
  # Still downloading in the background: doctor's checks, then a status that names doctor itself (its own status says
  # "the same command", which here would be this installer). In Codex the welcome says to run nothing more: every
  # command of this session asks first, the download finishes by itself, and a deck request in a new session waits.
  checks_only
  if [ "$HOSTS_RC" -eq 0 ] && welcome --downloading; then printf '%s\n' "$welcome_out"; exit "$welcome_rc"; fi
  where=$(printf '%s\n' "$doctor_out" | sed -n 's/^THEODORE SETUP_IN_PROGRESS: \(.*\)\.$/\1/p' | tail -n 1)
  status SETUP_IN_PROGRESS 67 "Theodore is installed; the rest of its runtime and its engine are still downloading in the background, which goes on after this installer has ended (${where:-downloading})." \
    "run \"$T\" status --wait 100 (it waits for the download and returns as soon as it is done), again while it says SETUP_IN_PROGRESS (not this installer), then report its last line." \
    "Theodore is installed and is finishing its download in the background. This takes a few minutes; I will check on it."
fi
printf '%s\n' "$doctor_out"
[ "$rc" -eq 0 ] || exit "$rc"
if [ "$HOSTS_RC" -eq 0 ] && welcome; then printf '%s\n' "$welcome_out"; exit "$welcome_rc"; fi
if [ "$HOSTS_RC" -ne 0 ]; then
  status RETRY 75 "Theodore is installed, but it could not be added to every assistant on this computer (see the claude-code and codex lines above)." \
    "register by hand with the commands under 'Other ways' at https://theodoreand.ai/install, then start a new session." \
    "Theodore is installed, but I could not add it to one of your assistants automatically. I will add it by hand."
fi
exit 0
