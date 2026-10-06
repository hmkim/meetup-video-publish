#!/usr/bin/env python3
"""Upload rendered clips to YouTube (resumable, parallel).

    youtube_upload.py <metadata.json> [--privacy private|unlisted|public] [--only 01]

metadata.json:
{
  "s3_prefix": "s3://bucket/output/meetup-2026-09/",
  "videos": [
    {"file": "01-presenter-name.mp4", "title": "...", "description": "...", "tags": ["..."],
     "categoryId": "28", "defaultLanguage": "ko", "recordingDate": "2026-09-29"}
  ]
}
Files are staged under ~/.cache/zoom-video-edit/youtube/media/ (deleted after a successful upload).
Progress + result (video id / URL) is written to ~/.cache/zoom-video-edit/youtube/upload_status.json.

NOTE: videos uploaded through an OAuth project that has not passed Google's API audit are
forced to PRIVATE by YouTube regardless of the requested privacyStatus; publish from Studio.
"""
import json, os, subprocess, sys, threading, time, warnings
warnings.filterwarnings("ignore", category=FutureWarning)
sys.path.insert(0, os.path.dirname(__file__))
from youtube_auth import load_creds, BASE  # noqa: E402

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

MEDIA = os.path.join(BASE, "media")
STATUS = os.path.join(BASE, "upload_status.json")
_lock = threading.Lock()
_status = {}


def set_status(name, **kw):
    with _lock:
        _status.setdefault(name, {}).update(kw, updated=time.strftime("%H:%M:%S"))
        tmp = STATUS + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_status, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATUS)


def stage(s3_prefix, name):
    os.makedirs(MEDIA, exist_ok=True)
    local = os.path.join(MEDIA, name)
    if not os.path.exists(local):
        set_status(name, phase="download")
        subprocess.run(["aws", "s3", "cp", s3_prefix + name, local, "--region", os.environ.get("ZVE_REGION", "ap-northeast-2"), "--only-show-errors"], check=True)
    set_status(name, phase="staged", bytes=os.path.getsize(local))
    return local


def upload_one(creds, s3_prefix, v, privacy):
    name = v["file"]
    try:
        local = stage(s3_prefix, name)
        yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
        body = {
            "snippet": {
                "title": v["title"], "description": v["description"], "tags": v.get("tags", []),
                "categoryId": v.get("categoryId", "28"), "defaultLanguage": v.get("defaultLanguage", "ko"),
                "defaultAudioLanguage": v.get("defaultLanguage", "ko"),
            },
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False, "embeddable": True},
        }
        if v.get("recordingDate"):
            body["recordingDetails"] = {"recordingDate": v["recordingDate"] + "T00:00:00Z"}
        media = MediaFileUpload(local, chunksize=16 * 1024 * 1024, resumable=True, mimetype="video/mp4")
        req = yt.videos().insert(part=",".join(body.keys()), body=body, media_body=media)
        resp, backoff = None, 1
        while resp is None:
            try:
                st, resp = req.next_chunk()
                if st:
                    set_status(name, phase="upload", pct=round(st.progress() * 100, 1))
                backoff = 1
            except HttpError as e:
                if e.resp.status in (500, 502, 503, 504) and backoff < 300:
                    set_status(name, phase="retry", error=str(e)[:200]); time.sleep(backoff); backoff *= 2
                else:
                    raise
        vid = resp["id"]
        set_status(name, phase="done", pct=100, videoId=vid, url=f"https://youtu.be/{vid}",
                   studio=f"https://studio.youtube.com/video/{vid}/edit",
                   privacy=resp.get("status", {}).get("privacyStatus"), uploadStatus=resp.get("status", {}).get("uploadStatus"))
        os.remove(local)
    except Exception as e:  # noqa: BLE001
        set_status(name, phase="failed", error=f"{type(e).__name__}: {e}"[:500])


def main():
    a = sys.argv[1:]
    meta = json.load(open(a[0], encoding="utf-8"))
    privacy = a[a.index("--privacy") + 1] if "--privacy" in a else "private"
    only = a[a.index("--only") + 1] if "--only" in a else None
    vids = [v for v in meta["videos"] if not only or v["file"].startswith(only)]
    creds = load_creds()
    ths = [threading.Thread(target=upload_one, args=(creds, meta["s3_prefix"], v, privacy), daemon=True) for v in vids]
    for t in ths: t.start()
    for t in ths: t.join()
    print(json.dumps(_status, ensure_ascii=False, indent=1))
    sys.exit(0 if all(s.get("phase") == "done" for s in _status.values()) else 1)


if __name__ == "__main__":
    main()
