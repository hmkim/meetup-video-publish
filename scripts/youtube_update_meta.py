#!/usr/bin/env python3
"""Update title/description/tags of an existing YouTube video (snippet.update).

Usage:
  youtube_update_meta.py <videoId> [--title T] [--description D] [--replace OLD NEW]...
      [--tags a,b,c] [--dry-run]

--replace applies a literal substring replacement to the CURRENT title, description
and tags (repeatable), so a wrong name can be fixed without retyping everything.
Requires the same token as youtube_upload.py (scope youtube.upload is NOT enough for
videos.update; youtube.force-ssl or youtube is required -- see check in main()).
"""
import argparse, json, os, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from youtube_auth import load_creds  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402
from googleapiclient.errors import HttpError  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video_id")
    ap.add_argument("--title")
    ap.add_argument("--description")
    ap.add_argument("--tags", help="comma separated; replaces the whole tag list")
    ap.add_argument("--replace", nargs=2, action="append", metavar=("OLD", "NEW"), default=[])
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    creds = load_creds()
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    cur = yt.videos().list(part="snippet", id=a.video_id).execute()
    if not cur.get("items"):
        sys.exit(f"video {a.video_id} not found / not owned")
    sn = cur["items"][0]["snippet"]
    new = {k: sn.get(k) for k in ("title", "description", "tags", "categoryId", "defaultLanguage", "defaultAudioLanguage")}
    new = {k: v for k, v in new.items() if v is not None}
    if a.title:
        new["title"] = a.title
    if a.description:
        new["description"] = a.description
    if a.tags:
        new["tags"] = [t.strip() for t in a.tags.split(",") if t.strip()]
    for old, rep in a.replace:
        new["title"] = new["title"].replace(old, rep)
        new["description"] = new.get("description", "").replace(old, rep)
        new["tags"] = [t.replace(old, rep) for t in new.get("tags", [])]
    if len(new["title"]) > 100:
        sys.exit(f"title too long ({len(new['title'])} > 100)")

    changed = {k: (sn.get(k), new[k]) for k in new if sn.get(k) != new[k]}
    print(json.dumps({"video": a.video_id, "changed": changed}, ensure_ascii=False, indent=1))
    if not changed:
        print("nothing to change"); return
    if a.dry_run:
        print("dry-run, not written"); return
    try:
        yt.videos().update(part="snippet", body={"id": a.video_id, "snippet": new}).execute()
    except HttpError as e:
        if e.resp.status == 403 and "insufficient" in str(e).lower():
            sys.exit("403 insufficientPermissions: token lacks youtube.force-ssl scope -> "
                     "re-run youtube_auth.py url/finish with the wider scope (see youtube_auth.SCOPES)")
        raise
    print("UPDATED")


if __name__ == "__main__":
    main()
