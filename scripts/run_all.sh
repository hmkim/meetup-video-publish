#!/usr/bin/env bash
# run_all.sh -- transcribe -> plan -> render for one input object. Idempotent: each stage is skipped when
# its S3 artifact already exists (pass --force to redo everything, or delete the artifact you want redone).
#
#   run_all.sh <name>            e.g. run_all.sh meetup-2026-09   (=> input/meetup-2026-09.mp4)
#   run_all.sh input/<name>.mp4
#   run_all.sh <name> --force
#
# Stages / artifacts:
#   1. transcribe.py  -> transcripts/<name>.json, transcripts/<name>.speakers.txt
#   2. plan_cuts.py   -> plans/<name>.cuts.json, plans/<name>.cuts.md
#   3. render.py      -> output/<name>/<NN-presenter>.mp4 (one MediaConvert job per presenter)
set -euo pipefail

BUCKET="${ZVE_BUCKET:?set ZVE_BUCKET to your working bucket (see README)}"
REGION="${ZVE_REGION:-ap-northeast-2}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${ZVE_PYTHON:-$HOME/.cache/zoom-video-edit/venv/bin/python}"

[ -n "${1:-}" ] || { sed -n '2,15p' "$0"; exit 2; }
NAME_ARG="$1"; shift
FORCE=""
[ "${1:-}" = "--force" ] && FORCE="--force"

[ -x "$PY" ] || { echo "python venv missing at $PY -- run: uv venv ~/.cache/zoom-video-edit/venv && uv pip install --python ~/.cache/zoom-video-edit/venv/bin/python boto3" >&2; exit 1; }

log() { printf '\n==== [%s] %s ====\n' "$(date +%H:%M:%S)" "$*" >&2; }

# Resolve object key exactly like zve_common.resolve_input_key
KEY="$("$PY" -W ignore -c "import sys; sys.path.insert(0,'$HERE'); import zve_common as C; print(C.resolve_input_key(sys.argv[1]))" "$NAME_ARG")"
NAME="$(basename "${KEY%.*}")"
aws sts get-caller-identity --region "$REGION" >/dev/null 2>&1 || { echo "no AWS credentials -- configure them first (aws configure / aws sso login / AWS_PROFILE)" >&2; exit 1; }
aws s3api head-object --region "$REGION" --bucket "$BUCKET" --key "$KEY" >/dev/null 2>&1 || { echo "s3://$BUCKET/$KEY not found (run ingest.sh first)" >&2; exit 1; }

log "1/3 transcribe  $KEY"
MEDIA_OPT=()
SIZE="$(aws s3api head-object --region "$REGION" --bucket "$BUCKET" --key "$KEY" --query ContentLength --output text)"
if [ "$SIZE" -gt $((2 * 1024 * 1024 * 1024)) ]; then
  AUDIO_KEY="output/$NAME/audio.m4a"
  aws s3api head-object --region "$REGION" --bucket "$BUCKET" --key "$AUDIO_KEY" >/dev/null 2>&1 \
    || "$PY" -W ignore "$HERE/render.py" "$KEY" --audio-only
  MEDIA_OPT=(--media-key "$AUDIO_KEY")
fi
"$PY" -W ignore "$HERE/transcribe.py" "$KEY" $FORCE "${MEDIA_OPT[@]}"

log "2/3 plan cuts (Bedrock Claude)"
"$PY" -W ignore "$HERE/plan_cuts.py" "$KEY" $FORCE

log "3/3 render (MediaConvert)"
"$PY" -W ignore "$HERE/render.py" "$KEY" $FORCE

log "done. review plan: s3://$BUCKET/plans/$NAME.cuts.md ; outputs: s3://$BUCKET/output/$NAME/"
aws s3 ls "s3://$BUCKET/output/$NAME/" --region "$REGION" --human-readable
