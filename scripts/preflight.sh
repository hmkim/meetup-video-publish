#!/usr/bin/env bash
# preflight.sh [<name>] -- verify everything a monthly run needs, print a status table, exit 1 on any FAIL.
#   checks: AWS creds (optionally pinned to ZVE_ACCOUNT) · S3 bucket · input/<name>.mp4 · python venv (boto3, google libs)
#           · ffmpeg (fps detect) · Bedrock Opus probe · YouTube token (refresh + channel)
#   YouTube check is skipped with --no-youtube (e.g. render-only runs).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUCKET="${ZVE_BUCKET:?set ZVE_BUCKET to your working bucket (see README)}"
REGION="${ZVE_REGION:-ap-northeast-2}"
EXPECT_ACCT="${ZVE_ACCOUNT:-}"   # optional: fail if credentials belong to another account
PY="${ZVE_PYTHON:-$HOME/.cache/zoom-video-edit/venv/bin/python}"
NAME=""; YT=1
for a in "$@"; do case "$a" in --no-youtube) YT=0;; *) NAME="$a";; esac; done
FAIL=0
row() { printf '%-22s %-6s %s\n' "$1" "$2" "$3"; [ "$2" = "FAIL" ] && FAIL=1; }

ACCT="$(aws sts get-caller-identity --query Account --output text --region "$REGION" 2>/dev/null)"
CREDS_OK=0
if [ -z "$ACCT" ]; then row "aws-creds" FAIL "no credentials -> aws configure / aws sso login / AWS_PROFILE";
elif [ -n "$EXPECT_ACCT" ] && [ "$ACCT" != "$EXPECT_ACCT" ]; then row "aws-creds" FAIL "credentials belong to account $ACCT, expected ZVE_ACCOUNT=$EXPECT_ACCT";
else row "aws-creds" OK "account $ACCT"; CREDS_OK=1; fi

if [ "$CREDS_OK" != 1 ]; then
  row "s3-bucket" SKIP "needs credentials"; row "input" SKIP "needs credentials"
elif aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null 2>&1; then row "s3-bucket" OK "$BUCKET"; else row "s3-bucket" FAIL "$BUCKET missing -> see README, Setup (SSE-S3, block public access, 30d expiry)"; fi

if [ "$CREDS_OK" != 1 ]; then :
elif [ -n "$NAME" ]; then
  SZ="$(aws s3api head-object --bucket "$BUCKET" --key "input/$NAME.mp4" --region "$REGION" --query ContentLength --output text 2>/dev/null)"
  if [ -n "$SZ" ]; then row "input" OK "input/$NAME.mp4 $((SZ/1048576)) MiB"; else row "input" FAIL "s3://$BUCKET/input/$NAME.mp4 not found (user uploads: aws s3 cp <file> s3://$BUCKET/input/$NAME.mp4 --region $REGION)"; fi
  OUT="$(aws s3 ls "s3://$BUCKET/output/$NAME/" --region "$REGION" 2>/dev/null | grep -c '\.mp4$')"
  [ "${OUT:-0}" -gt 0 ] && row "output-exists" WARN "output/$NAME/ already has $OUT mp4 (render.py will skip unless --force)"
else
  row "input" SKIP "no <name> given"
fi

if [ -x "$PY" ] && "$PY" -W ignore -c "import boto3, googleapiclient, google_auth_oauthlib" 2>/dev/null; then row "python-venv" OK "$PY"; else row "python-venv" FAIL "uv venv ~/.cache/zoom-video-edit/venv && uv pip install --python $PY boto3 google-api-python-client google-auth-oauthlib"; fi

if [ -x "$HOME/.cache/zoom-video-edit/ffmpeg" ]; then row "ffmpeg" OK "fps auto-detect enabled"; else row "ffmpeg" WARN "missing -> render.py assumes 30fps; pass --fps or install (README, Setup)"; fi

if [ "$CREDS_OK" = 1 ]; then
  if "$PY" -W ignore "$HERE/plan_cuts.py" --probe 2>/dev/null | grep -qi "opus"; then row "bedrock-opus" OK "Claude Opus answers"; else row "bedrock-opus" WARN "Opus probe failed; plan_cuts.py falls back to Sonnet (check --probe output)"; fi
fi

if [ "$YT" = 1 ]; then
  if [ -f "$HOME/.cache/zoom-video-edit/youtube/token.json" ]; then
    WHO="$("$PY" -W ignore "$HERE/youtube_auth.py" whoami 2>&1 | grep '^channel:')"
    if [ -n "$WHO" ]; then row "youtube-token" OK "$WHO"; else row "youtube-token" FAIL "token expired/revoked (Testing-mode OAuth refresh tokens die after 7 days) -> youtube_auth.py url -> laptop login -> finish '<redirect-url>'"; fi
  else
    row "youtube-token" FAIL "no token.json -> youtube_auth.py url (needs client_secret.json)"
  fi
fi

echo; [ "$FAIL" = 0 ] && echo "PREFLIGHT_OK" || { echo "PREFLIGHT_FAILED"; exit 1; }
