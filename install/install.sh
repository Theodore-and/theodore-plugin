#!/bin/sh
# Theodore installer for macOS and Linux. No admin rights, no package manager; everything goes into
# ~/.theodore (or THEODORE_HOME). Written to be run by an AI assistant or by a person:
#
#   sh install.sh --code ABCD-1234        sign this computer in with a one-time code and set everything up
#   sh install.sh                         the same, with a browser sign-in instead of a code
#   sh install.sh --accept-settings ...   also turn on plugin auto-update and allow the launcher in Claude Code
#   sh install.sh --uninstall             remove Theodore from this computer
#
# What it does, in order: prints its own SHA-256; downloads the Theodore plugin (a few small text files) into
# <home>/plugin and checks its checksum; redeems the code before any large download; downloads Theodore's Python
# runtime and engine (about 120 MB); registers the plugin in Claude Code and Codex when they are installed;
# starts the asset packs (about 95 MB) and, on a Mac without LibreOffice, LibreOffice (about 300 MB) in the
# background; ends with `theodore doctor`. Running it again is safe.
# Lines starting with "THEODORE" are statuses for the assistant: follow the NEXT line.

set -u

DL_BASE=${THEODORE_DL_BASE:-https://dl.theodoreand.ai}
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
jget() { sed -n 's/^[[:space:]]*"'"$2"'"[[:space:]]*:[[:space:]]*"\{0,1\}\([^",]*\)"\{0,1\}[[:space:]]*,\{0,1\}[[:space:]]*$/\1/p' "$1" 2>/dev/null | head -n 1; }

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

if [ -n "$UNINSTALL" ]; then
  if [ -f "$HOME_DIR/bin/theodore" ]; then
    exec sh "$HOME_DIR/bin/theodore" uninstall
  fi
  status OK 0 "Theodore is not installed in $HOME_DIR." "nothing to do." "Theodore was not installed on this computer."
fi

if [ -n "$CODE" ]; then
  CODE=$(printf '%s' "$CODE" | tr '[:lower:]' '[:upper:]')
  printf '%s' "$CODE" | grep -Eq '^[A-Z0-9]{4}-[A-Z0-9]{4}$' ||
    status USAGE 64 "the activation code must look like ABCD-1234." "use the code exactly as it appears in the sentence the user pasted." \
      "The activation code did not look right. Please copy the sentence again from theodoreand.ai."
fi
command -v curl >/dev/null 2>&1 || status SETUP_FAILED 68 "cause NETWORK: curl is not installed." \
  "ask the user to install curl, then run the installer again." "Theodore's installer needs curl, which is missing on this computer."
mkdir -p "$HOME_DIR/staging" "$HOME_DIR/state" 2>/dev/null && chmod 700 "$HOME_DIR/state" 2>/dev/null ||
  status SETUP_FAILED 68 "cause DISK: $HOME_DIR is not writable." "make $HOME_DIR writable (or set THEODORE_HOME), then run the installer again." \
    "Theodore could not create its folder ($HOME_DIR)."

fetch() { # url dest what
  curl -fsSL --retry 2 --connect-timeout 20 -o "$2" "$1" 2>"$HOME_DIR/staging/install.err" && return 0
  rc=$?
  cause=NETWORK
  case "$rc" in 35|51|53|58|59|60|77|83|90|91) cause=TLS_INTERCEPT ;; 23) cause=DISK ;; esac
  grep -q 407 "$HOME_DIR/staging/install.err" 2>/dev/null && cause=PROXY_AUTH
  rm -f "$HOME_DIR/staging/install.err"
  status SETUP_FAILED 68 "cause $cause: $3 could not be downloaded from $DL_BASE (curl exit $rc)." \
    "if the download host is blocked, register the plugin directly instead (step 2 of the install page); otherwise check the connection and run the installer again." \
    "Theodore's installer could not download its files. I will try the direct route."
}

# --- 1. the plugin (stub skills and launcher: small text files) ---------------------------------------------------
say "downloading the Theodore plugin"
fetch "$DL_BASE/plugin/latest.txt" "$HOME_DIR/staging/latest.txt" "the plugin version"
read -r P_VERSION P_SHA < "$HOME_DIR/staging/latest.txt" || true
printf '%s' "${P_VERSION:-}" | grep -Eq '^[0-9A-Za-z.+-]+$' && [ -n "${P_SHA:-}" ] ||
  status SETUP_FAILED 68 "cause HASH_MISMATCH: the plugin version file is malformed." "run the installer again; if it fails twice, tell the user to write to support@theodoreand.ai." \
    "A Theodore download arrived damaged. I am trying again."
fetch "$DL_BASE/plugin/theodore-plugin-$P_VERSION.tar.gz" "$HOME_DIR/staging/plugin.tar.gz" "the plugin"
[ "$(sha256_of "$HOME_DIR/staging/plugin.tar.gz")" = "$P_SHA" ] || {
  rm -f "$HOME_DIR/staging/plugin.tar.gz"
  status SETUP_FAILED 68 "cause HASH_MISMATCH: the plugin did not match its published checksum." \
    "run the installer again; if it fails twice, tell the user to write to support@theodoreand.ai." "A Theodore download arrived damaged. I am trying again."
}
rm -rf "$HOME_DIR/plugin.partial"
mkdir -p "$HOME_DIR/plugin.partial" && tar -xzf "$HOME_DIR/staging/plugin.tar.gz" -C "$HOME_DIR/plugin.partial" ||
  status SETUP_FAILED 68 "cause DISK: the plugin could not be unpacked into $HOME_DIR." "free disk space, then run the installer again." "Theodore could not write its files."
rm -rf "$HOME_DIR/plugin" && mv "$HOME_DIR/plugin.partial" "$HOME_DIR/plugin"
rm -f "$HOME_DIR/staging/plugin.tar.gz" "$HOME_DIR/staging/latest.txt" "$HOME_DIR/staging/install.err"
SCRIPTS="$HOME_DIR/plugin/plugins/theodore/skills/pptx/scripts"
LAUNCHER="$SCRIPTS/theodore"
[ -f "$LAUNCHER" ] && [ -f "$SCRIPTS/launcher.json" ] ||
  status INTERNAL 78 "the plugin archive has no launcher." "tell the user to write to support@theodoreand.ai." "Theodore's download is incomplete."
chmod 755 "$LAUNCHER" 2>/dev/null
say "plugin $P_VERSION is in $HOME_DIR/plugin"

# --- 2. redeem the one-time code first: it lasts ten minutes, the downloads may take longer -------------------------
if [ -n "$CODE" ]; then
  API_BASE=$(jget "$SCRIPTS/launcher.json" api_base)
  case "$(uname -s)" in Darwin) plat=darwin ;; *) plat=linux ;; esac
  case "$(uname -m)" in arm64|aarch64) arch=arm64 ;; *) arch=x64 ;; esac
  name=$(hostname 2>/dev/null | tr -cd 'A-Za-z0-9._-' | cut -c1-60)
  body=$(printf '{"code":"%s","device_name":"%s","platform":"%s","arch":"%s","host":"installer","client_version":"%s"}' \
    "$CODE" "${name:-computer}" "$plat" "$arch" "$(jget "$SCRIPTS/launcher.json" stage1_client_version)")
  pending="$HOME_DIR/state/activation.pending"
  ( umask 077; : > "$pending" )
  http=$(printf '%s' "$body" | curl -sS --connect-timeout 20 -m 60 -o "$pending" -w '%{http_code}' -H 'Content-Type: application/json' --data-binary @- "$API_BASE/v1/device/activate" 2>/dev/null) || http=000
  if [ "$http" = 200 ]; then
    say "this computer is signed in (the one-time code was accepted)"
  else
    rm -f "$pending"
    say "the one-time code was not accepted (it may have expired or been used); a browser sign-in will be offered instead"
    CODE=
  fi
fi

# --- 3. runtime and engine now, so the first deck request does not wait --------------------------------------------
say "downloading Theodore's runtime (about 120 MB); this is the long step"
THEODORE_HOME=$HOME_DIR THEODORE_BOOTSTRAP_WAIT=1800 sh "$LAUNCHER" setup --wait 1800
rc=$?
[ "$rc" -eq 0 ] || exit "$rc"
T="$HOME_DIR/bin/theodore"
[ -f "$T" ] || T=$LAUNCHER
theodore() { THEODORE_HOME=$HOME_DIR sh "$T" "$@"; }

if [ -z "$CODE" ]; then
  theodore whoami >/dev/null 2>&1 || theodore login --start
fi

# --- 4. the assistants on this computer ----------------------------------------------------------------------------------
say "registering Theodore in the assistants found on this computer"
theodore hosts register
[ -n "$ACCEPT" ] && theodore hosts settings --apply
[ -z "$ACCEPT" ] && theodore hosts settings

# --- 5. background downloads -----------------------------------------------------------------------------------------------
if theodore whoami >/dev/null 2>&1; then
  theodore guide pptx >/dev/null 2>&1          # installs the engine if the setup job has not done it yet
  say "asset packs (icons, flags, maps, backgrounds; about 95 MB) are downloading in the background"
  theodore packs install --all --background >/dev/null 2>&1
fi
if [ "$(uname -s)" = Darwin ] && ! command -v soffice >/dev/null 2>&1 && [ ! -d /Applications/LibreOffice.app ] && [ ! -d "$HOME_DIR/tools/libreoffice" ]; then
  say "LibreOffice (about 300 MB, from The Document Foundation) is downloading in the background into $HOME_DIR/tools; Theodore uses it to preview slides"
  theodore tools install libreoffice --background >/dev/null 2>&1
fi

# --- 6. the check the assistant reports -------------------------------------------------------------------------------------
say "the stable launcher is $T"
theodore doctor
