#!/usr/bin/env bash
# Where a throwaway Azure VM can go now: region-and-size fallback for every runner that makes one (engine native
# Windows, plugin ci Windows, cloud checks). Keep the copies identical: scripts/azure-place.sh in theodore-presentations,
# ci/azure-place.sh in theodore-plugin. Sourced (bash).
#
#   az_place_candidates <cores>          "region size" lines, in order, whose family and region have <cores> free now
#   az_vm_create_placed <group> <vm> <cores> <az vm create args…>
#                                        az vm create in the first candidate; on QuotaExceeded, SkuNotAvailable,
#                                        OperationNotAllowed, AllocationFailed, ZonalAllocationFailed or a capacity
#                                        error it removes what the attempt left in the group and tries the next one.
#                                        Prints "region size" of the VM made; exit 1 when every candidate failed.
#
# AZ_PLACE_SIZES: sizes in order (default: Standard_F8s_v2 Standard_D8s_v4 Standard_D8s_v3 Standard_D8ds_v4 for 8
# cores, the _4 / F4s_v2 sizes for 4). AZ_PLACE_REGIONS (default: eastus2 eastus westus2 centralus). The order is
# region-major: every size in eastus2 first, then the next region. The group may live in any region; the VM goes where
# there is room (a resource group's location is only where its metadata lives).

az_place_family() { # size: the usage name of its quota family
  case "$1" in
    Standard_F*s_v2) echo standardFSv2Family ;;
    Standard_D*ds_v4) echo standardDDSv4Family ;;
    Standard_D*s_v4) echo standardDSv4Family ;;
    Standard_D*s_v3) echo standardDSv3Family ;;
    Standard_D*as_v4) echo standardDASv4Family ;;
    Standard_D*ps_v6) echo standardDPSv6Family ;;
    Standard_D*ps_v5) echo standardDPSv5Family ;;
    *) echo unknown ;;
  esac
}

az_place_sizes() { # cores
  if [ -n "${AZ_PLACE_SIZES:-}" ]; then echo "$AZ_PLACE_SIZES"; return; fi
  case "$1" in
    4) echo "Standard_F4s_v2 Standard_D4s_v4 Standard_D4s_v3 Standard_D4ds_v4" ;;
    *) echo "Standard_F8s_v2 Standard_D8s_v4 Standard_D8s_v3 Standard_D8ds_v4" ;;
  esac
}

az_place_candidates() { # cores
  local cores=$1 region size family usage free_family free_total
  for region in ${AZ_PLACE_REGIONS:-eastus2 eastus westus2 centralus}; do
    usage=$(az vm list-usage -l "$region" -o tsv --query "[].[name.value, currentValue, limit]" 2>/dev/null) || continue
    free_total=$(printf '%s\n' "$usage" | awk -F'\t' '$1=="cores"{print $3-$2}')
    for size in $(az_place_sizes "$cores"); do
      family=$(az_place_family "$size")
      free_family=$(printf '%s\n' "$usage" | awk -F'\t' -v f="$family" '$1==f{print $3-$2}')
      [ -n "$free_family" ] && [ -n "$free_total" ] || continue
      if [ "$free_family" -ge "$cores" ] && [ "$free_total" -ge "$cores" ]; then echo "$region $size"; fi
    done
  done
  return 0
}

az_place_clear_group() { # group: delete every resource an attempt left (the group itself stays, with its tags)
  local ids
  ids=$(az resource list -g "$1" --query "[].id" -o tsv 2>/dev/null)
  [ -z "$ids" ] && return 0
  # the VM first, then the rest (a NIC or a disk cannot go while the VM holds it)
  az vm delete --ids $(printf '%s\n' "$ids" | grep '/virtualMachines/') --yes -o none 2>/dev/null || true
  ids=$(az resource list -g "$1" --query "[].id" -o tsv 2>/dev/null)
  [ -n "$ids" ] && az resource delete --ids $ids -o none 2>/dev/null || true
}

az_vm_create_placed() { # group vm cores args…
  local group=$1 vm=$2 cores=$3 region size err tried=0
  shift 3
  err=$(mktemp)
  while read -r region size; do
    [ -n "$region" ] || continue
    tried=$((tried + 1))
    echo "azure-place: trying $size in $region" >&2
    if az vm create -g "$group" -n "$vm" -l "$region" --size "$size" "$@" -o none 2>"$err"; then
      rm -f "$err"; echo "$region $size"; return 0
    fi
    if grep -qiE 'QuotaExceeded|SkuNotAvailable|OperationNotAllowed|AllocationFailed|ZonalAllocationFailed|capacity|NotAvailableForSubscription' "$err"; then
      echo "azure-place: $size in $region refused ($(grep -oiE 'QuotaExceeded|SkuNotAvailable|OperationNotAllowed|ZonalAllocationFailed|AllocationFailed|capacity|NotAvailableForSubscription' "$err" | head -n 1)); next" >&2
      az_place_clear_group "$group"
      continue
    fi
    echo "azure-place: az vm create failed for another reason:" >&2; tail -n 5 "$err" >&2
    rm -f "$err"; return 1
  done < <(az_place_candidates "$cores")
  rm -f "$err"
  echo "azure-place: no region and size had room ($tried tried; sizes $(az_place_sizes "$cores"); regions ${AZ_PLACE_REGIONS:-eastus2 eastus westus2 centralus})" >&2
  return 1
}
