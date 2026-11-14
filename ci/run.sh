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
#   ci/run.sh stamp                    record this tree's results per target and write the stamp when every target has
#                                      a green record at this commit (a target that failed for an outside reason is
#                                      rerun alone; a test failure needs a new commit)
#   ci/run.sh plan                     what HEAD needs, from what changed since the nearest stamped ancestor, by the table
#                                      CHANGE_TABLE below (inherit, text, windows, client, runtime), and the targets
#   ci/run.sh selected [--azure]       run only those targets, then a stamp that names the ancestor the other rows are
#                                      inherited from (a runtime change runs `all`)
#   ci/run.sh lock-status              the shared lock's holder and its queue in order (lock-waits: today's waits and holds)
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
# Verified downloads are kept by their pinned sha256 for every job (bootstrap_python.py, build_bundle.py): a rerun never
# fetches them again. The containers see the same folder at /repo/ci/out/cache.
export THEODORE_FETCH_STORE="$CACHE/by-sha256"
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
  [ -n "${CI_SHIM_ONLY:-}" ] && { result "$name" runtime not-run; rm -rf "${dir:?}/work" "${dir:?}/tmp"; return; }
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
  docker run --rm --label "theodore-ci=$$" --platform "$dplat" -v "$ROOT:/repo:ro" -v "$OUT/$name:/repo/ci/out/$name" -v "$CACHE:/repo/ci/out/cache" -e THEODORE_FETCH_STORE=/repo/ci/out/cache/by-sha256 \
    -e CI_NAME="$name" -e PYTHONDONTWRITEBYTECODE=1 -w /repo "$UBUNTU" sh -c "$script"
}

# What the old runners had and the jobs use: a Python to bootstrap the pinned one, curl, openssl, xz, git, and one
# font with fontconfig (skia-canvas measures text with the system's fonts; a desktop Linux always has some).
SETUP='set -e; export DEBIAN_FRONTEND=noninteractive; echo "Acquire::Retries \"5\";" > /etc/apt/apt.conf.d/80retries; apt-get update -qq >/dev/null
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
      $( [ -n "${CI_SHIM_ONLY:-}" ] && echo 'exit 0;' )  # shim only: the runner records runtime as not-run
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
  [ -f "$tgz" ] || curl -sSf --proto =https -L --retry 5 --retry-all-errors --retry-delay 2 -C - -o "$tgz" "https://github.com/gitleaks/gitleaks/releases/download/v$GITLEAKS_VERSION/gitleaks_${GITLEAKS_VERSION}_$asset.tar.gz"
  # The release's own checksum file names the sha256 of every archive; the archive must match it.
  sums="$CACHE/gitleaks_${GITLEAKS_VERSION}_checksums.txt"
  [ -f "$sums" ] || curl -sSf --proto =https -L --retry 5 --retry-all-errors --retry-delay 2 -C - -o "$sums" "https://github.com/gitleaks/gitleaks/releases/download/v$GITLEAKS_VERSION/gitleaks_${GITLEAKS_VERSION}_checksums.txt"
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
  name=windows-x64; dir="$OUT/$name"; fresh "$dir"; commit > "$dir/commit"
  [ -n "$HOST" ] || { say "windows-x64 needs --host user@host"; result "$name" shim not-run; result "$name" runtime not-run; return; }
  pins=$("$(host_python)" -c 'import json,sys; p=json.load(open(sys.argv[1]))["python"]; a=p["assets"]["win32-x64"]; print(p["pbs_release"], a["file"], a["sha256"])' \
    "$ROOT/runtime/locks/versions.json")
  ( cd "$ROOT" && git ls-files -z | COPYFILE_DISABLE=1 xargs -0 tar --no-mac-metadata --no-xattrs -cf "$dir/repo.tar" ) || { result "$name" shim fail; result "$name" runtime fail; return; }
  SSHO="-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=60 ${KNOWN:+-o UserKnownHostsFile=$KNOWN} ${KEY:+-i $KEY}"
  # Retried: a connection can drop (this Mac may switch uplinks); the job itself runs detached on the machine.
  retry() { n=0; until "$@"; do n=$((n + 1)); [ $n -lt 5 ] || return 1; [ -z "${ADMIT:-}" ] || $ADMIT >/dev/null 2>&1; sleep 20; done; }
  # On a throwaway Azure machine the pinned CPython and the bundle cache live in C:\theodore-ci-cache, which outlives
  # the run (a prepared image keeps it). The suites run one after another unless CI_WINDOWS_PARALLEL=1.
  keep=; [ -z "$AZURE" ] || keep='; New-Item -ItemType Directory -Force C:\theodore-ci-cache | Out-Null'
  # The suites side by side (CI_WINDOWS_PARALLEL=1) failed the first-run tests' 60 s stage-2 wait twice under load on
  # 8 vCPU (2026-11-13): one after another stays the default until that wait is fixed.
  mode=serial; [ "${CI_WINDOWS_PARALLEL:-}" != 1 ] || mode=
  # The poll's limit: the last run that passed here, plus half and 15 min (CI_WINDOWS_TIMEOUT overrides; 3 h without one).
  limit=${CI_WINDOWS_TIMEOUT:-}
  if [ -z "$limit" ]; then
    last=$(cat "$LOGS/history/plugin-windows.seconds" 2>/dev/null)
    case "$last" in ''|*[!0-9]*) limit=10800 ;; *) limit=$(( last * 3 / 2 + 900 )); [ "$limit" -ge 1800 ] || limit=1800 ;; esac
  fi
  say "windows-x64: poll limit $limit s"
  if retry ssh $SSHO -o ConnectTimeout=20 "$HOST" "powershell -NoProfile -Command \"Remove-Item -Recurse -Force C:\\theodore-ci -ErrorAction SilentlyContinue; New-Item -ItemType Directory C:\\theodore-ci\\repo | Out-Null$keep\"" > "$dir/remote.log" 2>&1 &&
      retry scp -q $SSHO -o ConnectTimeout=20 "$dir/repo.tar" "$HOST:C:/theodore-ci/repo.tar" >> "$dir/remote.log" 2>&1 &&
      retry ssh $SSHO -o ConnectTimeout=20 "$HOST" "(if not exist C:\\theodore-ci\\started C:\\Windows\\System32\\tar.exe -xf C:\\theodore-ci\\repo.tar -C C:\\theodore-ci\\repo) & powershell -NoProfile -ExecutionPolicy Bypass -File C:\\theodore-ci\\repo\\ci\\windows-start.ps1 $pins $mode" >> "$dir/remote.log" 2>&1; then
    # windows-run.ps1 writes runtime.result last. A failed poll (a dropped connection) is only retried, up to the limit.
    wstart=$(date +%s)
    until ssh $SSHO -o ConnectTimeout=20 "$HOST" 'if exist C:\theodore-ci\out\runtime.result echo done' 2>/dev/null | grep -q done; do
      [ $(( $(date +%s) - wstart )) -lt "$limit" ] || { say "windows-x64: no result after $limit s"; break; }
      # Not answering: on Azure, this Mac may have changed address; admit the new one (the job runs on regardless).
      ssh $SSHO -o ConnectTimeout=20 "$HOST" 'echo up' >/dev/null 2>&1 || { [ -z "${ADMIT:-}" ] || $ADMIT >> "$dir/remote.log" 2>&1; }
      sleep 30
    done
  fi
  retry scp -q -r $SSHO -o ConnectTimeout=20 "$HOST:C:/theodore-ci/out/*" "$dir/" 2>>"$dir/remote.log"
  for job in shim runtime; do [ -f "$dir/$job.result" ] || echo not-run > "$dir/$job.result"; say "$name $job: $(tr -d '\r' < "$dir/$job.result")"; done
  [ -z "${wstart:-}" ] || echo "$(( $(date +%s) - wstart ))" > "$dir/job.seconds"
  if [ "$(tr -d '\r' < "$dir/shim.result")" = pass ] && [ "$(tr -d '\r' < "$dir/runtime.result")" = pass ] && [ -n "${wstart:-}" ]; then
    mkdir -p "$LOGS/history" && echo "$(( $(date +%s) - wstart ))" > "$LOGS/history/plugin-windows.seconds"
  fi
  rm -f "$dir/repo.tar"
}

# --- Windows on a throwaway Azure VM ---------------------------------------------------------------------------------------
run_windows_azure() {
  state="$OUT/azure-windows"; dir="$OUT/windows-x64"
  mkdir -p "$dir"
  if [ -f "$state/group" ] && [ "$(az group exists -n "$(cat "$state/group")" -o tsv 2>/dev/null)" = false ]; then
    say "clearing $state: its group $(cat "$state/group") no longer exists"; rm -rf "${state:?}"
  fi
  [ ! -f "$state/group" ] || { say "$state still names a machine: ci/azure-windows.sh down $state"; result windows-x64 shim not-run; result windows-x64 runtime not-run; return; }
  # The VM is deleted on every way out: the end of this function, a failure, or a signal (the signal handler exits,
  # which runs the EXIT trap). The trap carries the paths by value.
  trap "bash '$ROOT/ci/azure-windows.sh' down '$state' >> '$dir/azure.log' 2>&1" EXIT
  trap 'exit 130' INT TERM HUP
  # Started by ci/run.sh all: end (and delete the machine) as soon as that run is gone.
  if [ -n "${CI_PARENT:-}" ]; then me=$$; ( while kill -0 "$CI_PARENT" 2>/dev/null; do sleep 10; done; kill -TERM "$me" 2>/dev/null ) & fi
  # Groups an earlier run could not delete (its Mac lost the network): past their deadline, deleted now.
  bash "$ROOT/ci/azure-windows.sh" sweep $(( ${CI_AZURE_DEADLINE_HOURS:-4} + 1 )) >> "$dir/azure.log" 2>&1
  started=$(date +%s)
  if CI_AZURE_KEY="${KEY:-$HOME/.ssh/theodore_ci}" bash "$ROOT/ci/azure-windows.sh" up "$state" > "$dir/azure.log" 2>&1; then
    HOST=$(cat "$state/host") KEY=${KEY:-$HOME/.ssh/theodore_ci} KNOWN="$state/known_hosts"
    ADMIT="bash $ROOT/ci/azure-windows.sh admit $state"
    say "windows-x64: $(cat "$state/size" 2>/dev/null) ready after $(( $(date +%s) - started )) s"
    cp "$dir/azure.log" "$OUT/azure-up.log"
    run_windows
    # CI_AZURE_CAPTURE=1: a run that passed makes this machine the next version of the prepared image.
    if [ "${CI_AZURE_CAPTURE:-}" = 1 ]; then
      if [ "$(tr -d '\r' < "$dir/shim.result")" = pass ] && [ "$(tr -d '\r' < "$dir/runtime.result")" = pass ]; then
        ssh $SSHO -o ConnectTimeout=20 "$HOST" 'powershell -NoProfile -Command "Remove-Item -Recurse -Force C:\theodore-ci -ErrorAction SilentlyContinue"' >> "$dir/remote.log" 2>&1
        bash "$ROOT/ci/azure-windows.sh" capture "$state" >> "$OUT/azure-up.log" 2>&1 && say "windows-x64: image captured" || say "windows-x64: image capture failed"
      else say "windows-x64: not every job passed: no image captured"; fi
    fi
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
  dir="$OUT/archives"; fresh "$dir"; commit > "$dir/commit"; mkdir -p "$CACHE"
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
  note_parts
  if [ -n "$dirty" ]; then line="checks NOT green at $sha: the tree has uncommitted changes (no stamp)"
  elif stamp_from_parts; then line="checks green at $sha"
  else line="checks NOT green at $sha: ${bad:-a target without a green record at this commit} (rerun a target that failed for an outside reason alone: ci/run.sh <target>)"; fi
  echo "$line" | tee "$dest/result.txt"
  say "record: $dest"
}
# The stamp is assembled from per-target records at one commit. Each job that passes on a clean tree leaves a green
# record of its own ($LOGS/green/plugin-parts/<sha>/<target>-<job>: its time and a copy of its log), so a target that
# failed for a reason outside the code (a network reset, a VM that did not come up) is rerun alone with
# `ci/run.sh <target>` at the same commit. A job that failed with a test failure in its log is marked, and no later
# pass at that commit counts: a test failure needs its fix and a new commit. The stamp is written when every
# required job has a green record and none is marked.
REQUIRED_PARTS="secrets-scan macos-arm64-shim macos-arm64-runtime macos-x64-shim macos-x64-runtime windows-x64-shim windows-x64-runtime linux-x64-shim linux-x64-runtime linux-arm64-shim linux-arm64-runtime network-job archives-build"
note_parts() { # record this run's results as per-target records for the commit
  sha=$(commit); dirty=$(git -C "$ROOT" status --porcelain --untracked-files=no)
  [ -z "$dirty" ] || { say "parts: the tree has uncommitted changes; no records"; return 0; }
  parts="$LOGS/green/plugin-parts/$sha"; mkdir -p "$parts"
  for part in $REQUIRED_PARTS; do
    job=${part##*-}; name=${part%-*}
    [ "$(cat "$OUT/$name/commit" 2>/dev/null)" = "$sha" ] || continue       # a result of another commit never counts
    res=$(tr -d '\r' 2>/dev/null < "$OUT/$name/$job.result")
    log="$OUT/$name/$job.log"
    case "$res" in
      pass) [ -f "$parts/$part" ] && [ "$OUT/$name/$job.result" -ot "$parts/$part" ] && continue
            { printf 'green at %s\n%s\n' "$sha" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"; cat "$OUT/$name/"*.log 2>/dev/null; true; } > "$parts/$part.tmp" && mv "$parts/$part.tmp" "$parts/$part" ;;
      # Test modules that could not import only because the job's runtime bundle was not built (support.py finds no
      # bundle manifest) are an outside failure, not a test failure.
      fail) if [ -f "$log" ] && grep -E '^(FAIL|ERROR): test' "$log" | { if grep -q 'REAL_BUNDLE).glob' "$log"; then grep -v '_FailedTest'; else cat; fi; } | grep -q .; then
              { printf 'test failure at %s\n%s\n' "$sha" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"; grep -E '^(FAIL|ERROR): ' "$log"; } > "$parts/$part.testfail"
            fi ;;
    esac
  done
}
stamp_from_parts() {
  sha=$(commit); parts="$LOGS/green/plugin-parts/$sha"; missing=; marked=
  for part in $REQUIRED_PARTS; do
    [ -f "$parts/$part.testfail" ] && marked="$marked $part"
    [ -f "$parts/$part" ] || missing="$missing $part"
  done
  if [ -n "$marked" ]; then say "no stamp: test failures at this commit (${marked# }): fix them in a new commit"; return 1; fi
  if [ -n "$missing" ]; then say "no stamp yet: no green record at this commit for${missing}"; return 1; fi
  mkdir -p "$LOGS/green/plugin"
  printf 'checks green at %s\n%s\n%s\n' "$sha" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$parts" > "$LOGS/green/plugin/$sha"
  say "checks green at $sha (every target has a green record: $parts)"
}
require() {
  sha=$(git -C "$ROOT" rev-parse "${REQUIRE:-HEAD}") || exit 2
  if [ -f "$LOGS/green/plugin/$sha" ]; then head -n 1 "$LOGS/green/plugin/$sha"; exit 0; fi
  echo "no green check for $sha: run ci/run.sh all --azure at that commit (stamps in $LOGS/green/plugin/)" >&2; exit 1
}

# --- which checks a commit needs: one table ---------------------------------------------------------------------------
# Each changed file takes the class of the first row its path matches; the commit needs the highest class of its files.
#   inherit  nothing that ships or that a test reads: secrets only
#   text     the skill stubs' and the plugin's text: secrets, archives, macos-arm64 shim (the stub-versus-engine test)
#   windows  Windows-only files: secrets, archives, windows-x64
#   client   the client, the launchers, the install files, the build tools or the tests: secrets, archives, network and
#            the shim job on every platform (bundles untouched, so no runtime job; windows-x64 runs whole)
#   runtime  the runtime recipe, its locks or anything else: everything (`all`)
CHANGE_TABLE='
.github/*|inherit
README.md|inherit
SECURITY.md|inherit
CHANGELOG.md|inherit
THIRD-PARTY-NOTICES.md|inherit
ci/run.sh|inherit
ci/lock.sh|inherit
plugins/*/skills/*/SKILL.md|text
plugins/*/README.md|text
ci/windows-*.ps1|windows
ci/azure-*.sh|windows
plugins/*/scripts/*.cmd|windows
install/install.cmd|windows
client/truststore/_windows.py|windows
runtime/build/vc_redist.py|windows
client/*|client
plugins/*|client
install/*|client
tests/*|client
tools/sync_launchers.py|client
tools/build_plugin_archives.py|client
tools/snapshot_*|client
*|runtime
'
CLASS_RANK="inherit text windows client runtime"
class_of() {
  printf '%s\n' "$CHANGE_TABLE" | while IFS= read -r row; do
    [ -n "$row" ] || continue
    pattern=${row%|*}
    # shellcheck disable=SC2254
    case "$1" in $pattern) echo "${row##*|}"; break ;; esac
  done
}
rank_of() { i=0; for c in $CLASS_RANK; do [ "$c" = "$1" ] && { echo $i; return; }; i=$((i + 1)); done; echo 99; }
stamped_ancestor() {
  for c in $(git -C "$ROOT" rev-list --max-count=300 HEAD); do
    [ -f "$LOGS/green/plugin/$c" ] && { echo "$c"; return 0; }
  done
  return 1
}
targets_of() { # class
  case "$1" in
    inherit) echo "secrets" ;;
    text) echo "secrets archives macos-arm64:shim" ;;
    windows) echo "secrets archives windows-x64" ;;
    client) echo "secrets archives network macos-arm64:shim macos-x64:shim linux-x64:shim linux-arm64:shim windows-x64" ;;
    *) echo "all" ;;
  esac
}
plan() { # sets PLAN_CLASS PLAN_FROM PLAN_FILE; prints the verdict
  PLAN_CLASS=runtime PLAN_FROM= PLAN_FILE=
  PLAN_FROM=$(stamped_ancestor) || { echo "runtime: no stamped ancestor within 300 commits: all"; return 0; }
  if [ "$PLAN_FROM" = "$(commit)" ]; then PLAN_CLASS=green; echo "green: HEAD is stamped"; return 0; fi
  best=inherit bestfile= n=0
  for f in $(git -C "$ROOT" diff --name-only --no-renames "$PLAN_FROM" HEAD); do
    n=$((n + 1)); c=$(class_of "$f")
    if [ "$(rank_of "$c")" -gt "$(rank_of "$best")" ]; then best=$c bestfile=$f; fi
  done
  PLAN_CLASS=$best PLAN_FILE=$bestfile
  echo "$best: ${bestfile:-only files no check reads} ($n files changed since the stamped $(printf %.12s "$PLAN_FROM")): $(targets_of "$best")"
}
record_selected() { # after `selected`: the targets it ran must pass; the stamp names the ancestor for the rest
  sha=$(commit); short=$(printf %.12s "$sha")
  dest="$LOGS/$(date +%Y-%m-%d)/plugin-$short-selected-$(date +%H%M%S)"; mkdir -p "$dest"
  cp "$OUT/summary.txt" "$dest/table.txt" 2>/dev/null
  bad=
  for t in $(targets_of "$PLAN_CLASS"); do
    case "$t" in
      *:shim) r=$(cat "$OUT/${t%:shim}/shim.result" 2>/dev/null) ;;
      windows-x64) r=pass; for j in shim runtime; do [ "$(tr -d '\r' < "$OUT/windows-x64/$j.result" 2>/dev/null)" = pass ] || r=fail; done ;;
      network) r=$(cat "$OUT/network/job.result" 2>/dev/null) ;;
      secrets) r=$(cat "$OUT/secrets/scan.result" 2>/dev/null) ;;
      archives) r=$(cat "$OUT/archives/build.result" 2>/dev/null) ;;
    esac
    [ "$r" = pass ] || bad="$bad$t "
  done
  dirty=$(git -C "$ROOT" status --porcelain --untracked-files=no)
  if [ -z "$bad" ] && [ -z "$dirty" ]; then
    mkdir -p "$LOGS/green/plugin"
    printf 'checks green at %s\n%s\n%s\ninherited from %s for every row but: %s (plan %s, decided by %s)\n' "$sha" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      "$dest" "$PLAN_FROM" "$(targets_of "$PLAN_CLASS")" "$PLAN_CLASS" "${PLAN_FILE:-none}" > "$LOGS/green/plugin/$sha"
    line="checks green at $sha (plan $PLAN_CLASS; the rest inherited from $(printf %.12s "$PLAN_FROM"))"
  elif [ -n "$dirty" ]; then line="checks NOT green at $sha: the tree has uncommitted changes (no stamp)"
  else line="checks NOT green at $sha: ${bad% }"; fi
  echo "$line" | tee "$dest/result.txt"
}

# --- guards: one matrix run at a time on this Mac, a load ceiling, a hard deadline per job ------------------------------
# The lock is shared by the three repositories' runners ($THEODORE_CI_LOGS/locks/matrix); a lock whose process is gone
# is taken over. Before each heavy local job the runner waits until the 1- and 5-minute load averages are under
# CI_MAX_LOAD (default 8). Each local job runs in its own process group under a hard deadline, and a watchdog kills it,
# with its containers (label theodore-ci=<runner pid>), on the deadline or as soon as this runner is gone, however it died.
# The shared lock is a fair queue (ci/lock.sh, the same file in the three repositories): the oldest waiter of the
# highest priority goes next; CI_LOCK_PRIORITY=release (a release check), normal (default) or low (rehearsals,
# measurements). `ci/run.sh lock-status` shows the holder and the waiters in order.
. "$ROOT/ci/lock.sh"
acquire_lock() { ci_lock_acquire "$1"; }
release_lock() { ci_lock_release; }
wait_load() {
  max=${CI_MAX_LOAD:-8}; said=
  while :; do
    set -- $(sysctl -n vm.loadavg 2>/dev/null | tr -d '{}') 0 0
    [ "${1%.*}" -lt "$max" ] && [ "${2%.*}" -lt "$max" ] && return 0
    [ -n "$said" ] || say "load $1 $2, limit $max: waiting"; said=1; sleep 60
  done
}
kill_containers() { docker ps -q --filter "label=theodore-ci=$$" 2>/dev/null | xargs docker kill >/dev/null 2>&1; }
limited() { # seconds label command...: the command in its own process group, killed on the deadline or when this runner dies
  secs=$1 label=$2; shift 2
  mkdir -p "$OUT/$label"; commit > "$OUT/$label/commit"        # its results count for this commit only
  set -m; "$@" & cpid=$!; set +m
  runner=$$
  ( n=0
    while kill -0 "$cpid" 2>/dev/null; do
      if ! kill -0 "$runner" 2>/dev/null || [ "$n" -ge "$secs" ]; then
        [ "$n" -ge "$secs" ] && echo "ci: $label: killed at its hard deadline ($secs s)" >&2
        kill -TERM -"$cpid" 2>/dev/null || kill -TERM "$cpid" 2>/dev/null
        docker ps -q --filter "label=theodore-ci=$runner" 2>/dev/null | xargs docker kill >/dev/null 2>&1
        sleep 10; kill -KILL -"$cpid" 2>/dev/null; break
      fi
      sleep 5; n=$((n + 5))
    done ) &
  wd=$!
  wait "$cpid"; rc=$?
  kill "$wd" 2>/dev/null; wait "$wd" 2>/dev/null
  return $rc
}
heavy() { # seconds label command...: wait for the load, then run it under its deadline
  wait_load; limited "$@"
}

mkdir -p "$OUT"
cleanup() { [ -n "${wpid:-}" ] && kill "$wpid" 2>/dev/null; kill_containers; release_lock; }
trap 'cleanup' EXIT
trap 'exit 130' INT TERM HUP
case "$TARGET" in
  macos-arm64|macos-x64|linux-x64|linux-arm64|network|archives|all) acquire_lock "plugin $TARGET at $(git -C "$ROOT" rev-parse --short HEAD)" ;;
esac
# `selected` takes the lock itself, after its Windows job has started: that job runs on its own machine, and waiting in
# the queue first only delayed the longest job of the run.
case "$TARGET" in
  macos-arm64) heavy 5400 macos-arm64 run_mac macos-arm64 darwin-arm64 "" ;;
  macos-x64) heavy 5400 macos-x64 run_mac macos-x64 darwin-x64 "arch -x86_64" ;;
  linux-x64) heavy 5400 linux-x64 run_linux linux-x64 linux/amd64 linux-x64 ;;
  linux-arm64) heavy 5400 linux-arm64 run_linux linux-arm64 linux/arm64 linux-arm64 ;;
  windows-x64) mkdir -p "$OUT/windows-x64"; commit > "$OUT/windows-x64/commit"
               if [ -n "$AZURE" ]; then run_windows_azure; else run_windows; fi ;;
  archives) heavy 900 archives run_archives ;;
  record) record ;;
  require) require ;;
  lock-status) sh "$ROOT/ci/lock.sh" status ;;
  lock-waits) sh "$ROOT/ci/lock.sh" waits ;;
  network) heavy 900 network run_network ;;
  secrets) limited 900 secrets run_secrets ;;
  index) run_index ;;
  summary) summary ;;
  stamp) note_parts; stamp_from_parts ;;
  plan) plan ;;
  selected)
    plan
    case "$PLAN_CLASS" in
      green) exit 0 ;;
      runtime) exec sh "$0" all ${AZURE:+--azure} ${HOST:+--host "$HOST"} ${KEY:+--key "$KEY"} ;;
    esac
    wanted=" $(targets_of "$PLAN_CLASS") "
    wpid=
    case "$wanted" in *" windows-x64 "*)
      mkdir -p "$OUT/windows-x64"
      CI_PARENT=$$ sh "$0" windows-x64 ${AZURE:+--azure} ${HOST:+--host "$HOST"} ${KEY:+--key "$KEY"} > "$OUT/windows-x64.run.log" 2>&1 & wpid=$!
      say "windows-x64 started (ci/out/windows-x64.run.log)" ;;
    esac
    acquire_lock "plugin $TARGET at $(git -C "$ROOT" rev-parse --short HEAD)"
    for t in $wanted; do
      case "$t" in
        secrets) limited 900 secrets run_secrets ;;
        archives) heavy 900 archives run_archives ;;
        network) heavy 900 network run_network ;;
        macos-arm64:shim) CI_SHIM_ONLY=1 heavy 5400 macos-arm64 run_mac macos-arm64 darwin-arm64 "" ;;
        macos-x64:shim) CI_SHIM_ONLY=1 heavy 5400 macos-x64 run_mac macos-x64 darwin-x64 "arch -x86_64" ;;
        linux-x64:shim) CI_SHIM_ONLY=1 heavy 5400 linux-x64 run_linux linux-x64 linux/amd64 linux-x64 ;;
        linux-arm64:shim) CI_SHIM_ONLY=1 heavy 5400 linux-arm64 run_linux linux-arm64 linux/arm64 linux-arm64 ;;
      esac
    done
    release_lock
    if [ -n "$wpid" ]; then say "waiting for windows-x64 (the shared lock is released)"; wait "$wpid"; wpid=; mv "$OUT/windows-x64.run.log" "$OUT/windows-x64/run.log" 2>/dev/null; fi
    summary
    record_selected ;;
  all)
    # Windows runs remotely from the start (it is the longest), the local jobs one at a time beside it.
    wpid=
    if [ -n "$AZURE$HOST" ]; then
      mkdir -p "$OUT/windows-x64"
      CI_PARENT=$$ sh "$0" windows-x64 ${AZURE:+--azure} ${HOST:+--host "$HOST"} ${KEY:+--key "$KEY"} > "$OUT/windows-x64.run.log" 2>&1 & wpid=$!
      say "windows-x64 started (ci/out/windows-x64.run.log)"
    else
      result windows-x64 shim not-run; result windows-x64 runtime not-run
    fi
    limited 900 secrets run_secrets
    heavy 5400 macos-arm64 run_mac macos-arm64 darwin-arm64 ""
    heavy 5400 macos-x64 run_mac macos-x64 darwin-x64 "arch -x86_64"
    heavy 5400 linux-x64 run_linux linux-x64 linux/amd64 linux-x64
    heavy 5400 linux-arm64 run_linux linux-arm64 linux/arm64 linux-arm64
    heavy 900 network run_network
    heavy 900 archives run_archives
    # The local heavy jobs are done: the lock is released now, so the other repositories' runners do not wait on a
    # Windows job that runs on its own VM (the VM's deletion stays with that job's own trap).
    release_lock
    if [ -n "$wpid" ]; then say "waiting for windows-x64 (the shared lock is released)"; wait "$wpid"; wpid=; mv "$OUT/windows-x64.run.log" "$OUT/windows-x64/run.log" 2>/dev/null; fi
    run_index
    summary
    record ;;
  *) sed -n '2,24p' "$0"; exit 64 ;;
esac
# A target run alone (a rerun after an outside failure) adds its record and writes the stamp when it completes the set.
case "$TARGET" in
  macos-arm64|macos-x64|linux-x64|linux-arm64|windows-x64|network|archives|secrets)
    [ -n "${CI_PARENT:-}" ] || { note_parts; stamp_from_parts || true; } ;;
esac
