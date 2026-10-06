#!/usr/bin/env python3
"""AWS Elemental MediaConvert render: one job per presenter with InputClippings (concatenated).

Usage:
  render.py <input-name>                       plan from plans/<name>.cuts.json (S3)
  render.py <input-name> --plan local.json     plan from a local file (hand-written / hand-adjusted)
  render.py <input-name> --only spk_1          render only one presenter label
  render.py <input-name> --audio-only          make output/<name>/audio.m4a (AAC) for Transcribe (>2 GB inputs)
  render.py --ensure-role                      just create/check the IAM role and exit
Options: --fps 25 (skip ffmpeg probe), --force (re-render even if output exists), --dry-run (print job JSON)

Output: s3://<bucket>/output/<name>/<presenter-slug>.mp4
Encoding: H.264 QVBR 8, max 12 Mbps, source resolution/frame rate; AAC-LC 192 kbps 48 kHz stereo;
loudness ITU-R BS.1770-3 -14 LKFS; MP4 moov PROGRESSIVE_DOWNLOAD.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

from botocore.exceptions import ClientError

import zve_common as C

ROLE_NAME = "MediaConvertZoomEditRole"
FFMPEG = os.path.expanduser("~/.cache/zoom-video-edit/ffmpeg")
PRESIGN_SECONDS = 12 * 3600


# ---------- IAM role ----------
def ensure_role():
    iam = C.client("iam", region="us-east-1")
    sts = C.client("sts")
    acct = sts.get_caller_identity()["Account"]
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "mediaconvert.amazonaws.com"},
        "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": acct}}}]}
    policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"],
         "Resource": f"arn:aws:s3:::{C.BUCKET}"},
        {"Effect": "Allow", "Action": ["s3:GetObject"],
         "Resource": [f"arn:aws:s3:::{C.BUCKET}/input/*", f"arn:aws:s3:::{C.BUCKET}/test/*",
                      f"arn:aws:s3:::{C.BUCKET}/output/*"]},
        {"Effect": "Allow", "Action": ["s3:PutObject"],
         "Resource": f"arn:aws:s3:::{C.BUCKET}/output/*"},
    ]}
    try:
        arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
        C.log(f"[render] role exists: {arn}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise
        arn = iam.create_role(
            RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(trust),
            Description="MediaConvert service role for zoom-video-edit (least privilege on working bucket)",
            Tags=[{"Key": k, "Value": v} for k, v in C.PROJECT_TAG.items()],
        )["Role"]["Arn"]
        C.log(f"[render] created role {arn}; waiting for IAM propagation")
        time.sleep(12)
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="zoom-video-edit-s3",
                        PolicyDocument=json.dumps(policy))
    return arn


# ---------- probing ----------
def probe_fps(s3, key):
    """Return (fps, duration_s) using the bundled ffmpeg over a presigned URL; (None, None) if unavailable."""
    if not os.path.exists(FFMPEG):
        return None, None
    url = s3.generate_presigned_url("get_object", Params={"Bucket": C.BUCKET, "Key": key}, ExpiresIn=900)
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-i", url], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    out = r.stderr
    m = re.search(r"(\d+(?:\.\d+)?) fps", out)
    d = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", out)
    fps = float(m.group(1)) if m else None
    dur = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3)) if d else None
    return fps, dur


def mc_client():
    mc = C.client("mediaconvert")
    url = mc.describe_endpoints(MaxResults=1)["Endpoints"][0]["Url"]
    return C.boto3.client("mediaconvert", region_name=C.REGION, endpoint_url=url)


# ---------- job settings ----------
def video_output(slug):
    return {
        "NameModifier": None,
        "ContainerSettings": {"Container": "MP4", "Mp4Settings": {"MoovPlacement": "PROGRESSIVE_DOWNLOAD",
                                                                    "CslgAtom": "INCLUDE", "FreeSpaceBox": "EXCLUDE"}},
        "VideoDescription": {
            "ScalingBehavior": "DEFAULT", "TimecodeInsertion": "DISABLED", "AntiAlias": "ENABLED",
            "Sharpness": 50, "AfdSignaling": "NONE", "DropFrameTimecode": "ENABLED", "RespondToAfd": "NONE",
            "ColorMetadata": "INSERT",
            "CodecSettings": {"Codec": "H_264", "H264Settings": {
                "RateControlMode": "QVBR", "QvbrSettings": {"QvbrQualityLevel": 8},
                "MaxBitrate": 12_000_000, "CodecProfile": "HIGH", "CodecLevel": "AUTO",
                "FramerateControl": "INITIALIZE_FROM_SOURCE", "ParControl": "INITIALIZE_FROM_SOURCE",
                "GopSizeUnits": "SECONDS", "GopSize": 2, "GopClosedCadence": 1, "NumberBFramesBetweenReferenceFrames": 2,
                "SceneChangeDetect": "TRANSITION_DETECTION", "QualityTuningLevel": "SINGLE_PASS",
                "AdaptiveQuantization": "HIGH", "EntropyEncoding": "CABAC", "InterlaceMode": "PROGRESSIVE",
            }},
        },
        "AudioDescriptions": [audio_description()],
    }


def audio_description():
    return {
        "AudioTypeControl": "FOLLOW_INPUT", "LanguageCodeControl": "FOLLOW_INPUT",
        "AudioNormalizationSettings": {"Algorithm": "ITU_BS_1770_3", "AlgorithmControl": "CORRECT_AUDIO",
                                       "TargetLkfs": -14.0, "LoudnessLogging": "LOG", "PeakCalculation": "NONE"},
        "CodecSettings": {"Codec": "AAC", "AacSettings": {
            "CodecProfile": "LC", "CodingMode": "CODING_MODE_2_0", "SampleRate": 48000, "Bitrate": 192000,
            "RateControlMode": "CBR", "RawFormat": "NONE", "Specification": "MPEG4"}},
    }


def audio_only_output():
    return {
        "Extension": "m4a",
        "ContainerSettings": {"Container": "MP4", "Mp4Settings": {"MoovPlacement": "PROGRESSIVE_DOWNLOAD"}},
        "AudioDescriptions": [{
            "AudioTypeControl": "FOLLOW_INPUT", "LanguageCodeControl": "FOLLOW_INPUT",
            "CodecSettings": {"Codec": "AAC", "AacSettings": {
                "CodecProfile": "LC", "CodingMode": "CODING_MODE_2_0", "SampleRate": 48000, "Bitrate": 96000,
                "RateControlMode": "CBR", "RawFormat": "NONE", "Specification": "MPEG4"}}}],
    }


def build_job(role_arn, in_key, dest_base, clippings, output, name, label):
    inp = {
        "FileInput": C.s3_uri(in_key), "TimecodeSource": "ZEROBASED",
        "AudioSelectors": {"Audio Selector 1": {"DefaultSelection": "DEFAULT"}},
        "FilterEnable": "AUTO", "PsiControl": "USE_PSI", "DeblockFilter": "DISABLED", "DenoiseFilter": "DISABLED",
    }
    if "VideoDescription" in output:
        inp["VideoSelector"] = {"ColorSpace": "FOLLOW", "Rotate": "DEGREE_0", "AlphaBehavior": "DISCARD"}
    if clippings:
        inp["InputClippings"] = clippings
    out = {k: v for k, v in output.items() if v is not None}
    return {
        "Role": role_arn,
        "Settings": {
            "TimecodeConfig": {"Source": "ZEROBASED"},
            "Inputs": [inp],
            "OutputGroups": [{
                "Name": "File Group", "CustomName": label,
                "OutputGroupSettings": {"Type": "FILE_GROUP_SETTINGS",
                                        "FileGroupSettings": {"Destination": C.s3_uri(dest_base)}},
                "Outputs": [out],
            }],
        },
        "StatusUpdateInterval": "SECONDS_15",
        "Priority": 0,
        "Tags": {**C.PROJECT_TAG, "Recording": name[:200], "Presenter": label[:200]},
        "UserMetadata": {**C.PROJECT_TAG, "recording": name[:200], "presenter": label[:200]},
    }


def segments_to_clippings(segments, fps):
    clips = []
    for s in segments:
        st, en = C.parse_ts(s["start"]), C.parse_ts(s["end"])
        clips.append({"StartTimecode": C.fmt_smpte(st, fps), "EndTimecode": C.fmt_smpte(en, fps)})
    return clips


def wait_job(mc, job_id, poll=15):
    t0 = time.time()
    while True:
        j = mc.get_job(Id=job_id)["Job"]
        st = j["Status"]
        if st == "COMPLETE":
            C.log(f"[render] job {job_id} COMPLETE in {time.time() - t0:.0f}s")
            return j
        if st in ("ERROR", "CANCELED"):
            C.die(f"job {job_id} {st}: {j.get('ErrorCode')} {j.get('ErrorMessage')}")
        C.log(f"[render] job {job_id} {st} {j.get('JobPercentComplete', 0)}% ... {time.time() - t0:.0f}s")
        time.sleep(poll)


def presenter_slug(p, idx):
    base = p.get("inferred_name_or_null") or p.get("talk_title_or_null") or p["presenter_label"]
    return f"{idx:02d}-{C.slugify(base)}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name", nargs="?")
    ap.add_argument("--plan")
    ap.add_argument("--only")
    ap.add_argument("--fps", type=float)
    ap.add_argument("--audio-only", action="store_true")
    ap.add_argument("--ensure-role", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    role_arn = ensure_role()
    if a.ensure_role:
        print(role_arn)
        return
    if not a.name:
        ap.error("name required")

    in_key = C.resolve_input_key(a.name)
    name = C.base_name(in_key)
    s3 = C.client("s3")
    mc = mc_client()
    s3.head_object(Bucket=C.BUCKET, Key=in_key)

    if a.audio_only:
        dest = f"output/{name}/audio"
        job = build_job(role_arn, in_key, dest, [], audio_only_output(), name, "audio-only")
        if a.dry_run:
            print(json.dumps(job, indent=1)); return
        jid = mc.create_job(**job)["Job"]["Id"]
        C.log(f"[render] audio-only job {jid}")
        wait_job(mc, jid)
        print(C.s3_uri(dest + ".m4a"))
        return

    if a.plan:
        plan = json.load(open(a.plan, encoding="utf-8"))
    else:
        plan = json.loads(s3.get_object(Bucket=C.BUCKET, Key=f"plans/{name}.cuts.json")["Body"].read())

    fps = a.fps
    src_dur = None
    if fps is None:
        fps, src_dur = probe_fps(s3, in_key)
        if fps is None:
            fps = 30.0
            C.log("[render] WARNING: could not probe frame rate; assuming 30 fps (pass --fps to override)")
    C.log(f"[render] input={C.s3_uri(in_key)} fps={fps} source_duration={src_dur}")

    results = []
    for idx, p in enumerate(plan["presenters"], 1):
        label = p["presenter_label"]
        if a.only and a.only not in (label, str(idx)):
            continue
        if not p.get("segments"):
            C.log(f"[render] {label}: no segments, skipping")
            continue
        slug = presenter_slug(p, idx)
        dest_base = f"output/{name}/{slug}"
        out_key = dest_base + ".mp4"
        planned = sum(C.parse_ts(s["end"]) - C.parse_ts(s["start"]) for s in p["segments"])
        if not a.force and not a.dry_run:
            try:
                s3.head_object(Bucket=C.BUCKET, Key=out_key)
                C.log(f"[render] {out_key} exists; skip (--force to re-render)")
                results.append((slug, out_key, None, planned, None))
                continue
            except ClientError:
                pass
        clips = segments_to_clippings(p["segments"], fps)
        job = build_job(role_arn, in_key, dest_base, clips, video_output(slug), name, label)
        if a.dry_run:
            print(json.dumps(job, indent=1))
            continue
        jid = mc.create_job(**job)["Job"]["Id"]
        C.log(f"[render] {label} -> {out_key} job={jid} clips={len(clips)} planned={C.fmt_ts(planned)}")
        j = wait_job(mc, jid)
        dur_ms = None
        for og in j.get("OutputGroupDetails", []):
            for od in og.get("OutputDetails", []):
                dur_ms = od.get("DurationInMs")
                vd = od.get("VideoDetails", {})
                C.log(f"[render]   output {vd.get('WidthInPx')}x{vd.get('HeightInPx')} duration={dur_ms} ms")
        results.append((slug, out_key, jid, planned, dur_ms))

    if a.dry_run:
        return
    print("\n== outputs ==")
    for slug, key, jid, planned, dur_ms in results:
        head = s3.head_object(Bucket=C.BUCKET, Key=key)
        size_mb = head["ContentLength"] / 1e6
        url = s3.generate_presigned_url("get_object", Params={"Bucket": C.BUCKET, "Key": key}, ExpiresIn=PRESIGN_SECONDS)
        got = f"{dur_ms / 1000:.3f}s" if dur_ms else "n/a"
        diff = f"diff={abs(dur_ms / 1000 - planned):.3f}s" if dur_ms else ""
        print(f"{slug}: {C.s3_uri(key)} size={size_mb:.1f} MB planned={planned:.3f}s rendered={got} {diff} job={jid}")
        print(f"   presigned (12 h requested; actual validity ends when the vending credentials expire):\n   {url}")


if __name__ == "__main__":
    main()
