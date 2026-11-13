#!/bin/sh
# The shared lock of the three repositories' check runners (theodore scripts/checks.sh, theodore-presentations
# scripts/checks.sh, theodore-plugin ci/run.sh), as a fair queue. The same file is in each repository; keep the copies
# identical (scripts/ci-lock.sh in theodore and theodore-presentations, ci/lock.sh in theodore-plugin).
#
# Sourced by a runner (POSIX sh, so the plugin's /bin/sh runner can use it):
#   ci_lock_acquire "<label>"   enqueue a ticket, wait until it is the first one and the lock is free, take the lock
#   ci_lock_release             release the lock (or withdraw the ticket when still waiting); safe to call twice
# Run:
#   sh ci-lock.sh status        the holder (pid, label, priority, held for) and the waiters in the order they will go
#   sh ci-lock.sh waits [date]  from the history log: per label, how long each run waited and held (default: today)
#
# The lock is $THEODORE_CI_LOGS/locks/matrix (default ~/theodore-ci), a directory taken with mkdir, as before, so a
# runner that predates the queue still respects it. Waiters are ticket files in locks/queue/, named
# <priority>-<enqueued epoch.microseconds>-<pid>, so `sort` gives the order: the higher priority first, then the oldest ticket.
# Only the first ticket may take the lock. A ticket whose process is gone is removed by any waiter; so is a holder
# whose process is gone (and a lock directory left without a pid for over a minute).
#
# Priority (CI_LOCK_PRIORITY): release (0: a release gate, a production deploy) goes ahead of normal (1, the default:
# lanes' checks) and low (2: rehearsals, measurements, exploratory runs). Within a priority, first come first served.
# Waiters poll every 5 s. Each run appends "enqueued acquired released priority pid label" (epoch seconds) to
# locks/history.log, which `waits` reads.

CI_LOCK_LOGS=${THEODORE_CI_LOGS:-$HOME/theodore-ci}
CI_LOCK_DIR=$CI_LOCK_LOGS/locks/matrix
CI_LOCK_QUEUE=$CI_LOCK_LOGS/locks/queue
CI_LOCK_HISTORY=$CI_LOCK_LOGS/locks/history.log
CI_LOCK_TICKET=
CI_LOCK_HELD=
CI_LOCK_ENQUEUED=

ci_lock_priority() {
  case "${CI_LOCK_PRIORITY:-normal}" in
    release | 0) echo 0 ;;
    low | rehearsal | 2) echo 2 ;;
    *) echo 1 ;;
  esac
}
ci_lock_alive() { [ -n "$1" ] && kill -0 "$1" 2>/dev/null; }
ci_lock_field() { sed -n "s/^$1=//p" "$2" 2>/dev/null | head -n 1; }

# Removes the tickets of processes that are gone, and a lock whose holder is gone.
ci_lock_prune() {
  for ticket in "$CI_LOCK_QUEUE"/*; do
    [ -f "$ticket" ] || continue
    ci_lock_alive "$(ci_lock_field pid "$ticket")" || rm -f "$ticket"
  done
  if [ -d "$CI_LOCK_DIR" ]; then
    holder=$(cat "$CI_LOCK_DIR/pid" 2>/dev/null)
    if [ -n "$holder" ]; then
      ci_lock_alive "$holder" || rm -rf "$CI_LOCK_DIR"
    elif [ -n "$(find "$CI_LOCK_DIR" -maxdepth 0 -mmin +1 2>/dev/null)" ]; then
      rm -rf "$CI_LOCK_DIR"
    fi
  fi
}

ci_lock_first() { ls "$CI_LOCK_QUEUE" 2>/dev/null | sort | head -n 1; }

ci_lock_acquire() { # label
  mkdir -p "$CI_LOCK_QUEUE"
  CI_LOCK_ENQUEUED=$(date +%s)
  prio=$(ci_lock_priority)
  # Microseconds in the name order two waiters of the same second (perl is on every runner host; else the pid decides).
  stamp=$(perl -MTime::HiRes=time -e 'printf "%.6f", time' 2>/dev/null || echo "$CI_LOCK_ENQUEUED.000000")
  CI_LOCK_TICKET=$CI_LOCK_QUEUE/$prio-$stamp-$(printf '%07d' "$$")
  printf 'pid=%s\nlabel=%s\nenqueued=%s\npriority=%s\n' "$$" "$1" "$CI_LOCK_ENQUEUED" "$prio" > "$CI_LOCK_TICKET"
  told=0
  while :; do
    ci_lock_prune
    [ -f "$CI_LOCK_TICKET" ] || printf 'pid=%s\nlabel=%s\nenqueued=%s\npriority=%s\n' "$$" "$1" "$CI_LOCK_ENQUEUED" "$prio" > "$CI_LOCK_TICKET"
    if [ "$(ci_lock_first)" = "${CI_LOCK_TICKET##*/}" ] && mkdir "$CI_LOCK_DIR" 2>/dev/null; then
      echo "$$" > "$CI_LOCK_DIR/pid"
      echo "$1" > "$CI_LOCK_DIR/what"
      date +%s > "$CI_LOCK_DIR/since"
      echo "$CI_LOCK_ENQUEUED" > "$CI_LOCK_DIR/enqueued"
      echo "$prio" > "$CI_LOCK_DIR/priority"
      rm -f "$CI_LOCK_TICKET"
      CI_LOCK_HELD=1
      waited=$(( $(date +%s) - CI_LOCK_ENQUEUED ))
      [ "$waited" -lt 5 ] || echo "ci-lock: took the lock after waiting ${waited} s" >&2
      return 0
    fi
    now=$(date +%s)
    if [ $(( now - told )) -ge 60 ]; then
      ahead=$(ls "$CI_LOCK_QUEUE" 2>/dev/null | sort | awk -v me="${CI_LOCK_TICKET##*/}" '$0 == me { print NR - 1; exit }')
      echo "ci-lock: waiting $(( now - CI_LOCK_ENQUEUED )) s; held by $(cat "$CI_LOCK_DIR/pid" 2>/dev/null || echo '?') ($(cat "$CI_LOCK_DIR/what" 2>/dev/null)); ${ahead:-0} ahead (status: sh ci-lock.sh status)" >&2
      told=$now
    fi
    sleep "${CI_LOCK_POLL:-5}"
  done
}

ci_lock_release() {
  [ -z "$CI_LOCK_TICKET" ] || rm -f "$CI_LOCK_TICKET"
  if [ -n "$CI_LOCK_HELD" ] && [ "$(cat "$CI_LOCK_DIR/pid" 2>/dev/null)" = "$$" ]; then
    printf '%s %s %s %s %s %s\n' "$CI_LOCK_ENQUEUED" "$(cat "$CI_LOCK_DIR/since" 2>/dev/null)" "$(date +%s)" \
      "$(cat "$CI_LOCK_DIR/priority" 2>/dev/null)" "$$" "$(cat "$CI_LOCK_DIR/what" 2>/dev/null)" >> "$CI_LOCK_HISTORY"
    rm -rf "$CI_LOCK_DIR"
  fi
  CI_LOCK_HELD=
}

ci_lock_status() {
  ci_lock_prune
  now=$(date +%s)
  names="release normal low"
  if [ -d "$CI_LOCK_DIR" ]; then
    since=$(cat "$CI_LOCK_DIR/since" 2>/dev/null)
    prio=$(cat "$CI_LOCK_DIR/priority" 2>/dev/null)
    if [ -n "$prio" ]; then prio=$(echo $names | cut -d' ' -f$((prio + 1))); else prio="? (an older runner)"; fi
    if [ -n "$since" ]; then held="$(( (now - since) / 60 )) min $(( (now - since) % 60 )) s"; else held="? (an older runner)"; fi
    printf 'holder: pid %s, %s, priority %s, held for %s\n' "$(cat "$CI_LOCK_DIR/pid" 2>/dev/null || echo '?')" \
      "$(cat "$CI_LOCK_DIR/what" 2>/dev/null || echo '?')" "$prio" "$held"
  else
    echo "holder: none (the lock is free)"
  fi
  n=0
  for ticket in $(ls "$CI_LOCK_QUEUE" 2>/dev/null | sort); do
    f=$CI_LOCK_QUEUE/$ticket
    [ -f "$f" ] || continue
    n=$((n + 1))
    prio=$(ci_lock_field priority "$f")
    printf '%2d. pid %s, %s, priority %s, waiting %s min\n' "$n" "$(ci_lock_field pid "$f")" "$(ci_lock_field label "$f")" \
      "$(echo $names | cut -d' ' -f$((prio + 1)))" "$(( (now - $(ci_lock_field enqueued "$f")) / 60 ))"
  done
  [ "$n" -gt 0 ] || echo "waiters: none"
}

ci_lock_waits() { # [YYYY-MM-DD]
  day=${1:-$(date +%Y-%m-%d)}
  [ -f "$CI_LOCK_HISTORY" ] || { echo "no history yet ($CI_LOCK_HISTORY)"; return 0; }
  start=$(date -j -f '%Y-%m-%d %H:%M:%S' "$day 00:00:00" +%s 2>/dev/null || date -d "$day" +%s)
  w=0 h=0
  while read -r enq acq rel prio pid label; do
    [ -n "$acq" ] && [ "$acq" -ge "$start" ] && [ "$acq" -lt $((start + 86400)) ] || continue
    printf '%s  waited %5d s  held %5d s  priority %s  %s\n' "$(date -r "$acq" +%H:%M 2>/dev/null || date -d "@$acq" +%H:%M)" \
      $((acq - enq)) $((rel - acq)) "$prio" "$label"
    w=$((w + acq - enq)) h=$((h + rel - acq))
  done < "$CI_LOCK_HISTORY"
  echo "total: waited $((w / 60)) min, held $((h / 60)) min"
}

case "${0##*/}" in
  ci-lock.sh | lock.sh)
    case "${1:-status}" in
      status) ci_lock_status ;;
      waits) ci_lock_waits "${2:-}" ;;
      *) echo "usage: sh $0 status | waits [YYYY-MM-DD]" >&2; exit 64 ;;
    esac ;;
esac
