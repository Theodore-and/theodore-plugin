#!/bin/sh
# The local runner: every check of this repository, per platform, from one Mac. It is the source of truth: nothing
# depends on GitHub Actions (the files in .github/workflows/ are a manual-only mirror that no push or pull request starts).
#
#   ci/run.sh macos-arm64              natively
#   ci/run.sh macos-x64                under Rosetta (arch -x86_64), with the x64 CPython and Node
#   ci/run.sh linux-x64 | linux-arm64  in an ubuntu 22.04 container (pinned by digest; Docker, for example colima)
#   ci/run.sh windows-x64 --azure      the same on a throwaway Azure VM made for this run and deleted after it, pass,
#                                      fail or interrupted (ci/azure-windows.sh; `ci/azure-windows.sh sweep` deletes leftovers)
#   ci/run.sh windows-x64 --host user@host [--key file]   over SSH on a Windows machine (stock Windows with OpenSSH
#                                      server; ci/windows-prepare.ps1 runs there first and installs Git for Windows);
#                                      the job runs detached there (ci/windows-start.ps1), so a dropped connection only
#                                      delays the copy of its results
#   ci/run.sh network                  the corporate-network job (TLS inspection, authenticating proxy), in a container
#   ci/run.sh secrets                  gitleaks over every commit
#   ci/run.sh archives                 the plugin archives, the client tar (its sha256 must equal launcher.json's pin) and
#                                      the install files, with SHA256SUMS (a release build when the tree is stamped)
#   ci/run.sh index                    runtime-<id>.json from the bundles in ci/out/*/bundle (lock_sha256 kept)
#   ci/run.sh summary                  the table of the last results
#   ci/run.sh all [--azure | --host user@host]   everything (Windows from the start, in the background), the table,
#                                      then `record`
#   ci/run.sh record                   copy the table and logs to $THEODORE_CI_LOGS/<date>/plugin-<commit>-<time>/
#                                      (default ~/theodore-ci) and, when every row passed on a clean tree, write the
#                                      stamp green/plugin/<commit> ("checks green at <commit>")
#   ci/run.sh require [commit]         exit 0 only when that commit (default HEAD) has the stamp: release steps call it
#
# Per platform: "shim" is sync_launchers --check and every test suite (on Windows against the real bundle, as the
# workflow did); "runtime" is build_bundle, smoke_test, and the launcher and installer suites against that bundle.
# Each runs with the pinned python-build-standalone CPython of the platform (ci/bootstrap_python.py), the same file
# users get. Outputs: ci/out/<platform>/{shim,runtime}.log and .result (pass, fail or not-run), bundle/ (the
# archives and manifest to publish), and ci/out/summary.txt. Containers mount the repository read-only.

set -u
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
OUT="$ROOT/ci/out"
CACHE="$OUT/cache"
# ubuntu:22.04 (glibc 2.35; the runtime's floor is 2.28): the multi-arch index digest, pulled 2026-11-10.
UBUNTU="ubuntu@sha256:076e7bf82319619f28dd0aa9c02ab34b055ad37100395b4f49a2ffba04fb5e11"
GITLEAKS_VERSION=8.30.1
HOST=
KEY=
AZURE=
KNOWN=
LOGS=${THEODORE_CI_LOGS:-$HOME/theodore-ci}
TARGET=${1:-}
[ $# -gt 0 ] && shift
while [ $# -gt 0 ]; do
  case "$1" in --host) HOST=${2:-}; shift ;; --key) KEY=${2:-}; shift ;; --azure) AZURE=1 ;;
    [0-9a-f]*) [ "$TARGET" = require ] && REQUIRE=$1 || { echo "unknown option $1" >&2; exit 64; } ;; *) echo "unknown option $1" >&2; exit 64 ;; esac
  shift
done

say() { printf 'ci: %s\n' "$*"; }
result() { # name job pass|fail|not-run
  mkdir -p "$OUT/$1"; printf '%s\n' "$3" > "$OUT/$1/$2.result"; say "$1 $2: $3"
}
host_python() { command -v python3 || echo /usr/bin/python3; }
fresh() { # dir: empty it (the variable is never empty: every caller passes a path under $OUT)
  rm -rf "${1:?}"; mkdir -p "${1:?}"
}

# --- one platform on this Mac (natively or under Rosetta) ----------------------------------------------------------
run_mac() { # name platform-key arch-prefix
  name=$1 key=$2 prefix=$3
  dir="$OUT/$name"
  mkdir -p "$dir" "$CACHE"; fresh "$dir/bundle"; fresh "$dir/tmp"; rm -rf "${dir:?}/work"; rm -f "$dir/"*.result "$dir/"*.log
  PY=$("$(host_python)" "$ROOT/ci/bootstrap_python.py" "$key" "$OUT/python-$key" "$CACHE") ||
    { result "$name" shim fail; result "$name" runtime fail; return; }
  say "$name: $($prefix "$PY" -c 'import platform,sys; print(sys.version.split()[0], platform.machine())')"
  ( cd "$ROOT" && export PYTHONDONTWRITEBYTECODE=1 TMPDIR="$dir/tmp" &&
    $prefix "$PY" tools/sync_launchers.py --check && $prefix "$PY" -m unittest discover -s tests -v ) > "$dir/shim.log" 2>&1 \
    && result "$name" shim pass || result "$name" shim fail
  ( cd "$ROOT" && export PYTHONDONTWRITEBYTECODE=1 TMPDIR="$dir/tmp" &&
    $prefix "$PY" runtime/build/build_bundle.py --platform "$key" --out "$dir/bundle" --work "$dir/work" --cache "$CACHE/bundle-$key" &&
    $prefix "$PY" runtime/build/smoke_test.py --bundle "$dir/bundle" --platform "$key" &&
    THEODORE_TEST_BUNDLE="$dir/bundle" $prefix "$PY" -m unittest -v tests.test_launcher tests.test_installer ) > "$dir/runtime.log" 2>&1 \
    && result "$name" runtime pass || result "$name" runtime fail
  rm -rf "${dir:?}/work" "${dir:?}/tmp"
}

# --- one platform in a container -------------------------------------------------------------------------------------
in_container() { # docker-platform name script
  dplat=$1 name=$2 script=$3
  command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 || { say "docker is not running"; return 1; }
  mkdir -p "$OUT/$name" "$CACHE"
  docker run --rm --platform "$dplat" -v "$ROOT:/repo:ro" -v "$OUT/$name:/repo/ci/out/$name" -v "$CACHE:/repo/ci/out/cache" \
    -e CI_NAME="$name" -e PYTHONDONTWRITEBYTECODE=1 -w /repo "$UBUNTU" sh -c "$script"
}

# What the old runners had and the jobs use: a Python to bootstrap the pinned one, curl, openssl, xz, git, and one
# font with fontconfig (skia-canvas measures text with the system's fonts; a desktop Linux always has some).
SETUP='set -e; export DEBIAN_FRONTEND=noninteractive; apt-get update -qq >/dev/null
  apt-get install -y -qq --no-install-recommends python3 curl ca-certificates openssl xz-utils git fontconfig fonts-dejavu-core >/dev/null
  useradd -m tester'

run_linux() { # name docker-platform platform-key
  name=$1 dplat=$2 key=$3
  dir="$OUT/$name"
  mkdir -p "$dir"; rm -rf "${dir:?}/bundle"; rm -f "$dir/"*.result
  in_container "$dplat" "$name" "$SETUP
    chown -R tester /repo/ci/out/$name /repo/ci/out/cache
    su tester -c 'cd /repo; O=ci/out/$name; export TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1
      PY=\$(python3 ci/bootstrap_python.py $key /home/tester/py ci/out/cache) || exit 1
      ( \$PY tools/sync_launchers.py --check && \$PY -m unittest discover -s tests -v ) > \$O/shim.log 2>&1 && echo pass > \$O/shim.result || echo fail > \$O/shim.result
      ( \$PY runtime/build/build_bundle.py --platform $key --out \$O/bundle --work /tmp/work --cache ci/out/cache/bundle-$key &&
        \$PY runtime/build/smoke_test.py --bundle \$O/bundle --platform $key &&
        THEODORE_TEST_BUNDLE=/repo/\$O/bundle \$PY -m unittest -v tests.test_launcher tests.test_installer ) > \$O/runtime.log 2>&1 && echo pass > \$O/runtime.result || echo fail > \$O/runtime.result'" \
    || say "$name: the container did not finish"
  for job in shim runtime; do [ -f "$dir/$job.result" ] || echo not-run > "$dir/$job.result"; say "$name $job: $(cat "$dir/$job.result")"; done
}

# --- the corporate network: a trusted inspecting certificate and an authenticating proxy --------------------------------
# In a linux/arm64 container: tinyproxy stops after its first request under x86-64 emulation on an Apple-silicon
# host, and the job tests the launcher and the client, which are the same on every Linux.
run_network() {
  dir="$OUT/network"; mkdir -p "$dir"; rm -f "$dir/"*.result "$dir/"*.log
  in_container linux/arm64 network "$SETUP
    apt-get install -y -qq --no-install-recommends tinyproxy >/dev/null
    openssl req -x509 -newkey rsa:2048 -nodes -keyout /tmp/trusted-key.pem -out /tmp/trusted-cert.pem -days 2 -subj /CN=127.0.0.1 \
      -addext subjectAltName=IP:127.0.0.1,DNS:localhost -addext basicConstraints=critical,CA:TRUE 2>/dev/null
    chmod 644 /tmp/trusted-key.pem
    cp /tmp/trusted-cert.pem /usr/local/share/ca-certificates/theodore-test.crt && update-ca-certificates >/dev/null
    printf 'Port 18888\nListen 127.0.0.1\nTimeout 600\nBasicAuth tester secret\nAllow 127.0.0.1\n' > /tmp/tinyproxy.conf
    tinyproxy -d -c /tmp/tinyproxy.conf > /tmp/tinyproxy.log 2>&1 &
    sleep 1
    code=\$(curl -s -o /dev/null -w '%{http_code}' -x http://127.0.0.1:18888 http://example.com/ || true)
    echo \"proxy without credentials answers \$code\" > ci/out/network/network.log; test \"\$code\" = 407
    chown -R tester ci/out/network ci/out/cache
    su tester -c 'cd /repo; PY=\$(python3 ci/bootstrap_python.py linux-arm64 /home/tester/py ci/out/cache) &&
      THEODORE_TEST_TRUSTED_CERT=/tmp/trusted-cert.pem:/tmp/trusted-key.pem THEODORE_TEST_AUTH_PROXY=http://127.0.0.1:18888 \
      THEODORE_TEST_AUTH_PROXY_CREDENTIALS=tester:secret \$PY -m unittest -v tests.test_launcher.CorporateNetwork' >> ci/out/network/network.log 2>&1" \
    && result network job pass || result network job fail
  grep -E "^Ran|^OK|^FAILED|proxy without" "$dir/network.log" 2>/dev/null
}

# --- secrets: gitleaks over every commit ---------------------------------------------------------------------------------
run_secrets() {
  dir="$OUT/secrets"; mkdir -p "$dir" "$CACHE"
  case "$(uname -m)" in arm64) asset=darwin_arm64 ;; *) asset=darwin_x64 ;; esac
  tgz="$CACHE/gitleaks_${GITLEAKS_VERSION}_$asset.tar.gz"
  [ -f "$tgz" ] || curl -sSf --proto =https -L -o "$tgz" "https://github.com/gitleaks/gitleaks/releases/download/v$GITLEAKS_VERSION/gitleaks_${GITLEAKS_VERSION}_$asset.tar.gz"
  # The release's own checksum file names the sha256 of every archive; the archive must match it.
  sums="$CACHE/gitleaks_${GITLEAKS_VERSION}_checksums.txt"
  [ -f "$sums" ] || curl -sSf --proto =https -L -o "$sums" "https://github.com/gitleaks/gitleaks/releases/download/v$GITLEAKS_VERSION/gitleaks_${GITLEAKS_VERSION}_checksums.txt"
  want=$(grep " gitleaks_${GITLEAKS_VERSION}_$asset.tar.gz\$" "$sums" | cut -d' ' -f1)
  got=$(shasum -a 256 "$tgz" | cut -d' ' -f1)
  if [ -z "$want" ] || [ "$got" != "$want" ]; then rm -f "$tgz"; say "gitleaks archive sha256 $got does not match the release's checksums"; result secrets scan fail; return; fi
  tar -xzf "$tgz" -C "$dir" gitleaks
  ( cd "$ROOT" && "$dir/gitleaks" git . --no-banner --redact --exit-code 1 ) > "$dir/scan.log" 2>&1 && result secrets scan pass || result secrets scan fail
  tail -n 2 "$dir/scan.log"
}

# --- the combined runtime index --------------------------------------------------------------------------------------------
run_index() {
  dir="$OUT/index"; fresh "$dir"; mkdir -p "$dir/bundles"
  have=
  for pair in macos-arm64:darwin-arm64 macos-x64:darwin-x64 windows-x64:win32-x64 linux-x64:linux-x64 linux-arm64:linux-arm64; do
    name=${pair%%:*} key=${pair#*:}
    if [ "$(cat "$OUT/$name/runtime.result" 2>/dev/null | tr -d '\r')" = pass ] && ls "$OUT/$name/bundle/"*"$key.manifest.json" >/dev/null 2>&1; then
      ln -s "$OUT/$name/bundle" "$dir/bundles/$key"; have="$have,$key"
    fi
  done
  [ -n "$have" ] || { say "index: no passing bundle"; result index build not-run; return; }
  "$(host_python)" "$ROOT/tools/runtime_index.py" "$dir/bundles" --out "$dir" --platforms "${have#,}" > "$dir/index.log" 2>&1 \
    && result index build pass || result index build fail
  case "$have" in *darwin-arm64*darwin-x64*win32-x64*linux-x64*linux-arm64*) ;; *) say "index: only ${have#,}: not all five platforms, not publishable" ;; esac
  cat "$dir/index.log"
}

# --- Windows over SSH --------------------------------------------------------------------------------------------------------
run_windows() {
  name=windows-x64; dir="$OUT/$name"; fresh "$dir"
  [ -n "$HOST" ] || { say "windows-x64 needs --host user@host"; result "$name" shim not-run; result "$name" runtime not-run; return; }
  pins=$("$(host_python)" -c 'import json,sys; p=json.load(open(sys.argv[1]))["python"]; a=p["assets"]["win32-x64"]; print(p["pbs_release"], a["file"], a["sha256"])' \
    "$ROOT/runtime/locks/versions.json")
  ( cd "$ROOT" && git ls-files -z | COPYFILE_DISABLE=1 xargs -0 tar --no-mac-metadata --no-xattrs -cf "$dir/repo.tar" ) || { result "$name" shim fail; result "$name" runtime fail; return; }
  SSHO="-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=60 ${KNOWN:+-o UserKnownHostsFile=$KNOWN} ${KEY:+-i $KEY}"
  # Retried: a connection can drop (this Mac may switch uplinks); the job itself runs detached on the machine.
  retry() { n=0; until "$@"; do n=$((n + 1)); [ $n -lt 5 ] || return 1; sleep 20; done; }
  if retry ssh $SSHO -o ConnectTimeout=20 "$HOST" 'powershell -NoProfile -Command "Remove-Item -Recurse -Force C:\theodore-ci -ErrorAction SilentlyContinue; New-Item -ItemType Directory C:\theodore-ci\repo | Out-Null"' > "$dir/remote.log" 2>&1 &&
      retry scp -q $SSHO -o ConnectTimeout=20 "$dir/repo.tar" "$HOST:C:/theodore-ci/repo.tar" >> "$dir/remote.log" 2>&1 &&
      retry ssh $SSHO -o ConnectTimeout=20 "$HOST" "(if not exist C:\\theodore-ci\\started C:\\Windows\\System32\\tar.exe -xf C:\\theodore-ci\\repo.tar -C C:\\theodore-ci\\repo) & powershell -NoProfile -ExecutionPolicy Bypass -File C:\\theodore-ci\\repo\\ci\\windows-start.ps1 $pins" >> "$dir/remote.log" 2>&1; then
    # windows-run.ps1 writes runtime.result last. A failed poll (a dropped connection) is only retried. 3 h at most.
    wstart=$(date +%s)
    until ssh $SSHO -o ConnectTimeout=20 "$HOST" 'if exist C:\theodore-ci\out\runtime.result echo done' 2>/dev/null | grep -q done; do
      [ $(( $(date +%s) - wstart )) -lt 10800 ] || { say "windows-x64: no result after 3 h"; break; }
      sleep 30
    done
  fi
  retry scp -q -r $SSHO -o ConnectTimeout=20 "$HOST:C:/theodore-ci/out/*" "$dir/" 2>>"$dir/remote.log"
  for job in shim runtime; do [ -f "$dir/$job.result" ] || echo not-run > "$dir/$job.result"; say "$name $job: $(tr -d '\r' < "$dir/$job.result")"; done
  rm -f "$dir/repo.tar"
}

# --- Windows on a throwaway Azure VM ---------------------------------------------------------------------------------------
run_windows_azure() {
  state="$OUT/azure-windows"; dir="$OUT/windows-x64"
  mkdir -p "$dir"
  [ ! -f "$state/group" ] || { say "$state still names a machine: ci/azure-windows.sh down $state"; result windows-x64 shim not-run; result windows-x64 runtime not-run; return; }
  # The VM is deleted on every way out: the end of this function, a failure, or a signal (the signal handler exits,
  # which runs the EXIT trap). The trap carries the paths by value.
  trap "bash '$ROOT/ci/azure-windows.sh' down '$state' >> '$dir/azure.log' 2>&1" EXIT
  trap 'exit 130' INT TERM HUP
  started=$(date +%s)
  if CI_AZURE_KEY="${KEY:-$HOME/.ssh/theodore_ci}" bash "$ROOT/ci/azure-windows.sh" up "$state" > "$dir/azure.log" 2>&1; then
    HOST=$(cat "$state/host") KEY=${KEY:-$HOME/.ssh/theodore_ci} KNOWN="$state/known_hosts"
    say "windows-x64: $(cat "$state/size" 2>/dev/null) ready after $(( $(date +%s) - started )) s"
    cp "$dir/azure.log" "$OUT/azure-up.log"
    run_windows
    cat "$OUT/azure-up.log" "$dir/azure.log" > "$OUT/azure.tmp" 2>/dev/null; mv "$OUT/azure.tmp" "$dir/azure.log"; rm -f "$OUT/azure-up.log"
  else
    say "windows-x64: the Azure machine could not be made (ci/out/windows-x64/azure.log)"
    result windows-x64 shim not-run; result windows-x64 runtime not-run
  fi
  bash "$ROOT/ci/azure-windows.sh" down "$state" >> "$dir/azure.log" 2>&1
  trap - EXIT INT TERM HUP
  echo "$(( $(date +%s) - started ))" > "$dir/azure.seconds"
  say "windows-x64: machine deleted; $(( ($(date +%s) - started) / 60 )) min from create to delete"
}

# --- the archives ----------------------------------------------------------------------------------------------------------
run_archives() {
  dir="$OUT/archives"; fresh "$dir"; mkdir -p "$CACHE"
  case "$(uname -m)" in arm64) key=darwin-arm64 ;; *) key=darwin-x64 ;; esac
  PY=$("$(host_python)" "$ROOT/ci/bootstrap_python.py" "$key" "$OUT/python-$key" "$CACHE") || { result archives build fail; return; }
  ( cd "$ROOT" && export PYTHONDONTWRITEBYTECODE=1 &&
    if "$PY" -c 'import sys; sys.path.insert(0, "tools"); import build_plugin_archives as b; sys.exit(1 if b.placeholder_keys(b.ROOT) else 0)'; then
      rel=--release; echo "stamped tree: release build"; else rel=; echo "placeholder key: development build (a release build would refuse it)"; fi &&
    "$PY" tools/build_plugin_archives.py --out "$dir/plugin" $rel &&
    "$PY" runtime/build/build_client.py --out "$dir/client" &&
    mkdir -p "$dir/install" && cp install/install.md install/install.sh install/install.cmd "$dir/install/" &&
    want=$("$PY" -c 'import json; print(json.load(open("plugins/theodore/skills/pptx/scripts/launcher.json"))["stage1_client_sha256"])') &&
    got=$(shasum -a 256 "$dir/client/"client-*.tar | cut -d' ' -f1) &&
    { [ "$got" = "$want" ] || { echo "client tar sha256 $got is not launcher.json's stage1_client_sha256 $want"; false; }; } &&
    echo "client tar sha256 equals launcher.json's pin" &&
    ( cd "$dir" && shasum -a 256 plugin/* client/* install/* > SHA256SUMS ) ) > "$dir/archives.log" 2>&1 \
    && result archives build pass || result archives build fail
  tail -n 3 "$dir/archives.log"
}

summary() {
  {
    printf '%-12s %-8s %-8s %-62s %s\n' platform shim runtime "bundle manifest" sha256
    for name in macos-arm64 macos-x64 windows-x64 linux-x64 linux-arm64; do
      m=$(ls "$OUT/$name/bundle/"*.manifest.json 2>/dev/null | head -n 1)
      printf '%-12s %-8s %-8s %-62s %s\n' "$name" "$(tr -d '\r' 2>/dev/null < "$OUT/$name/shim.result" || echo not-run)" \
        "$(tr -d '\r' 2>/dev/null < "$OUT/$name/runtime.result" || echo not-run)" "${m#$ROOT/}" "$( [ -n "$m" ] && shasum -a 256 "$m" | cut -d' ' -f1)"
    done
    for job in network/job secrets/scan archives/build index/build; do
      printf '%-12s %s\n' "${job%/*}" "$(cat "$OUT/$job.result" 2>/dev/null || echo not-run)"
    done
    ls "$OUT/index/"runtime-*.json 2>/dev/null | sed "s|$ROOT/||"
  } | tee "$OUT/summary.txt"
}

# --- the record: a dated folder outside the repository, and the stamp a release step requires ---------------------------
commit() { git -C "$ROOT" rev-parse HEAD; }
record() {
  sha=$(commit); short=$(printf %.12s "$sha")
  dest="$LOGS/$(date +%Y-%m-%d)/plugin-$short-$(date +%H%M%S)"; mkdir -p "$dest"
  for name in macos-arm64 macos-x64 windows-x64 linux-x64 linux-arm64 network secrets archives index; do
    [ -d "$OUT/$name" ] || continue
    mkdir -p "$dest/$name"; find "$OUT/$name" -maxdepth 1 -type f \( -name '*.log' -o -name '*.result' -o -name '*.seconds' -o -name SHA256SUMS -o -name 'runtime-*.json' \) -exec cp {} "$dest/$name/" \;
  done
  cp "$OUT/summary.txt" "$dest/table.txt"
  # Every row must pass. index is only required when all five bundles were built (it is the publish input).
  bad=$(grep -vE '^platform ' "$OUT/summary.txt" | grep -vE '^ci/out/' | awk '$1 != "index" { for (i = 2; i <= 3 && i <= NF; i++) if ($i == "fail" || $i == "not-run") print $1 } $1 == "index" && $2 == "fail" { print $1 }' | sort -u | tr '\n' ' ')
  dirty=$(git -C "$ROOT" status --porcelain --untracked-files=no)
  if [ -z "$bad" ] && [ -z "$dirty" ]; then
    mkdir -p "$LOGS/green/plugin"
    printf 'checks green at %s\n%s\n%s\n' "$sha" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$dest" > "$LOGS/green/plugin/$sha"
    line="checks green at $sha"
  elif [ -n "$dirty" ]; then line="checks NOT green at $sha: the tree has uncommitted changes (no stamp)"
  else line="checks NOT green at $sha: ${bad% }"; fi
  echo "$line" | tee "$dest/result.txt"
  say "record: $dest"
}
require() {
  sha=$(git -C "$ROOT" rev-parse "${REQUIRE:-HEAD}") || exit 2
  if [ -f "$LOGS/green/plugin/$sha" ]; then head -n 1 "$LOGS/green/plugin/$sha"; exit 0; fi
  echo "no green check for $sha: run ci/run.sh all --azure at that commit (stamps in $LOGS/green/plugin/)" >&2; exit 1
}

mkdir -p "$OUT"
case "$TARGET" in
  macos-arm64) run_mac macos-arm64 darwin-arm64 "" ;;
  macos-x64) run_mac macos-x64 darwin-x64 "arch -x86_64" ;;
  linux-x64) run_linux linux-x64 linux/amd64 linux-x64 ;;
  linux-arm64) run_linux linux-arm64 linux/arm64 linux-arm64 ;;
  windows-x64) if [ -n "$AZURE" ]; then run_windows_azure; else run_windows; fi ;;
  archives) run_archives ;;
  record) record ;;
  require) require ;;
  network) run_network ;;
  secrets) run_secrets ;;
  index) run_index ;;
  summary) summary ;;
  all)
    # Windows runs remotely from the start (it is the longest), the local jobs one at a time beside it.
    wpid=
    if [ -n "$AZURE$HOST" ]; then
      mkdir -p "$OUT/windows-x64"
      sh "$0" windows-x64 ${AZURE:+--azure} ${HOST:+--host "$HOST"} ${KEY:+--key "$KEY"} > "$OUT/windows-x64.run.log" 2>&1 & wpid=$!
      trap 'kill $wpid 2>/dev/null; wait $wpid; exit 130' INT TERM HUP
      say "windows-x64 started (ci/out/windows-x64.run.log)"
    else
      result windows-x64 shim not-run; result windows-x64 runtime not-run
    fi
    run_secrets
    run_mac macos-arm64 darwin-arm64 ""
    run_mac macos-x64 darwin-x64 "arch -x86_64"
    run_linux linux-x64 linux/amd64 linux-x64
    run_linux linux-arm64 linux/arm64 linux-arm64
    run_network
    run_archives
    if [ -n "$wpid" ]; then say "waiting for windows-x64"; wait "$wpid"; trap - INT TERM HUP; mv "$OUT/windows-x64.run.log" "$OUT/windows-x64/run.log" 2>/dev/null; fi
    run_index
    summary
    record ;;
  *) sed -n '2,20p' "$0"; exit 64 ;;
esac
