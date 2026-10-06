#!/usr/bin/env bash
# ingest.sh -- get the meetup recording into s3://<bucket>/input/<name>.mp4
#
#   ingest.sh zoom  <url-file> [name]   stream the Zoom cloud recording via yt-dlp straight to S3
#   ingest.sh local <path>     [name]   upload a local MP4
#   ingest.sh audio <name>              (>2 GB inputs) make output/<name>/audio.m4a with MediaConvert
#
# <url-file> = a text file with the Zoom recording URL (and passcode via yt-dlp's --video-password if
# needed: put it in ZVE_ZOOM_PASSCODE). Zoom recordings behind an organization SSO wall need the browser cookies:
# set ZVE_COOKIES=/path/to/cookies.txt (Netscape format) and it is passed to yt-dlp.
set -euo pipefail

BUCKET="${ZVE_BUCKET:?set ZVE_BUCKET to your working bucket (see README)}"
REGION="${ZVE_REGION:-ap-northeast-2}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${ZVE_PYTHON:-$HOME/.cache/zoom-video-edit/venv/bin/python}"
TWO_GB=$((2 * 1024 * 1024 * 1024))

log() { printf '[ingest] %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

sanitize() { # file name -> safe object name (no spaces / odd chars)
  printf '%s' "$1" | sed -E 's/\.[A-Za-z0-9]+$//; s/[^A-Za-z0-9._가-힣-]+/-/g; s/^-+|-+$//g' | cut -c1-80
}

s3_put_tag() {
  aws s3api put-object-tagging --region "$REGION" --bucket "$BUCKET" --key "$1" \
    --tagging "TagSet=[{Key=Project,Value=${ZVE_PROJECT_TAG:-zoom-video-edit}}]" >/dev/null
}

s3_size() { aws s3api head-object --region "$REGION" --bucket "$BUCKET" --key "$1" --query ContentLength --output text; }

check_size_hint() {
  local key="$1" size
  size="$(s3_size "$key")"
  log "uploaded s3://$BUCKET/$key ($((size / 1024 / 1024)) MiB)"
  if [ "$size" -gt "$TWO_GB" ]; then
    log "file exceeds Transcribe's 2 GB limit -> creating audio-only track"
    "$0" audio "$(basename "${key%.*}")"
  fi
}

cmd_zoom() {
  local urlfile="$1" name="${2:-}"
  [ -f "$urlfile" ] || die "url file not found: $urlfile"
  local -a ydl=(uvx yt-dlp -a "$urlfile" --no-playlist)
  [ -n "${ZVE_COOKIES:-}" ] && ydl+=(--cookies "$ZVE_COOKIES")
  [ -n "${ZVE_ZOOM_PASSCODE:-}" ] && ydl+=(--video-password "$ZVE_ZOOM_PASSCODE")

  log "listing formats"
  "${ydl[@]}" -F | tee /dev/stderr > "${TMPDIR:-/tmp}/zve-formats.txt"
  if [ -z "$name" ]; then
    name="$(sanitize "$("${ydl[@]}" --print '%(title)s' 2>/dev/null | head -1)")"
    [ -n "$name" ] || name="zoom-$(date +%Y%m%d-%H%M%S)"
  fi
  # Zoom exposes separate views (shared_screen_with_speaker_view, active_speaker, gallery...). Prefer
  # the shared-screen+speaker view, then the highest-resolution mp4 that carries both video and audio.
  local fmt
  fmt="$(awk 'tolower($0) ~ /shared.?screen.*speaker/ && /mp4/ {print $1; exit}' "${TMPDIR:-/tmp}/zve-formats.txt")"
  if [ -n "$fmt" ]; then
    log "using shared-screen+speaker format id $fmt"
  else
    fmt='bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best'
    log "no shared-screen view listed; using $fmt"
  fi

  local key="input/$name.mp4"
  log "streaming to s3://$BUCKET/$key"
  "${ydl[@]}" -f "$fmt" --merge-output-format mp4 -o - \
    | aws s3 cp - "s3://$BUCKET/$key" --region "$REGION" --content-type video/mp4 --expected-size 8000000000
  s3_put_tag "$key"

  # Best effort extras: audio-only rendition and any subtitle/transcript Zoom exposes.
  if grep -qiE 'audio only|m4a' "${TMPDIR:-/tmp}/zve-formats.txt"; then
    log "audio-only rendition detected; streaming to input/$name.audio.m4a"
    "${ydl[@]}" -f 'bestaudio[ext=m4a]/bestaudio' -o - \
      | aws s3 cp - "s3://$BUCKET/input/$name.audio.m4a" --region "$REGION" --content-type audio/mp4 --expected-size 1000000000 \
      && s3_put_tag "input/$name.audio.m4a" || log "audio-only rendition skipped"
  fi
  local subdir="${TMPDIR:-/tmp}/zve-subs-$$"
  mkdir -p "$subdir"
  if "${ydl[@]}" --skip-download --write-subs --write-auto-subs --sub-format vtt -o "$subdir/$name" >/dev/null 2>&1; then
    for f in "$subdir"/*.vtt; do
      [ -e "$f" ] || continue
      aws s3 cp "$f" "s3://$BUCKET/input/$name.$(basename "$f" | sed -E 's/^.*\.([a-zA-Z-]+)\.vtt$/\1/').vtt" --region "$REGION" --only-show-errors
      log "uploaded Zoom transcript $(basename "$f")"
    done
  fi
  rm -rf "$subdir"
  check_size_hint "$key"
  echo "$name"
}

cmd_local() {
  local path="$1" name="${2:-}"
  [ -f "$path" ] || die "file not found: $path"
  [ -n "$name" ] || name="$(sanitize "$(basename "$path")")"
  local key="input/$name.mp4"
  aws s3 cp "$path" "s3://$BUCKET/$key" --region "$REGION" --content-type video/mp4 --only-show-errors
  s3_put_tag "$key"
  check_size_hint "$key"
  echo "$name"
}

cmd_audio() {
  local name="$1"
  [ -x "$PY" ] || die "python venv missing: $PY (see README)"
  "$PY" -W ignore "$HERE/render.py" "$name" --audio-only
}

case "${1:-}" in
  zoom)  shift; cmd_zoom "$@" ;;
  local) shift; cmd_local "$@" ;;
  audio) shift; cmd_audio "$@" ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac
