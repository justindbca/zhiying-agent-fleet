#!/bin/bash
# Start Flash Next in pi; keep this filename for existing desktop shortcuts.
# Provider configuration: ~/.pi/agent/models.json (flashnext, OpenAI chat API).
# The old Codex/tmux launcher is preserved beside this file for rollback.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LEGACY="/Users/justin/Documents/projects/us-stock-option/start-codex-flashnext.command.bak-before-pi"

# Existing pair/long-task commands still belong to the Codex orchestrator.
case "${1:-}" in
  reset-all|new-task|--long)
    exec /bin/bash "$LEGACY" "$@"
    ;;
  --legacy-codex)
    shift
    exec /bin/bash "$LEGACY" "$@"
    ;;
esac

# Do not inherit the calling pi session's PI_MODEL or the cloud Codex default.
# Model id comes from the flashnext provider in models.json, so swapping the
# served model only needs one edit there (the old hardcoded oQ4e id went stale).
PI_MODELS_JSON="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}/models.json"
FLASHNEXT_ID="$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["providers"]["flashnext"]["models"][0]["id"])' "$PI_MODELS_JSON" 2>/dev/null || true)"
[ -n "$FLASHNEXT_ID" ] || { echo "ERROR: $PI_MODELS_JSON 里没有 flashnext provider 的模型" >&2; exit 1; }
export PI_MODEL="flashnext/$FLASHNEXT_ID"
export PI_THINKING=off

# Keep --continue/--resume separate from cloud-model sessions.
exec /bin/bash "$PROJECT_DIR/launchers/start-pi-full.command" \
  --session-dir "${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}/sessions/flashnext" "$@"
