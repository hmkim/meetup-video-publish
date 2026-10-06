#!/usr/bin/env bash
# run_plan_only.sh <name> [--force] [--talk-only] [--model <profile-id>] -- stages 1-2 only (transcribe -> plan cuts), no render.
# Use when you want to review plans/<name>.cuts.md before spending on MediaConvert; then run render.py <name>.
# Any flag after <name> is passed to plan_cuts.py; --force is also passed to transcribe.py.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${ZVE_PYTHON:-$HOME/.cache/zoom-video-edit/venv/bin/python}"
NAME_ARG="${1:?usage: run_plan_only.sh <name> [--force] [--talk-only] [--model <id>]}"; shift
FORCE=""
for a in "$@"; do [ "$a" = "--force" ] && FORCE="--force"; done
log() { printf '\n==== [%s] %s ====\n' "$(date +%H:%M:%S)" "$*" >&2; }
KEY="$("$PY" -W ignore -c "import sys; sys.path.insert(0,'$HERE'); import zve_common as C; print(C.resolve_input_key(sys.argv[1]))" "$NAME_ARG")"
log "1/2 transcribe  $KEY"
"$PY" -W ignore "$HERE/transcribe.py" "$KEY" $FORCE
log "2/2 plan cuts (Bedrock Claude) $*"
"$PY" -W ignore "$HERE/plan_cuts.py" "$KEY" "$@"
log "PLAN_DONE"
