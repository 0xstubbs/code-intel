#!/usr/bin/env bash
# install-hook.sh <target-name> — install the codeintel post-commit refresh
# hook into the target repo's .git/hooks/. Refuses to clobber a foreign hook.
set -eu
TARGET="${1:?usage: install-hook.sh <target-name>}"
CI_ROOT="/home/hermes/code-intel"
ROOT=$(cd "$CI_ROOT" && uv run python -c "
from codeintel.config import load_target_by_name
from codeintel import TARGETS_DIR
print(load_target_by_name('$TARGET', TARGETS_DIR).root)")
HOOK_DIR="$ROOT/.git/hooks"
HOOK="$HOOK_DIR/post-commit"
MARK="# codeintel-refresh"

[ -d "$HOOK_DIR" ] || { echo "error: $ROOT is not a git repo"; exit 1; }
if [ -f "$HOOK" ] && ! grep -q "$MARK" "$HOOK"; then
  echo "error: $HOOK exists and is not ours — append manually:"
  echo "  $CI_ROOT/scripts/post-commit-refresh.sh $TARGET $MARK"
  exit 1
fi
cat > "$HOOK" <<EOF
#!/usr/bin/env bash
$MARK — keep the codeintel graph current after every commit (non-blocking)
$CI_ROOT/scripts/post-commit-refresh.sh $TARGET
EOF
chmod +x "$HOOK"
echo "installed: $HOOK -> refresh target '$TARGET'"
