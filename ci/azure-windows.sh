#!/usr/bin/env bash
# A throwaway Windows x64 test machine on Azure for ci/run.sh windows-x64 --azure: created for one run and deleted with
# everything it holds (its own resource group) when the run ends, pass, fail or interrupted. No standing VM.
# (A copy of the engine repository's scripts/native/azure-windows.sh with its own group prefix and tag.)
#
#   ci/azure-windows.sh up <state-dir>     create it; writes <state-dir>/host (user@ip), known_hosts, group
#   ci/azure-windows.sh down <state-dir>   delete its resource group (and wait), then the state folder
#   ci/azure-windows.sh sweep              delete every throwaway group left behind (tag purpose=theodore-plugin-ci
#                                          and a theodore-plugin-ci-<UTC> name)
#
# Needs the Azure CLI signed in (az login) and an SSH key pair (CI_AZURE_KEY, default ~/.ssh/theodore_ci). What it
# makes, all in a new resource group theodore-plugin-ci-<UTC time> in CI_AZURE_LOCATION (default eastus2):
#   one VM, Windows Server 2022 Datacenter (gen2), the first size of CI_AZURE_SIZE (space-separated) that Azure has
#   capacity for (default Standard_D4s_v3, then other 4-vCPU sizes), its disk, NIC, public IP and a network security
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
[ -n "$ACTION" ] && { [ -n "$STATE" ] || [ "$ACTION" = sweep ]; } || { sed -n '2,9p' "$0"; exit 64; }
LOCATION=${CI_AZURE_LOCATION:-eastus2}
# Tried in order until Azure has capacity for one (a size can be "not available" in a region for hours).
SIZES=${CI_AZURE_SIZE:-Standard_D4s_v3 Standard_F4s_v2 Standard_D4as_v4 Standard_D4s_v4}
IMAGE=${CI_AZURE_IMAGE:-MicrosoftWindowsServer:WindowsServer:2022-datacenter-g2:latest}
KEY=${CI_AZURE_KEY:-$HOME/.ssh/theodore_ci}
PREFIX=theodore-plugin-ci
PURPOSE=theodore-plugin-ci
USER_NAME=theoci
VM=theo-plugin-ci

up() {
  command -v az >/dev/null || die "the Azure CLI (az) is not installed"
  az account show >/dev/null 2>&1 || die "az is not signed in (az login)"
  [ -f "$KEY" ] && [ -f "$KEY.pub" ] || die "no SSH key pair at $KEY"
  mkdir -p "$STATE"; chmod 700 "$STATE"
  [ ! -f "$STATE/group" ] || die "$STATE already names a machine ($(cat "$STATE/group")); run down first"
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
  ( umask 077; printf 'Aa1-%s' "$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)" > "$STATE/password" )
  az group create -n "$group" -l "$LOCATION" --tags purpose=$PURPOSE -o none
  local size made=
  for size in $SIZES; do
    say "creating $group ($size, $LOCATION)"
    if az vm create -g "$group" -n "$VM" --image "$IMAGE" --size "$size" --admin-username "$USER_NAME" \
        --admin-password "@$STATE/password" --public-ip-sku Standard --nsg-rule NONE --os-disk-size-gb 128 \
        --storage-sku Premium_LRS -o none 2>"$STATE/create.err"; then
      made=$size; break
    fi
    grep -o 'SkuNotAvailable[^.]*\|QuotaExceeded[^.]*\|OperationNotAllowed[^.]*' "$STATE/create.err" | head -n 1 >&2 || tail -n 3 "$STATE/create.err" >&2
    az vm delete -g "$group" -n "$VM" --yes -o none 2>/dev/null || true
  done
  [ -n "$made" ] || die "no size of '$SIZES' could be made in $LOCATION"
  echo "$made" > "$STATE/size"
  rm -f "$STATE/password"
  az network nsg rule create -g "$group" --nsg-name "${VM}NSG" -n ssh-from-runner --priority 100 --direction Inbound \
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
  say "enabling OpenSSH server"
  az vm run-command invoke -g "$group" -n "$VM" --command-id RunPowerShellScript --scripts "@$STATE/sshd.ps1" \
    --query 'value[0].message' -o tsv | tail -n 1 >&2
  local addr; addr=$(az vm show -d -g "$group" -n "$VM" --query publicIps -o tsv)
  echo "$USER_NAME@$addr" > "$STATE/host"
  : > "$STATE/known_hosts"
  local i
  for i in $(seq 1 30); do
    if ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$STATE/known_hosts" \
        "$USER_NAME@$addr" "echo ready" >/dev/null 2>&1; then
      say "ready: $USER_NAME@$addr"; return 0
    fi
    sleep 10
  done
  die "no SSH answer from $addr"
}

down() {
  [ -f "$STATE/group" ] || { say "nothing to delete in $STATE"; return 0; }
  local group; group=$(cat "$STATE/group")
  [[ "$group" =~ ^$PREFIX-[0-9]{14}$ ]] || die "refusing to delete '$group': not a group this script made"
  say "deleting $group"
  az group delete -n "$group" --yes -o none
  rm -rf "${STATE:?}"
  say "deleted $group"
}

sweep() {
  local group found=0
  for group in $(az group list --tag purpose=$PURPOSE --query '[].name' -o tsv); do
    [[ "$group" =~ ^$PREFIX-[0-9]{14}$ ]] || { say "skipping $group: not a name this script makes"; continue; }
    say "deleting leftover $group"; az group delete -n "$group" --yes -o none; found=$((found + 1))
  done
  say "sweep: $found group(s) deleted"
}

# One compound command, read whole before it runs: editing this file during a run cannot shift what runs next.
{
  case "$ACTION" in
    up) up ;;
    down) down ;;
    sweep) sweep ;;
    *) sed -n '2,9p' "$0"; exit 64 ;;
  esac
  exit $?
}
