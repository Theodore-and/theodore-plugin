#!/usr/bin/env bash
# A throwaway Windows x64 test machine on Azure for ci/run.sh windows-x64 --azure: created for one run and deleted with
# everything it holds (its own resource group) when the run ends, pass, fail or interrupted. No standing VM.
# (A copy of the engine repository's scripts/native/azure-windows.sh with its own group prefix and tag.)
#
#   ci/azure-windows.sh up <state-dir>     create it; writes <state-dir>/host (user@ip), known_hosts, group
#   ci/azure-windows.sh down <state-dir>   delete its resource group (and wait), then the state folder
#   ci/azure-windows.sh admit <state-dir>  add the /24 this machine leaves from now to the SSH rule (its address can
#                                          change during a run; ci/run.sh calls it when the machine stops answering)
#   ci/azure-windows.sh sweep [hours]      delete every throwaway group left behind (tag purpose=theodore-plugin-ci
#                                          and a theodore-plugin-ci-<UTC> name); with hours, only those older; and
#                                          clear ci/out/azure-windows when the group it names no longer exists
#   ci/azure-windows.sh capture <state-dir>  the machine (deallocated) becomes the next version of the prepared image;
#                                          older versions are deleted once it exists (one image is kept)
#   ci/azure-windows.sh images             the prepared image's versions
#
# The prepared image: a specialized copy of a test machine after a run that passed (ci/run.sh with CI_AZURE_CAPTURE=1),
# in the gallery theodorePluginImages (resource group theodore-ci-images, kept, never swept): OpenSSH and the key set
# up, Git for Windows installed, the pinned CPython and the bundle download cache in C:\theodore-ci-cache. `up` uses
# its newest version when there is one (CI_AZURE_IMAGE overrides; CI_AZURE_IMAGE=marketplace forces the stock image).
#
# A deadline that does not need this Mac: the VM gets Azure's own auto-shutdown at CI_AZURE_DEADLINE_HOURS (default 4)
# after it is made, which deallocates it (no compute billed) even if the run that made it is gone, and every
# ci/run.sh windows-x64 --azure starts by sweeping groups older than that deadline plus one hour.
#
# Needs the Azure CLI signed in (az login) and an SSH key pair (CI_AZURE_KEY, default ~/.ssh/theodore_ci). What it
# makes, all in a new resource group theodore-plugin-ci-<UTC time> in CI_AZURE_LOCATION (default centralus):
#   one VM, Windows Server 2022 Datacenter (gen2), the first size of CI_AZURE_SIZE (space-separated) that Azure has
#   capacity for (default Standard_D8s_v3, then other 8- and 4-vCPU sizes), its disk, NIC, public IP and a network security
#   group that admits SSH only from the /24 networks this machine leaves from (probed a dozen times: a connection with
#   two uplinks alternates; CI_AZURE_SSH_FROM overrides). OpenSSH server is enabled through the VM agent
#   (az vm run-command) and trusts the one public key.
# The administrator password is random, lives in <state-dir> (mode 700) for the az call only (read by az from the
# file, never on a command line), and is deleted right after; logins use the key.
set -euo pipefail

say() { printf 'azure-windows: %s\n' "$*" >&2; }
die() { say "$*"; exit 1; }

ACTION=${1:-}
STATE=${2:-}
case "$ACTION" in sweep | images) ;; *) [ -n "$ACTION" ] && [ -n "$STATE" ] || { sed -n '2,20p' "$0"; exit 64; } ;; esac
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)
# centralus: each region allows this subscription 10 vCPU; the release lanes' machines use eastus2 and the engine's
# build machine and its image use eastus, so this 8-vCPU machine and its image take neither's quota.
LOCATION=${CI_AZURE_LOCATION:-centralus}
# Tried in order until Azure has capacity for one (a size can be "not available" in a region for hours).
# 8 vCPU first (the suites run side by side, five processes); a 4-vCPU size when the region's quota (10 vCPU for this
# subscription, shared with the engine's and other test machines) or capacity has no room for it.
SIZES=${CI_AZURE_SIZE:-Standard_D8s_v3 Standard_D8s_v4 Standard_D8as_v4 Standard_D4s_v3 Standard_F4s_v2 Standard_D4as_v4 Standard_D4s_v4}
MARKETPLACE=MicrosoftWindowsServer:WindowsServer:2022-datacenter-g2:latest
IMAGE=${CI_AZURE_IMAGE:-}
IMG_GROUP=${CI_AZURE_IMAGE_GROUP:-theodore-ci-images}
GALLERY=${CI_AZURE_GALLERY:-theodorePluginImages}
IMG_DEF=${CI_AZURE_IMAGE_DEF:-win2022-plugin-ci}
KEY=${CI_AZURE_KEY:-$HOME/.ssh/theodore_ci}
PREFIX=theodore-plugin-ci
PURPOSE=theodore-plugin-ci
USER_NAME=theoci
VM=theo-plugin-ci

# az, retried when this Mac's connection to Azure drops (connection reset, timeout); any other failure is returned
azr() {
  local n errf; errf=$(mktemp)
  for n in 1 2 3 4 5; do
    if az "$@" 2>"$errf"; then rm -f "$errf"; return 0; fi
    grep -q 'Connection aborted\|Connection reset\|ConnectionError\|RemoteDisconnected\|timed out\|Max retries' "$errf" || break
    say "the connection to Azure dropped (az $1 $2); again in 15 s"; sleep 15
  done
  grep -v '^WARNING' "$errf" >&2; rm -f "$errf"; return 1
}

up() {
  command -v az >/dev/null || die "the Azure CLI (az) is not installed"
  az account show >/dev/null 2>&1 || die "az is not signed in (az login)"
  [ -f "$KEY" ] && [ -f "$KEY.pub" ] || die "no SSH key pair at $KEY"
  mkdir -p "$STATE"; chmod 700 "$STATE"
  if [ -f "$STATE/group" ]; then
    # Left by a run whose group was deleted since (by sweep or by hand): nothing to keep.
    if [ "$(az group exists -n "$(cat "$STATE/group")" -o tsv 2>/dev/null)" = false ]; then
      say "clearing $STATE: its group $(cat "$STATE/group") no longer exists"; rm -rf "${STATE:?}"; mkdir -p "$STATE"; chmod 700 "$STATE"
    else
      die "$STATE already names a machine ($(cat "$STATE/group")); run down first"
    fi
  fi
  local image=$IMAGE specialized=
  [ -n "$image" ] || image=$(latest_image)
  [ -n "$image" ] && [ "$image" != marketplace ] || image=$MARKETPLACE
  case "$image" in */galleries/*) specialized=1 ;; esac
  say "image: ${image##*/galleries/}"
  local group; group="$PREFIX-$(date -u +%Y%m%d%H%M%S)"
  echo "$group" > "$STATE/group"
  # Who may connect: CI_AZURE_SSH_FROM (space-separated prefixes), else every /24 this machine was seen leaving
  # from in a dozen probes (a connection with more than one uplink alternates between them).
  local from=${CI_AZURE_SSH_FROM:-} ip i
  if [ -z "$from" ]; then
    for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
      ip=$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null) || continue
      [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && from="$from ${ip%.*}.0/24"
    done
    from=$(printf '%s\n' $from | sort -u | tr '\n' ' ')
  fi
  [ -n "${from// /}" ] || die "cannot read this machine's public address"
  say "SSH admitted from: $from"
  # 24 characters from the system's random source, with the four classes Windows asks for
  [ -n "$specialized" ] || ( umask 077; printf 'Aa1-%s' "$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)" > "$STATE/password" )
  local deadline_h=${CI_AZURE_DEADLINE_HOURS:-4} expires shutdown_at
  expires=$(date -u -v+"${deadline_h}"H +%Y-%m-%dT%H:%MZ 2>/dev/null || date -u -d "+$deadline_h hours" +%Y-%m-%dT%H:%MZ)
  shutdown_at=$(date -u -v+"${deadline_h}"H +%H%M 2>/dev/null || date -u -d "+$deadline_h hours" +%H%M)
  local t
  for t in 1 2 3 4; do  # retried: this Mac's connection to Azure can drop
    az group create -n "$group" -l "$LOCATION" --tags purpose=$PURPOSE expires="$expires" -o none && break
    [ "$t" -lt 4 ] || die "could not create $group"; sleep 15
  done
  local size made=
  for size in $SIZES; do
    say "creating $group ($size, $LOCATION)"
    if [ -n "$specialized" ]; then
      set -- --specialized $(image_security "$image")
    else
      set -- --admin-username "$USER_NAME" --admin-password "@$STATE/password"
    fi
    local try
    for try in 1 2 3; do
      if az vm create -g "$group" -n "$VM" --image "$image" --size "$size" "$@" --public-ip-sku Standard --nsg-rule NONE \
          --os-disk-size-gb 128 --storage-sku Premium_LRS -o none 2>"$STATE/create.err"; then
        made=$size; break
      fi
      # This Mac's connection to Azure dropped (not the size): the same size again
      grep -q 'Connection aborted\|Connection reset\|ConnectionError\|timed out\|RemoteDisconnected' "$STATE/create.err" || break
      say "the connection to Azure dropped; trying $size again"; sleep 20
      az vm delete -g "$group" -n "$VM" --yes -o none 2>/dev/null || true
    done
    [ -z "$made" ] || break
    grep -o 'SkuNotAvailable[^.]*\|QuotaExceeded[^.]*\|OperationNotAllowed[^.]*' "$STATE/create.err" | head -n 1 >&2 || tail -n 3 "$STATE/create.err" >&2
    az vm delete -g "$group" -n "$VM" --yes -o none 2>/dev/null || true
  done
  [ -n "$made" ] || die "no size of '$SIZES' could be made in $LOCATION"
  echo "$made" > "$STATE/size"
  rm -f "$STATE/password"
  # The deadline Azure keeps by itself: deallocated at that UTC time even if nothing here is left to delete it.
  azr vm auto-shutdown -g "$group" -n "$VM" --time "$shutdown_at" -o none || die "could not set the VM's auto-shutdown deadline"
  say "auto-shutdown (deallocate) at $shutdown_at UTC"
  azr network nsg rule create -g "$group" --nsg-name "${VM}NSG" -n ssh-from-runner --priority 100 --direction Inbound \
    --access Allow --protocol Tcp --source-address-prefixes $from --destination-port-ranges 22 -o none
  local pub; pub=$(cut -d' ' -f1-2 "$KEY.pub")
  [[ "$pub" =~ ^ssh-(ed25519|rsa)\ [A-Za-z0-9+/=]+$ ]] || die "$KEY.pub is not an ssh-ed25519 or ssh-rsa public key"
  cat > "$STATE/sshd.ps1" <<PS
\$ErrorActionPreference = 'Stop'
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 | Out-Null
Set-Service sshd -StartupType Automatic
Start-Service sshd
Set-Content -Path C:\\ProgramData\\ssh\\administrators_authorized_keys -Value '$pub' -Encoding ascii
icacls C:\\ProgramData\\ssh\\administrators_authorized_keys /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null
if (-not (Get-NetFirewallRule -Name sshd -ErrorAction SilentlyContinue)) { New-NetFirewallRule -Name sshd -DisplayName sshd -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null }
'sshd ready'
PS
  if [ -z "$specialized" ]; then
    say "enabling OpenSSH server"
    azr vm run-command invoke -g "$group" -n "$VM" --command-id RunPowerShellScript --scripts "@$STATE/sshd.ps1" \
      --query 'value[0].message' -o tsv | tail -n 1 >&2
  fi
  local addr; addr=$(azr vm show -d -g "$group" -n "$VM" --query publicIps -o tsv)
  echo "$USER_NAME@$addr" > "$STATE/host"
  : > "$STATE/known_hosts"
  local i
  for i in $(seq 1 30); do
    if ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$STATE/known_hosts" \
        "$USER_NAME@$addr" "echo ready" >/dev/null 2>&1; then
      say "ready: $USER_NAME@$addr"; return 0
    fi
    # This machine may now leave from another address than the ones probed at the start.
    [ $((i % 6)) -ne 0 ] || admit || true
    sleep 10
  done
  die "no SSH answer from $addr"
}

down() {
  [ -f "$STATE/group" ] || { say "nothing to delete in $STATE"; return 0; }
  local group; group=$(cat "$STATE/group")
  [[ "$group" =~ ^$PREFIX-[0-9]{14}$ ]] || die "refusing to delete '$group': not a group this script made"
  say "deleting $group"
  local n=0
  until az group delete -n "$group" --yes -o none; do
    n=$((n + 1))
    [ $n -lt 6 ] || die "could not delete $group (network?): its auto-shutdown deallocates it, and the next run's sweep deletes it"
    sleep 30
  done
  rm -rf "${STATE:?}"
  say "deleted $group"
}

admit() {
  [ -f "$STATE/group" ] || die "no machine in $STATE"
  local group ip net cur; group=$(cat "$STATE/group")
  [[ "$group" =~ ^$PREFIX-[0-9]{14}$ ]] || die "refusing to change '$group': not a group this script made"
  ip=$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null) || { say "cannot read this machine's public address"; return 1; }
  [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { say "cannot read this machine's public address"; return 1; }
  net="${ip%.*}.0/24"
  cur=$(azr network nsg rule show -g "$group" --nsg-name "${VM}NSG" -n ssh-from-runner \
    --query "join(' ', [sourceAddressPrefix || '', join(' ', sourceAddressPrefixes)])" -o tsv)
  case " $cur " in *" $net "*) return 0 ;; esac
  azr network nsg rule update -g "$group" --nsg-name "${VM}NSG" -n ssh-from-runner \
    --source-address-prefixes $(printf '%s\n' $cur "$net" | sort -u) -o none
  say "SSH now also admitted from $net"
}

# --- the prepared image (kept: resource group theodore-ci-images) ------------------------------------------------------
latest_image() { # the newest version of the image definition, as an id; empty when there is none
  az sig image-version list -g "$IMG_GROUP" -r "$GALLERY" -i "$IMG_DEF" --query "sort_by([?provisioningState=='Succeeded'], &name)[-1].id" -o tsv 2>/dev/null || true
}
image_security() { # the --security-type the image's definition asks for (Trusted Launch), if any
  local def=${1%/versions/*} t
  t=$(az sig image-definition show --ids "$def" --query "features[?name=='SecurityType'].value | [0]" -o tsv 2>/dev/null) || return 0
  case "$t" in TrustedLaunch*) echo "--security-type TrustedLaunch" ;; esac
}

capture() {
  [ -f "$STATE/group" ] || die "no machine in $STATE"
  local group vmid sec version t0 old; group=$(cat "$STATE/group"); t0=$(date +%s)
  [[ "$group" =~ ^$PREFIX-[0-9]{14}$ ]] || die "refusing to capture '$group': not a group this script made"
  vmid=$(az vm show -g "$group" -n "$VM" --query id -o tsv)
  sec=$(az vm show -g "$group" -n "$VM" --query securityProfile.securityType -o tsv 2>/dev/null)
  az group show -n "$IMG_GROUP" -o none 2>/dev/null || az group create -n "$IMG_GROUP" -l "$LOCATION" --tags purpose=theodore-ci-image -o none
  az sig show -g "$IMG_GROUP" -r "$GALLERY" -o none 2>/dev/null || az sig create -g "$IMG_GROUP" -r "$GALLERY" -l "$LOCATION" -o none
  if ! az sig image-definition show -g "$IMG_GROUP" -r "$GALLERY" -i "$IMG_DEF" -o none 2>/dev/null; then
    az sig image-definition create -g "$IMG_GROUP" -r "$GALLERY" -i "$IMG_DEF" --publisher theodore --offer ci --sku "$IMG_DEF" \
      --os-type Windows --os-state Specialized --hyper-v-generation V2 ${sec:+--features SecurityType=$sec} -l "$LOCATION" -o none
  fi
  say "capturing $group into $GALLERY/$IMG_DEF"
  azr vm deallocate -g "$group" -n "$VM" -o none
  version="1.$(date -u +%Y%m%d).$((10#$(date -u +%H%M)))"
  azr sig image-version create -g "$IMG_GROUP" -r "$GALLERY" -i "$IMG_DEF" -e "$version" --virtual-machine "$vmid" -l "$LOCATION" \
    --target-regions "$LOCATION" --replica-count 1 --storage-account-type Standard_LRS -o none || die "the image version could not be made"
  for old in $(az sig image-version list -g "$IMG_GROUP" -r "$GALLERY" -i "$IMG_DEF" --query "[?name!='$version'].name" -o tsv); do
    az sig image-version delete -g "$IMG_GROUP" -r "$GALLERY" -i "$IMG_DEF" -e "$old" -o none && say "deleted the older image version $old"
  done
  say "image $GALLERY/$IMG_DEF/$version made in $(( $(date +%s) - t0 )) s"
}

images() {
  az sig image-version list -g "$IMG_GROUP" -r "$GALLERY" -i "$IMG_DEF" \
    --query "[].{version:name, state:provisioningState, regions:join(',', publishingProfile.targetRegions[].name)}" -o table 2>/dev/null
}

sweep() {
  local group found=0 hours=${STATE:-} made age
  [ -z "$hours" ] || [[ "$hours" =~ ^[0-9]+$ ]] || die "sweep takes a number of hours"
  for group in $(az group list --tag purpose=$PURPOSE --query '[].name' -o tsv); do
    [[ "$group" =~ ^$PREFIX-[0-9]{14}$ ]] || { say "skipping $group: not a name this script makes"; continue; }
    if [ -n "$hours" ]; then
      made=${group##*-}
      age=$(( $(date -u +%s) - $(date -u -j -f %Y%m%d%H%M%S "$made" +%s 2>/dev/null || date -u -d "${made:0:8} ${made:8:2}:${made:10:2}:${made:12:2}" +%s) ))
      [ "$age" -ge $((hours * 3600)) ] || continue
    fi
    say "deleting leftover $group"; azr group delete -n "$group" --yes -o none; found=$((found + 1))
  done
  say "sweep: $found group(s) deleted"
  local dir="$ROOT/ci/out/azure-windows"
  if [ -f "$dir/group" ] && [ "$(az group exists -n "$(cat "$dir/group")" -o tsv 2>/dev/null)" = false ]; then
    say "clearing $dir: its group $(cat "$dir/group") no longer exists"; rm -rf "${dir:?}"
  fi
}

# One compound command, read whole before it runs: editing this file during a run cannot shift what runs next.
{
  case "$ACTION" in
    up) up ;;
    down) down ;;
    admit) admit ;;
    sweep) sweep ;;
    capture) capture ;;
    images) images ;;
    *) sed -n '2,20p' "$0"; exit 64 ;;
  esac
  exit $?
}
