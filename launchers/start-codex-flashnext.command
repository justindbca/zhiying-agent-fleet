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
export PI_MODEL="flashnext/Qwen3.8-Flash-Next-Uncensored-oQ4e-100K-MTP"
export PI_THINKING=off

# Keep --continue/--resume separate from cloud-model sessions.
exec /bin/bash "$PROJECT_DIR/launchers/start-pi-full.command" \
  --session-dir "${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}/sessions/flashnext" "$@"
