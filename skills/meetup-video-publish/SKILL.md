---
name: meetup-video-publish
description: Turn a meetup Zoom recording in S3 into one YouTube video per presenter on AWS (Transcribe diarization -> Bedrock Claude cut plan -> word-timestamp boundary check -> MediaConvert clip -> YouTube Data API upload). Use when asked to "edit the meetup recording", "split the talk video by speaker", "upload the talks to YouTube", or anything mentioning meetup-video-publish / zoom-video-edit. Two human approval gates (cut table, metadata); never render or upload without them.
license: MIT
compatibility: Linux/macOS host with AWS credentials for the bucket's account (S3, Transcribe, MediaConvert, Bedrock), Python venv from requirements.txt, ffmpeg optional. No browser needed on the host; the Google login is a URL round-trip through the user's own browser.
metadata:
  repo: https://github.com/hmkim/meetup-video-publish
  runbook: AGENTS.md (repository root) — this skill is the short form; read AGENTS.md for details
  gates: 1 = cut table approved before render, 2 = titles/descriptions approved before upload
---

# meetup-video-publish

Monthly job: one recording in, N presenter videos out, uploaded private to the channel. Read
`AGENTS.md` at the repository root once per session; it has the exact commands, boundary rules and
the failure table. This file is the checklist.

## Preconditions

- `.env` sourced (`ZVE_BUCKET` required; `ZVE_REGION`, `ZVE_EVENT`, `ZVE_PYTHON` recommended).
- Recording at `s3://$ZVE_BUCKET/input/$NAME.mp4`, `NAME=meetup-YYYY-MM`. If missing, hand the
  user the `aws s3 cp` command; recordings behind a login cannot be fetched by the agent.
- `scripts/preflight.sh $NAME` prints `PREFLIGHT_OK`.
- If the user posted the event announcement, save names/titles/affiliations/date to
  `~/.cache/zoom-video-edit/$NAME.event.json` first.

## Checklist

1. `scripts/run_plan_only.sh $NAME --talk-only` in the background; poll the log for `PLAN_DONE`.
2. Copy `work/$NAME/*` to `~/.cache/zoom-video-edit/`. Read the planner's notes.
3. For every segment start/end: `tools/words_in_range.py <transcript.json> HH:MM:SS-HH:MM:SS`,
   move the boundary to the word (start = first content word −0.3 s, end = last talk word +0.3 s,
   never into the next speaker). Record changes in `_meta.manual_review`; re-upload the plan.
4. **Gate 1** — present the cut table (video, presenter, title, kept ranges, length, removed
   items + why). Stop until approved.
5. `scripts/render.py $NAME --plan <fixed plan> --force` in the background; done when each line
   shows `rendered=` with `diff` < 0.2 s.
6. `scripts/make_meta.py $NAME --date <event date> --event "$ZVE_EVENT"`, then fill every TODO
   from event.json + transcript (title ≤ 100 chars, 5–8 factual bullets, hashtags, real
   `recordingDate`). Ask for any affiliation you cannot find.
7. **Gate 2** — present full titles and descriptions. Stop until approved.
8. `scripts/youtube_upload.py <meta.json>`; confirm `upload_status.json` and that the staging
   dir is empty. If the token is dead: `youtube_auth.py url` → user logs in → `finish '<url>'` →
   `whoami`.
9. Report: YouTube + Studio links, length, "processing, private"; the user's to-dos (playback
   check, visibility, thumbnail). Schedule `scripts/cost_report.py $NAME --from … --to …` for
   two days later with the runtime's scheduler.

## Never

- Render before gate 1, upload before gate 2.
- Trust a planner boundary inside a long speaker turn without the word-timestamp check.
- Print, copy or commit `client_secret.json` / `token.json`, or hard-code a bucket/account/channel.
- Chain `sleep`/`aws … wait` in one shell call; poll with the runtime's wait or monitor tool.
