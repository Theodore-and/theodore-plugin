#!/usr/bin/env bash
# A standing Windows x64 build machine on Azure: made once, deallocated whenever no run uses it (then it costs its disk
# only), started on demand by the runs that need it, with its tools, download caches and compile caches kept warm on
# its own disk. Keep the copies identical: scripts/build-machine.sh in theodore-presentations, ci/build-machine.sh in
# theodore-plugin.
#
#   build-machine.sh create <name>      make it: resource group theodore-build-<name> (tag purpose=theodore-build), one
#                                       VM (Windows Server 2022 Datacenter gen2, the image the throwaway machines use;
#                                       8 vCPU where scripts/azure-place.sh finds room), a static public IP, SSH only
#                                       from this Mac's /24 networks, OpenSSH with the one public key; then deallocated
#   build-machine.sh start <name>       a lease for this process tree (BUILD_MACHINE_LEASE_PID, default the caller's
#                                       parent shell), the machine started when it is not running, this Mac's address
#                                       admitted, the backstop renewed; prints user@host when SSH answers. One run at a
#                                       time: exit 3 when another live lease holds it (the caller goes elsewhere)
#   build-machine.sh stop <name>        drop this lease; when no live lease is left, remove the work folders and
#                                       deallocate (BUILD_MACHINE_KEEP=1 leaves it running)
#   build-machine.sh status [name]      each machine: power state, size, region, live leases
#   build-machine.sh destroy <name>     delete its resource group and its state
#   build-machine.sh known-hosts <name> the path of its known_hosts file (for ssh -o UserKnownHostsFile=)
#
# Safety. A run that dies leaves a lease whose process is gone: the next start or stop ignores it. Backstops that need
# nothing on this Mac: Azure's auto-shutdown (deallocate) is moved to BUILD_MACHINE_DEADLINE_HOURS (default 3) after
# every start, and a task on the machine removes the work folders (C:\theo-n, C:\theodore-ci) at every boot, so no engine
# source or payload outlives a run on its disk even when its own cleanup could not finish.
# What it holds: tools, pip and npm download caches, the compile caches (C:\theo-cache), the plugin's runtime bundle
# cache. Never a secret (logins use this Mac's key; no password is kept: the administrator password is random, used
# once by az from a mode-600 file and deleted), never a fixture deck (the runners refuse to send one).
# State (shared by every checkout and both repositories): $BUILD_MACHINE_HOME (default ~/.theodore-build)/<name>/.
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/azure-place.sh"

say() { printf 'build-machine: %s\n' "$*" >&2; }
die() { say "$*"; exit 1; }

ACTION=${1:-}; NAME=${2:-}
HOME_DIR=${BUILD_MACHINE_HOME:-$HOME/.theodore-build}
KEY=${BUILD_MACHINE_KEY:-$HOME/.ssh/theodore_ci}
USER_NAME=theoci
VM=theo-build
MARKETPLACE=MicrosoftWindowsServer:WindowsServer:2022-datacenter-g2:latest
case "$ACTION" in status) ;; create | start | stop | destroy | known-hosts)
  [[ "$NAME" =~ ^[a-z][a-z0-9-]{1,20}$ ]] || die "a name is lowercase letters, digits and '-' (for example engine-win)" ;;
  *) sed -n '2,32p' "$0"; exit 64 ;; esac
ST="$HOME_DIR/$NAME"
GROUP="theodore-build-$NAME"

azr() { # az, retried when this Mac's connection to Azure drops
  local n errf; errf=$(mktemp)
  for n in 1 2 3 4 5; do
    if az "$@" 2>"$errf"; then rm -f "$errf"; return 0; fi
    grep -q 'Connection aborted\|Connection reset\|ConnectionError\|RemoteDisconnected\|timed out\|Max retries' "$errf" || break
    sleep 15
  done
  grep -v '^WARNING' "$errf" >&2; rm -f "$errf"; return 1
}
my_nets() {
  local from=${BUILD_MACHINE_SSH_FROM:-} ip i
  if [ -z "$from" ]; then
    for i in 1 2 3 4 5 6; do
      ip=$(curl -fsS --max-time 8 https://api.ipify.org 2>/dev/null) || continue
      [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && from="$from ${ip%.*}.0/24"
    done
  fi
  printf '%s\n' $from | sort -u | tr '\n' ' '
}
admit() { # add this Mac's current /24 networks to the SSH rule (kept: earlier ones stay)
  local cur nets
  nets=$(my_nets); [ -n "${nets// /}" ] || { say "cannot read this Mac's public address"; return 1; }
  cur=$(azr network nsg rule show -g "$GROUP" --nsg-name "${VM}NSG" -n ssh-from-release-mac \
    --query "join(' ', [sourceAddressPrefix || '', join(' ', sourceAddressPrefixes)])" -o tsv) || return 1
  local all; all=$(printf '%s\n' $cur $nets | grep -v '^$' | sort -u | tr '\n' ' ')
  [ "$(printf '%s\n' $cur | grep -v '^$' | sort -u | tr '\n' ' ')" = "$all" ] && return 0
  # shellcheck disable=SC2086
  azr network nsg rule update -g "$GROUP" --nsg-name "${VM}NSG" -n ssh-from-release-mac --source-address-prefixes $all -o none
  say "SSH admitted from: $all"
}
ssh_ok() { ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$ST/known_hosts" "$(cat "$ST/host")" "echo ready" >/dev/null 2>&1; }
power() { azr vm get-instance-view -g "$GROUP" -n "$VM" --query "instanceView.statuses[?starts_with(code,'PowerState/')].code | [0]" -o tsv 2>/dev/null | sed 's|PowerState/||'; }
backstop() { # Azure deallocates it at this UTC time, whatever this Mac does
  local h=${BUILD_MACHINE_DEADLINE_HOURS:-3} at
  at=$(date -u -v+"${h}"H +%H%M 2>/dev/null || date -u -d "+$h hours" +%H%M)
  azr vm auto-shutdown -g "$GROUP" -n "$VM" --time "$at" --location "$(cat "$ST/location")" -o none && say "backstop: deallocated at $at UTC at the latest"
}
live_leases() { # prints the pids of live leases; removes dead ones
  local f pid
  mkdir -p "$ST/leases"
  for f in "$ST/leases"/*; do
    [ -e "$f" ] || continue
    pid=${f##*/}
    if kill -0 "$pid" 2>/dev/null; then echo "$pid"; else rm -f "$f"; fi
  done
}
remote_ps() {
  local enc; enc=$(printf '%s' "\$ErrorActionPreference='Stop'; \$ProgressPreference='SilentlyContinue'; $1" | iconv -f UTF-8 -t UTF-16LE | base64 | tr -d '\n')
  ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$ST/known_hosts" "$(cat "$ST/host")" \
    "powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand $enc"
}
# The work folders the runners use (engine: run.sh's C:\theo-n; plugin: ci/run.sh's C:\theodore-ci). Never the caches.
WORK_FOLDERS="'C:\\theo-n','C:\\theodore-ci'"
# What the boot task runs (rd through \\?\: parity leaves paths past 260 characters, which Remove-Item cannot delete)
BOOT_PS="cmd /d /c 'rd /s /q \\\\?\\C:\\theo-n'; cmd /d /c 'rd /s /q \\\\?\\C:\\theodore-ci'"
CLEAN_PS="foreach (\$d in @($WORK_FOLDERS)) { Get-CimInstance Win32_Process | Where-Object { \$_.CommandLine -like \"*\$d\\*\" -and \$_.ProcessId -ne \$PID } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force -ErrorAction SilentlyContinue }; foreach (\$i in 1..6) { if (Test-Path \$d) { & { \$ErrorActionPreference = 'Continue'; & cmd.exe /d /c \"rd /s /q \\\\?\\\$d\" 2>&1 | Out-Null } }; if (-not (Test-Path \$d)) { break }; Start-Sleep 5 } }"

create() {
  command -v az >/dev/null || die "the Azure CLI (az) is not installed"
  az account show >/dev/null 2>&1 || die "az is not signed in (az login)"
  [ -f "$KEY" ] && [ -f "$KEY.pub" ] || die "no SSH key pair at $KEY"
  [ ! -f "$ST/host" ] || die "$NAME exists already ($ST)"
  mkdir -p "$ST/leases"; chmod 700 "$ST"
  local nets; nets=$(my_nets); [ -n "${nets// /}" ] || die "cannot read this Mac's public address"
  azr group create -n "$GROUP" -l "${BUILD_MACHINE_GROUP_LOCATION:-eastus}" --tags purpose=theodore-build machine="$NAME" -o none
  echo "$GROUP" > "$ST/group"
  ( umask 077; printf 'Aa1-%s' "$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)" > "$ST/password" )
  export AZ_PLACE_REGIONS=${BUILD_MACHINE_REGIONS:-eastus westus2 centralus eastus2 northeurope westeurope}
  local placed
  placed=$(az_vm_create_placed "$GROUP" "$VM" "${BUILD_MACHINE_CORES:-8}" --image "$MARKETPLACE" --admin-username "$USER_NAME" \
    --admin-password "@$ST/password" --public-ip-sku Standard --public-ip-address-allocation static --nsg-rule NONE \
    --os-disk-size-gb 128 --storage-sku Premium_LRS --tags purpose=theodore-build machine="$NAME" 2>"$ST/create.err") || {
      rm -f "$ST/password"; grep -v '^WARNING' "$ST/create.err" | tail -n 4 >&2; die "no region had room; the group $GROUP is left empty (destroy $NAME)"; }
  rm -f "$ST/password"
  echo "${placed% *}" > "$ST/location"; echo "${placed#* }" > "$ST/size"
  say "made ${placed#* } in ${placed% *}"
  # shellcheck disable=SC2086
  azr network nsg rule create -g "$GROUP" --nsg-name "${VM}NSG" -n ssh-from-release-mac --priority 100 --direction Inbound \
    --access Allow --protocol Tcp --source-address-prefixes $nets --destination-port-ranges 22 -o none
  local pub; pub=$(cut -d' ' -f1-2 "$KEY.pub")
  [[ "$pub" =~ ^ssh-(ed25519|rsa)\ [A-Za-z0-9+/=]+$ ]] || die "$KEY.pub is not an ssh-ed25519 or ssh-rsa public key"
  cat > "$ST/setup.ps1" <<PS
\$ErrorActionPreference = 'Stop'
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null
Set-Service sshd -StartupType Automatic
Start-Service sshd
Set-Content -Path C:\\ProgramData\\ssh\\administrators_authorized_keys -Value '$pub' -Encoding ascii
icacls C:\\ProgramData\\ssh\\administrators_authorized_keys /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null
if (-not (Get-NetFirewallRule -Name sshd -ErrorAction SilentlyContinue)) { New-NetFirewallRule -Name sshd -DisplayName sshd -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null }
# At every boot: the runners' work folders go (engine source and payloads never outlive a run on this disk)
New-Item -ItemType Directory -Force C:\\theo-boot | Out-Null
Set-Content -Path C:\\theo-boot\\clean.ps1 -Encoding ascii -Value "$BOOT_PS"
\$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\\theo-boot\\clean.ps1'
Register-ScheduledTask -TaskName theo-clean-at-boot -Action \$a -Trigger (New-ScheduledTaskTrigger -AtStartup) -User SYSTEM -RunLevel Highest -Force | Out-Null
# Defender would scan every file a compile writes: the caches and work folders are left out of real-time scanning
Add-MpPreference -ExclusionPath 'C:\\theo-cache','C:\\theo-n','C:\\theodore-ci','C:\\theodore-ci-cache' -ErrorAction SilentlyContinue
'setup done'
PS
  azr vm run-command invoke -g "$GROUP" -n "$VM" --command-id RunPowerShellScript --scripts "@$ST/setup.ps1" --query 'value[0].message' -o tsv | tail -n 1 >&2
  rm -f "$ST/setup.ps1"
  echo "$USER_NAME@$(azr vm show -d -g "$GROUP" -n "$VM" --query publicIps -o tsv)" > "$ST/host"
  : > "$ST/known_hosts"
  local i; for i in $(seq 1 30); do ssh_ok && break; sleep 10; done
  ssh_ok || die "no SSH answer from $(cat "$ST/host")"
  azr vm deallocate -g "$GROUP" -n "$VM" -o none
  say "$NAME ready and deallocated: $(cat "$ST/host") ($(cat "$ST/size"), $(cat "$ST/location"))"
}

# One process at a time decides on the leases (start and stop): mkdir is atomic; a holder that is gone is taken over.
state_lock() {
  local i
  for i in $(seq 1 120); do
    if mkdir "$ST/lock" 2>/dev/null; then echo $$ > "$ST/lock/pid"; return 0; fi
    kill -0 "$(cat "$ST/lock/pid" 2>/dev/null)" 2>/dev/null || { rm -rf "$ST/lock"; continue; }
    sleep 1
  done
  die "the state lock of $NAME is held by $(cat "$ST/lock/pid" 2>/dev/null)"
}
state_unlock() { rm -rf "$ST/lock"; }

start() {
  [ -f "$ST/host" ] || die "no machine $NAME (create it first)"
  mkdir -p "$ST/leases"
  local pid=${BUILD_MACHINE_LEASE_PID:-$PPID} others
  # One run at a time: a second run (its work folder, its cleanup) would collide with the first. It is told so (exit 3)
  # and goes to a throwaway machine instead.
  state_lock
  others=$(live_leases | { grep -vx "$pid" || true; } | tr '\n' ' ')
  if [ -n "${others// /}" ]; then state_unlock; say "$NAME is in use by $others"; exit 3; fi
  echo "$(date -u +%FT%TZ)" > "$ST/leases/$pid"
  state_unlock
  local t0=$SECONDS state; state=$(power)
  case "$state" in
    running) say "$NAME is running" ;;
    *) say "$NAME is ${state:-unknown}: starting"; azr vm start -g "$GROUP" -n "$VM" -o none ;;
  esac
  backstop || say "the backstop could not be renewed (the last one still stands)"
  admit || true
  local i; for i in $(seq 1 40); do ssh_ok && break; [ $((i % 6)) -ne 0 ] || admit || true; sleep 5; done
  ssh_ok || die "no SSH answer from $(cat "$ST/host")"
  # The only run on it: what an earlier run that died left in the work folders goes now (with its processes), and the
  # boot task's list is written again (it may predate a folder added here).
  if [ "$(live_leases | grep -vcx "$pid")" = 0 ]; then
    remote_ps "$CLEAN_PS; New-Item -ItemType Directory -Force C:\\theo-boot | Out-Null; Set-Content -Path C:\\theo-boot\\clean.ps1 -Encoding ascii -Value \"$BOOT_PS\"" >"$ST/clean.log" 2>&1 \
      || say "the work folders could not be cleared now"
  fi
  say "$NAME ready after $((SECONDS - t0)) s"
  cat "$ST/host"
}

stop() {
  [ -f "$ST/host" ] || die "no machine $NAME"
  state_lock
  rm -f "$ST/leases/${BUILD_MACHINE_LEASE_PID:-$PPID}"
  local live; live=$(live_leases | tr '\n' ' ')
  if [ -n "${live// /}" ]; then state_unlock; say "$NAME still used by: $live; left running"; return 0; fi
  [ -z "${BUILD_MACHINE_KEEP:-}" ] || { state_unlock; say "BUILD_MACHINE_KEEP: $NAME left running"; return 0; }
  if [ "$(power)" = running ]; then
    remote_ps "$CLEAN_PS" >/dev/null 2>&1 || say "the work folders could not be removed now; the boot task removes them at the next start"
  fi
  # held through the deallocation: a start that comes now waits, then starts the machine again
  azr vm deallocate -g "$GROUP" -n "$VM" -o none && say "$NAME deallocated"
  state_unlock
}

status() {
  local d n
  for d in "$HOME_DIR"/*/; do
    [ -f "$d/host" ] || continue
    n=$(basename "$d"); ST=$HOME_DIR/$n; GROUP="theodore-build-$n"
    printf '%-14s %-12s %-18s %-10s leases: %s\n' "$n" "$(power)" "$(cat "$d/size")" "$(cat "$d/location")" "$(live_leases | tr '\n' ' ')"
  done
}

destroy() {
  [[ "$GROUP" =~ ^theodore-build-[a-z][a-z0-9-]+$ ]] || die "refusing to delete '$GROUP'"
  [ "$(az group show -n "$GROUP" --query tags.purpose -o tsv 2>/dev/null)" = theodore-build ] || { say "$GROUP is not there (or not tagged purpose=theodore-build)"; rm -rf "${ST:?}"; return 0; }
  azr group delete -n "$GROUP" --yes -o none && say "deleted $GROUP"
  rm -rf "${ST:?}"
}

{
  case "$ACTION" in
    create) create ;; start) start ;; stop) stop ;; status) status ;; destroy) destroy ;;
    known-hosts) echo "$ST/known_hosts" ;;
  esac
  exit $?
}
