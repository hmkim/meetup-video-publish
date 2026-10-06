#!/usr/bin/env python3
"""make_meta.py -- build the YouTube metadata skeleton for youtube_upload.py from a locked cuts plan.

    make_meta.py <name> [--plan <cuts.json>] [--event "My Meetup"] [--date 2026-10-28]
                        [--out ~/.cache/zoom-video-edit/youtube/<name>.meta.json]

Reads presenters (order, name, talk title) from the plan, derives the output file names exactly like
render.py (NN-<slug>.mp4) and writes a meta.json whose title/description/tags are PLACEHOLDERS marked
"TODO" -- the agent fills them from the transcript, the user approves, then youtube_upload.py runs.
Common fields are pre-set: categoryId 28 (과학기술), defaultLanguage ko, recordingDate.
"""
import argparse, json, os, re, sys
sys.path.insert(0, os.path.dirname(__file__))
import zve_common as C  # noqa: E402

BASE = os.path.expanduser("~/.cache/zoom-video-edit")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--plan", help="local cuts.json (default: ~/.cache/zoom-video-edit/<name>.cuts.json, else S3 plans/)")
    ap.add_argument("--event", default=os.environ.get("ZVE_EVENT", "My Meetup"), help="event name used in titles/descriptions (or ZVE_EVENT)")
    ap.add_argument("--date", help="recording date YYYY-MM-DD (default: from name like meetup-2026-09 -> 2026-09-01 guess, flagged TODO)")
    ap.add_argument("--out")
    a = ap.parse_args()

    plan_path = a.plan or os.path.join(BASE, f"{a.name}.cuts.json")
    if not os.path.exists(plan_path):
        import subprocess
        subprocess.run(["aws", "s3", "cp", f"s3://{C.BUCKET}/plans/{a.name}.cuts.json", plan_path,
                        "--region", C.REGION, "--only-show-errors"], check=True)
    plan = json.load(open(plan_path))

    date = a.date
    date_todo = False
    if not date:
        m = re.search(r"(\d{4})-(\d{2})", a.name)
        date = f"{m.group(1)}-{m.group(2)}-01" if m else "TODO-YYYY-MM-DD"
        date_todo = True
    ym = date[:7].replace("-", ".")

    videos = []
    for i, p in enumerate(plan["presenters"], 1):
        who = p.get("inferred_name_or_null") or f"presenter-{i}"
        talk = p.get("talk_title_or_null") or "TODO 발표 제목"
        base = f"{i:02d}-{C.slugify(who)}"
        total = sum(C.parse_ts(s["end"]) - C.parse_ts(s["start"]) for s in p["segments"])
        videos.append({
            "file": f"{base}.mp4",
            "_presenter": who, "_talk": talk, "_duration_s": round(total, 1),
            "title": f"TODO {talk} | {who} (TODO 소속) — {a.event} {ym}",
            "description": f"TODO {date[:4]}년 {int(date[5:7])}월 {('TODO일' if date_todo else str(int(date[8:10]))+'일')} {a.event}(온라인) {i}부 발표입니다.\n\n"
                           f"발표자: {who} — TODO 소속\n주제: {talk}\n\nTODO 발표 내용 요약 (전사 기반, 5~8개 항목)\n\n"
                           f"발표 내용만 편집, Q&A 제외. #TODO해시태그 #AWS",
            "tags": ["TODO", "AWS"],
            "categoryId": "28",
            "defaultLanguage": "ko",
            "recordingDate": date,
        })

    meta = {"s3_prefix": f"s3://{C.BUCKET}/output/{a.name}/", "_event": a.event,
            "_note": "Fill every TODO, delete keys starting with '_' if you like (uploader ignores them), get user approval, then youtube_upload.py",
            "videos": videos}
    out = a.out or os.path.join(BASE, "youtube", f"{a.name}.meta.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if os.path.exists(out):
        sys.exit(f"{out} exists -- edit it or pass --out")
    json.dump(meta, open(out, "w"), ensure_ascii=False, indent=2)
    print(out)
    for v in videos:
        print(f"  {v['file']}  {v['_presenter']}  {v['_talk']}  {v['_duration_s']}s")
    if date_todo:
        print("  NOTE: recordingDate guessed from name -> set --date or fix in the file")


if __name__ == "__main__":
    main()
