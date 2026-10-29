# AGENTS.md — runbook for AI coding agents

This file is the operating contract for an agent (Codex, Claude Code, Kiro / Kiro Crew, or any
tool that reads `AGENTS.md`) asked to turn a meetup recording into per-speaker YouTube videos
with this repository. `CLAUDE.md` imports it; `skills/meetup-video-publish/SKILL.md` is the
same procedure in Agent Skills form. Humans: read `README.md` first.

Everything below assumes `.env` is filled in (`cp .env.example .env`) and sourced, and that
`ZVE_PYTHON` points at a venv with `requirements.txt` installed. `$NAME` is the run name,
`meetup-YYYY-MM` by convention (letters, digits, `-` only); every S3 key and local file uses it.

## Non-negotiables

1. **Two human approvals per run.** Never start a render before the user approves the cut table
   (gate 1), and never upload before the user approves titles and descriptions (gate 2). Everything
   else — transcribe, plan, verify, draft — is yours to do without asking.
2. **Verify every boundary against word timestamps.** The planner (a language model) finds the
   right speaker turns reliably but places boundaries *inside* long turns by estimating from text
   length; in practice it is off by 3–10 s. Check each segment start/end with
   `tools/words_in_range.py` and move it to the word. This step is part of the pipeline, not a
   review nicety.
3. **No media on the agent host except the upload staging copy.** Transcribe, plan and render run
   on AWS against S3; `youtube_upload.py` stages the finished MP4s locally and deletes them.
4. **Secrets stay where they are.** `~/.cache/zoom-video-edit/youtube/{client_secret,token}.json`
   are 0600; never print, copy or commit them. Do not put a bucket name, account ID or video ID
   that identifies a private channel into files tracked by git.
5. **Public-safe by default.** If you change a script, keep the environment-variable contract
   (`ZVE_*`) and do not hard-code a region, bucket, account or channel.

## The run, step by step

```
user:  Zoom MP4  --aws s3 cp-->  s3://$ZVE_BUCKET/input/$NAME.mp4
agent: preflight -> run_plan_only --talk-only -> verify + fix boundaries -> [GATE 1: cut table]
       -> render -> make_meta -> write titles/descriptions -> [GATE 2: metadata] -> youtube_upload
user:  check processed video in YouTube Studio -> set visibility -> thumbnail
agent: two days later: cost_report -> costs/ledger.md
```

### 0. Preflight

```bash
scripts/preflight.sh $NAME
```
Proceed only on `PREFLIGHT_OK`. Each FAIL row prints its fix. The two you will meet:

- `input FAIL` — the recording is not in S3. You cannot fetch a Zoom recording that requires
  login; give the user the exact `aws s3 cp <file> s3://$ZVE_BUCKET/input/$NAME.mp4 --region $ZVE_REGION`
  command and wait. Do not keep probing Zoom.
- `youtube-token FAIL` — the Google refresh token expired (OAuth apps in "Testing" status expire
  tokens after 7 days). Run the login round-trip in §5. Suggest, once, that the user move the app to
  "In production" in Google Cloud Console to get a durable token; never do it for them.

If the user gives you the event announcement (names, titles, affiliations, date), save it as
`~/.cache/zoom-video-edit/$NAME.event.json` before planning: it feeds filenames and metadata and
lets you catch a mis-spelled name in the transcript (diarization-based name inference reads the
name as pronounced, which is not always how it is written).

### 1. Transcribe and plan (stops before render)

```bash
nohup scripts/run_plan_only.sh $NAME --talk-only > ~/.cache/zoom-video-edit/logs/$NAME.plan.log 2>&1 &
```
Poll the log every few minutes (use your runtime's wait/monitor facility, not a shell `sleep`
chain) until it prints `PLAN_DONE`. A 60-minute recording transcribes in ~10 min.

- `--talk-only` keeps only the presenter's own talk: every Q&A exchange, host introduction,
  hand-over and closing is dropped; sub-3-second back-channel ("yes", "right") is kept. Omit the
  flag to keep each talk's own Q&A.
- Outputs: `s3://$ZVE_BUCKET/plans/$NAME.cuts.json` + `.cuts.md`, transcript at
  `transcripts/$NAME.json` and `transcripts/$NAME.speakers.txt`, local copies under
  `work/$NAME/`. Copy them to `~/.cache/zoom-video-edit/` for review:
  ```bash
  cp work/$NAME/$NAME.{cuts.json,json,speakers.txt} ~/.cache/zoom-video-edit/
  ```
- Read `_meta`/`notes` in the plan. The planner says when it estimated a boundary ("by text
  ratio", "check the waveform"); treat those as certainly wrong by a few seconds.

### 2. Verify and fix boundaries (always)

```bash
# speaker turns, to see where talks start/end and who interrupts
cut -c1-160 ~/.cache/zoom-video-edit/$NAME.speakers.txt
# word-level timestamps in a window: HH:MM:SS-HH:MM:SS (one argument per window)
tools/words_in_range.py ~/.cache/zoom-video-edit/$NAME.json 00:03:05-00:03:20 00:20:10-00:20:25
# silences over 15 s (break / screen-share trouble candidates)
tools/inspect_cuts.py ~/.cache/zoom-video-edit/$NAME.json --gaps 15
```
If the window printout is too coarse, read `results.items[]` of the transcript JSON directly: each
pronunciation item has `start_time`, `end_time`, `alternatives[0].content`.

Rules for the final boundary:
- **start** = first word of the presenter's first content sentence ("Hello, I'm …", "Let me
  start") **−0.3 s**; exclude "can you see my screen?", "is it recording?", the host's "go ahead".
- **end** = last word of the last talk sentence ("… thank you") **+0.3 s**, but not into the next
  speaker's first word; exclude housekeeping that follows (survey QR, next-event notice, "can we
  stop the recording?").
- A presenter's own addendum (a tip, an extra slide) is talk content: keep it.
- To drop something in the middle, split `segments`; the renderer concatenates the list in order
  and reads nothing else.
- Write what you changed into `_meta.manual_review` (old → new, why), save, and re-upload:
  `aws s3 cp ~/.cache/zoom-video-edit/$NAME.cuts.json s3://$ZVE_BUCKET/plans/$NAME.cuts.json`.
  Keep the planner's original as `$NAME.cuts.<model>-draft.json`.

**GATE 1.** Show the user a table — video, presenter, title, kept range(s), length, what was
removed and why — and the planner's quality notes. Wait for approval. Edits the user asks for go
into the plan file, then show the table again.

### 3. Render

```bash
nohup $ZVE_PYTHON scripts/render.py $NAME --plan ~/.cache/zoom-video-edit/$NAME.cuts.json --force \
  > ~/.cache/zoom-video-edit/logs/$NAME.render.log 2>&1 &
```
One MediaConvert job per presenter, sequential; ~8 min per 20 output minutes at 2880-wide. Poll the
log. Done when each presenter line reports `rendered=` with `diff` under 0.2 s. Re-render a single
presenter with `--only spk_N`. Credentials from short-lived vending expire; re-vend before this
step if the plan phase took long.

### 4. Titles and descriptions

```bash
$ZVE_PYTHON scripts/make_meta.py $NAME --date YYYY-MM-DD --event "$ZVE_EVENT"
```
This writes `~/.cache/zoom-video-edit/youtube/$NAME.meta.json` with a `TODO` skeleton per video.
Fill it from `$NAME.event.json` and the transcript:

- title: `<talk title> | <presenter> (<affiliation>) — <event> YYYY.MM`, **≤ 100 characters**
  (count them; CJK titles run long).
- description: one line event + part, presenter + affiliation + topic, 5–8 bullet summary of what
  was actually said (transcript-based, no claims the speaker did not make), then the edit note
  ("talk only, Q&A removed") and 5–7 hashtags.
- tags 6–10, `categoryId` 28 (Science & Technology) unless the event is something else,
  `defaultLanguage`, `recordingDate` = the real event date.
- Ask the user for an affiliation you cannot find; never guess one.

**GATE 2.** Show the full titles and descriptions. Wait for approval; apply edits to the JSON and
show a diff.

### 5. Upload

```bash
$ZVE_PYTHON scripts/youtube_upload.py ~/.cache/zoom-video-edit/youtube/$NAME.meta.json
cat ~/.cache/zoom-video-edit/youtube/upload_status.json
```
Both videos upload in parallel (resumable, 16 MB chunks; ~20 s per 0.5 GB). Confirm
`uploadStatus: uploaded` for each and that `~/.cache/zoom-video-edit/youtube/media/` is empty.

If `preflight` flagged the token, do the login round-trip first. There is no browser on the agent
host; the user does the browser part:
```bash
$ZVE_PYTHON scripts/youtube_auth.py url        # print the Google consent URL -> give it to the user
# user logs in on their laptop, lands on the registered redirect URI (an error page there is fine)
$ZVE_PYTHON scripts/youtube_auth.py finish '<the full redirect URL the user pastes back>'
$ZVE_PYTHON scripts/youtube_auth.py whoami     # must print the intended channel
```
A "Web application" OAuth client only accepts its registered redirect URIs; set `YT_REDIRECT` to
one of them. A "Desktop app" client accepts `http://localhost:<port>/`.

To fix the title/description of an already-uploaded video (a wrong name, a typo):
`scripts/youtube_update_meta.py <videoId> --replace OLD NEW --dry-run`, then without `--dry-run`.
This needs the `youtube.force-ssl` scope, which `youtube_auth.SCOPES` now requests; a token issued
before that scope was added has to be re-issued once.

### 6. Report, then cost

Report a table: video | `https://youtu.be/<id>` | `https://studio.youtube.com/video/<id>/edit` |
length | status (processing, private). Tell the user what is theirs to do: check playback once
YouTube finishes processing, set visibility (uploads from an unverified OAuth app are forced to
private and cannot be changed through the API), thumbnail, playlist. Mention the S3 lifecycle
(objects expire; give a download command if they want to keep the masters).

Two days later (Cost Explorer lags ~24 h, and a new tag value can take a day to appear):
```bash
$ZVE_PYTHON scripts/cost_report.py $NAME --from <input upload date> --to <upload date> --note "…"
```
If `days` in the output lacks the last working day, record it as partial and re-run a day later.
Schedule this with whatever your runtime offers (a one-shot cron, a reminder); do not leave it to
memory.

## Things that have gone wrong before

| Symptom | Cause → what to do |
|---|---|
| Bedrock `ValidationException … temperature` | The model rejects `temperature` → `plan_cuts.py` retries without it; keep that path if you add models |
| Plan JSON fails to parse, first block is `reasoning` | Reasoning models prepend thinking blocks → `plan_cuts.py` collects `text` blocks only |
| Two presenters under one `spk_N` | Diarization merged them → find the hand-over in `speakers.txt`, split `presenters` by hand |
| Host is also a presenter | Normal; the planner handles it, but check the host's talk starts after their own hand-over chatter |
| `words_in_range.py` "not enough values to unpack" | Window syntax is `HH:MM:SS-HH:MM:SS` in one argument, not two arguments |
| `inspect_cuts.py` `KeyError: 'results'` | It takes the **transcript** JSON, not the cuts JSON |
| Plan files not under `~/.cache/zoom-video-edit/` | `run_plan_only.sh` writes to `work/$NAME/`; copy them (step 1) |
| `youtube_update_meta.py` 403 insufficientPermissions | Token lacks `youtube.force-ssl` → re-run `youtube_auth.py url/finish` |
| Presigned URL dies after minutes | Its life is bound to the vended credentials → give an `aws s3 cp` command instead |
| MediaConvert bill 3× the estimate | Output above 1080p is the UHD tier (~$0.056/output-min vs ~$0.017) → pass a 1920-wide output if the HD rate matters |
| Long `aws … wait` / `sleep` chains killed | Tool watchdogs → poll with your runtime's wait/monitor facility |
| Approval prompt expired while unattended | Stop issuing commands, write state to a file/message, end the turn |

## Working on the code

- Scripts share `scripts/zve_common.py` (env, S3 helpers, tagging). Add new settings there and to
  `.env.example`.
- Keep stages idempotent: an existing S3 artifact is reused unless `--force`.
- Before committing, grep the tree for anything account-, bucket- or channel-specific; the public
  repo must run from `.env` alone.
