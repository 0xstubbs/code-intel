#!/usr/bin/env bash
# post-commit-refresh.sh <target-name>
# Called from a target repo's .git/hooks/post-commit. Refreshes the codeintel
# graph (and map, on drift) in a fully detached background job so commits are
# never blocked. Debounced: overlapping invocations coalesce into one trailing
# re-run, so commit bursts end with a current graph.
set -u
TARGET="${1:?usage: post-commit-refresh.sh <target-name>}"
CI_ROOT="/home/hermes/code-intel"
OUT="$CI_ROOT/out/$TARGET"
LOCK="$OUT/.refresh-lock"
PENDING="$OUT/.refresh-pending"
LOG="$OUT/refresh-hook.log"

mkdir -p "$OUT"

_worker() {
  cd "$CI_ROOT" || exit 0
  while :; do
    rm -f "$PENDING"
    {
      echo "=== $(date -u +%FT%TZ) post-commit refresh ($TARGET)"
      if uv run codeintel refresh "$TARGET" --check; then
        echo "no drift"
      else
        uv run codeintel map "$TARGET" && echo "drift -> map regenerated"
      fi
    } >> "$LOG" 2>&1
    # trailing-edge debounce: commits that arrived mid-run queued a pending marker
    [ -e "$PENDING" ] || break
  done
  rmdir "$LOCK" 2>/dev/null
  # keep the log bounded
  [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 1048576 ] && tail -c 262144 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
}

if mkdir "$LOCK" 2>/dev/null; then
  setsid bash -c "$(declare -f _worker); TARGET='$TARGET' CI_ROOT='$CI_ROOT' OUT='$OUT' LOCK='$LOCK' PENDING='$PENDING' LOG='$LOG' _worker" >/dev/null 2>&1 &
else
  touch "$PENDING"   # a refresh is running; ask it to go once more when done
fi
exit 0
