#!/usr/bin/env python3
"""Shared helpers for the zoom-video-edit pipeline (stdlib + boto3 only)."""
import os
import re
import sys

import boto3
from botocore.config import Config

REGION = os.environ.get("ZVE_REGION", "ap-northeast-2")
BUCKET = os.environ.get("ZVE_BUCKET")
if not BUCKET:
    sys.exit("ZVE_BUCKET is not set -- export ZVE_BUCKET=<your-working-bucket> (see README, Setup)")
PROJECT_TAG = {"Project": os.environ.get("ZVE_PROJECT_TAG", "zoom-video-edit")}
WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_BOTO_CFG = Config(region_name=REGION, retries={"max_attempts": 8, "mode": "standard"},
                   s3={"addressing_style": "virtual"})


def client(service, region=None):
    region = region or REGION
    kwargs = {}
    if service == "s3":
        # Pin the regional endpoint: presigned URLs on the global endpoint answer 307 TemporaryRedirect,
        # which drops the signature when a client (ffmpeg, browser) follows it -> 403.
        kwargs["endpoint_url"] = f"https://s3.{region}.amazonaws.com"
    return boto3.client(service, config=_BOTO_CFG, region_name=region, **kwargs)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def die(msg, code=1):
    log("ERROR:", msg)
    sys.exit(code)


# ---------- time helpers ----------
def fmt_ts(seconds: float) -> str:
    """seconds -> HH:MM:SS.mmm"""
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


_TS_RE = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})(?:[.,](\d{1,3}))?$")


def parse_ts(text: str) -> float:
    """HH:MM:SS.mmm -> seconds"""
    m = _TS_RE.match(text.strip())
    if not m:
        raise ValueError(f"bad timestamp: {text!r}")
    h, mi, s, ms = m.groups()
    ms = (ms or "0").ljust(3, "0")
    return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms) / 1000.0


def fmt_smpte(seconds: float, fps: float) -> str:
    """seconds -> HH:MM:SS:FF (MediaConvert InputClippings, non-drop-frame)."""
    if seconds < 0:
        seconds = 0.0
    fps_int = int(round(fps))
    total_frames = int(round(seconds * fps))
    frames = total_frames % fps_int
    total_s = total_frames // fps_int
    h, rem = divmod(total_s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}:{frames:02d}"


# ---------- names ----------
def base_name(key_or_name: str) -> str:
    """'input/foo bar.mp4' -> 'foo bar'"""
    n = key_or_name.rsplit("/", 1)[-1]
    if "." in n:
        n = n.rsplit(".", 1)[0]
    return n


def slugify(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9가-힣._-]+", "-", text.strip()).strip("-")
    return text[:64] or "presenter"


def transcribe_job_name(name: str) -> str:
    """Transcribe job names: [0-9a-zA-Z._-]{1,200}"""
    s = re.sub(r"[^0-9a-zA-Z._-]+", "-", name).strip("-")
    return ("zve-" + s)[:200]


def s3_uri(key: str) -> str:
    return f"s3://{BUCKET}/{key}"


def resolve_input_key(name_or_key: str) -> str:
    """Accept 'name', 'name.mp4', 'input/name.mp4' or a full s3:// URI on our bucket."""
    v = name_or_key
    if v.startswith("s3://"):
        b, k = v[5:].split("/", 1)
        if b != BUCKET:
            die(f"input must be in bucket {BUCKET}")
        return k
    if "/" in v:
        return v
    if not v.lower().endswith((".mp4", ".m4a", ".mp3", ".mov", ".mkv", ".wav")):
        v += ".mp4"
    return "input/" + v
