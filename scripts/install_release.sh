#!/bin/bash
# Install one cmux-fleet release on the target machine.
#   scripts/install_release.sh <sha> <tarball from `git archive <sha>`> [--test] [--activate]
# Steps: unpack into $RELEASES/<sha> (never overwrite an existing release) -> .venv with the pinned packages
# -> deps.yaml check (fail = stop) -> optional full test suite (fail = stop) -> private Redis runtime
# dirs owner-only (700; start refuses anything else) -> optional: point ~/cmux-fleet-current at it.
set -euo pipefail
SHA="${1:?sha}"; TARBALL="${2:?tarball}"; shift 2
TEST=0; ACTIVATE=0
for a in "$@"; do case "$a" in --test) TEST=1 ;; --activate) ACTIVATE=1 ;; *) echo "unknown option $a" >&2; exit 2 ;; esac; done
RELEASES="${CMUX_FLEET_RELEASES:-$HOME/cmux-fleet-releases}"
PY="${CMUX_FLEET_PYTHON:-/opt/homebrew/bin/python3.14}"
REL="$RELEASES/$SHA"
export PATH="/opt/homebrew/opt/redis/bin:$HOME/cmux-fleet/npm/bin:/Applications/cmux.app/Contents/Resources/bin:/opt/homebrew/bin:$PATH"
[ -e "$REL" ] && { echo "release exists, not touched: $REL" >&2; exit 1; }
mkdir -p "$REL" && tar xzf "$TARBALL" -C "$REL"
cd "$REL"
"$PY" -m venv .venv
.venv/bin/pip install -q -r requirements-dev.txt
.venv/bin/pip install -q --no-deps -e .
.venv/bin/python scripts/check_deps.py || { echo "deps.yaml not satisfied on this machine: not releasing" >&2; exit 1; }
if [ "$TEST" = 1 ]; then
  .venv/bin/python -m pytest -q -p no:cacheprovider tests > "$REL/release-tests.log" 2>&1 \
    || { tail -5 "$REL/release-tests.log" >&2; echo "full suite failed: not releasing" >&2; exit 1; }
  tail -1 "$REL/release-tests.log"
fi
# The suite (and any earlier run) may have created these with default permissions; start refuses them.
mkdir -p .runtime/redis && chmod 700 .runtime .runtime/redis
if [ "$ACTIVATE" = 1 ]; then ln -sfn "$REL" "$HOME/cmux-fleet-current"; echo "current -> $(readlink "$HOME/cmux-fleet-current")"; fi
echo "release ready: $REL"
