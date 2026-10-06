#!/usr/bin/env python3
"""Amazon Transcribe with speaker labels -> transcripts/<name>.speakers.txt

Usage:
  transcribe.py <input-name | input/key.mp4 | s3://bucket/key>  [--force] [--media-key KEY]

Writes to S3:
  transcripts/<name>.json            raw Transcribe output
  transcripts/<name>.speakers.txt    [HH:MM:SS.mmm - HH:MM:SS.mmm] spk_N: text  (merged)
and mirrors both locally under <workspace>/work/<name>/ .
Idempotent: if transcripts/<name>.speakers.txt exists and --force is not given, it is reused.
"""
import argparse
import json
import os
import time

from botocore.exceptions import ClientError

import zve_common as C

SILENCE_GAP_NEW_LINE = 1.0  # start a new line for the same speaker after this many seconds of gap


def start_job(job_name, media_uri, out_key):
    tr = C.client("transcribe")
    ext = media_uri.rsplit(".", 1)[-1].lower()
    fmt = {"m4a": "m4a", "mp3": "mp3", "wav": "wav", "flac": "flac", "ogg": "ogg", "webm": "webm"}.get(ext, "mp4")
    params = dict(
        TranscriptionJobName=job_name,
        Media={"MediaFileUri": media_uri},
        MediaFormat=fmt,
        OutputBucketName=C.BUCKET,
        OutputKey=out_key,
        IdentifyMultipleLanguages=True,
        LanguageOptions=["ko-KR", "en-US"],
        Settings={"ShowSpeakerLabels": True, "MaxSpeakerLabels": 10},
        Tags=[{"Key": k, "Value": v} for k, v in C.PROJECT_TAG.items()],
    )
    tr.start_transcription_job(**params)
    C.log(f"[transcribe] started job {job_name} on {media_uri}")


def wait_job(job_name, poll=15):
    tr = C.client("transcribe")
    t0 = time.time()
    while True:
        j = tr.get_transcription_job(TranscriptionJobName=job_name)["TranscriptionJob"]
        st = j["TranscriptionJobStatus"]
        if st == "COMPLETED":
            C.log(f"[transcribe] COMPLETED in {time.time() - t0:.0f}s; languages={j.get('LanguageCodes')}")
            return j
        if st == "FAILED":
            C.die(f"transcribe job failed: {j.get('FailureReason')}")
        C.log(f"[transcribe] {st} ... {time.time() - t0:.0f}s")
        time.sleep(poll)


def build_speaker_lines(doc):
    """Merge consecutive same-speaker items into lines.

    Transcribe JSON: results.items[] with speaker_label per item (when ShowSpeakerLabels on)
    and results.speaker_labels.segments[] as fallback.
    """
    res = doc["results"]
    items = res.get("items", [])
    # Map item start_time -> speaker via segments if items lack speaker_label.
    if items and "speaker_label" not in items[0]:
        seg_map = {}
        for seg in res.get("speaker_labels", {}).get("segments", []):
            for it in seg.get("items", []):
                seg_map[it["start_time"]] = it["speaker_label"]
        for it in items:
            if "start_time" in it:
                it["speaker_label"] = seg_map.get(it["start_time"], "spk_?")
    lines = []  # dicts: spk, start, end, words[]
    cur = None
    for it in items:
        content = it["alternatives"][0]["content"]
        if it["type"] == "punctuation":
            if cur is not None:
                cur["words"].append(("", content))
            continue
        spk = it.get("speaker_label", "spk_?")
        st, en = float(it["start_time"]), float(it["end_time"])
        if cur is None or cur["spk"] != spk or (st - cur["end"]) > SILENCE_GAP_NEW_LINE * 8:
            cur = {"spk": spk, "start": st, "end": en, "words": []}
            lines.append(cur)
        cur["end"] = en
        cur["words"].append((" ", content))
    out = []
    totals = {}
    for ln in lines:
        text = "".join(sep + w for sep, w in ln["words"]).strip()
        out.append(f"[{C.fmt_ts(ln['start'])} - {C.fmt_ts(ln['end'])}] {ln['spk']}: {text}")
        totals[ln["spk"]] = totals.get(ln["spk"], 0.0) + (ln["end"] - ln["start"])
    return out, totals


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--media-key", help="transcribe this key instead (e.g. audio-only m4a)")
    a = ap.parse_args()

    in_key = C.resolve_input_key(a.name)
    name = C.base_name(in_key)
    media_key = a.media_key or in_key
    s3 = C.client("s3")
    json_key = f"transcripts/{name}.json"
    txt_key = f"transcripts/{name}.speakers.txt"
    workdir = os.path.join(C.WORKSPACE, "work", name)
    os.makedirs(workdir, exist_ok=True)
    local_txt = os.path.join(workdir, f"{name}.speakers.txt")

    if not a.force:
        try:
            s3.head_object(Bucket=C.BUCKET, Key=txt_key)
            C.log(f"[transcribe] {C.s3_uri(txt_key)} exists; reuse (--force to redo)")
            s3.download_file(C.BUCKET, txt_key, local_txt)
            print(local_txt)
            return
        except ClientError:
            pass

    head = s3.head_object(Bucket=C.BUCKET, Key=media_key)
    size = head["ContentLength"]
    if size > 2 * 1024 ** 3:
        C.die(f"{media_key} is {size / 1e9:.2f} GB > Transcribe 2 GB limit; run ingest.sh audio-only first "
              f"and pass --media-key")
    job_name = C.transcribe_job_name(name + "-" + str(int(time.time())))
    start_job(job_name, C.s3_uri(media_key), json_key)
    job = wait_job(job_name)

    local_json = os.path.join(workdir, f"{name}.json")
    s3.download_file(C.BUCKET, json_key, local_json)
    doc = json.load(open(local_json, encoding="utf-8"))
    lines, totals = build_speaker_lines(doc)
    with open(local_txt, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    s3.upload_file(local_txt, C.BUCKET, txt_key,
                   ExtraArgs={"ContentType": "text/plain; charset=utf-8",
                              "Tagging": "Project=zoom-video-edit"})
    s3.put_object_tagging(Bucket=C.BUCKET, Key=json_key,
                          Tagging={"TagSet": [{"Key": "Project", "Value": "zoom-video-edit"}]})
    C.log(f"[transcribe] job={job_name} lines={len(lines)} -> {C.s3_uri(txt_key)}")
    C.log("[transcribe] speaking time per speaker:")
    for spk, secs in sorted(totals.items(), key=lambda kv: -kv[1]):
        C.log(f"   {spk:8s} {C.fmt_ts(secs)}  ({secs / 60:.1f} min)")
    print(local_txt)


if __name__ == "__main__":
    main()
