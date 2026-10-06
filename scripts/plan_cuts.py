#!/usr/bin/env python3
"""Bedrock Claude cut planning: transcripts/<name>.speakers.txt -> plans/<name>.cuts.json + plans/<name>.cuts.md

Usage:
  plan_cuts.py <input-name>            (reads transcripts/<name>.speakers.txt from S3)
  plan_cuts.py --probe                 (just probe which Claude model answers; prints model id)
  plan_cuts.py <name> --model <id>     (force a model)
  plan_cuts.py <name> --force          (ignore existing plan)

Output JSON schema (strict):
{
  "host_label": "spk_0",
  "rationale": "...",
  "presenters": [
    {"presenter_label": "spk_1", "inferred_name_or_null": ..., "talk_title_or_null": ...,
     "segments": [{"start": "HH:MM:SS.mmm", "end": "HH:MM:SS.mmm", "reason": "..."}],
     "drop_intervals_inside": [{"start": ..., "end": ..., "reason": "..."}],
     "notes": "..."}
  ]
}
"""
import argparse
import json
import os
import re
import sys
import time

from botocore.exceptions import ClientError

import zve_common as C

PREFERRED = ["global.anthropic.claude-opus-5-5", "global.anthropic.claude-sonnet-4-5-20250929-v1:0"]
KNOWN_FALLBACK = "apac.anthropic.claude-3-5-sonnet-20241022-v2:0"
PAD_S = 1.5
MAX_INPUT_CHARS = 350_000  # ~ 90k tokens; chunk above this

# --talk-only: keep ONLY the presenter's own talk. Replaces RULE 2/3 of SYSTEM_PROMPT.
TALK_ONLY_RULES = """
2. For each presenter, keep from the FIRST CONTENT SENTENCE of their talk (not the host's intro, not
   "can you see my screen?") to the presenter's LAST TALK SENTENCE (the wrap-up of the presentation
   itself, e.g. "이상입니다 / 감사합니다 / 질문 있으시면"). Do NOT keep the Q&A that follows the talk.
3. DROP everything spoken by OTHER speakers (host, audience) and the presenter's answers to them:
   host intros/housekeeping/hand-offs, audience questions and the presenter's replies (also those in
   the MIDDLE of the talk, e.g. live polls, interjected questions), breaks, silence longer than 20
   seconds, audio/screen-share troubleshooting, unrelated chatter. Exception: an interruption shorter
   than 3 seconds (a "네", "맞아요" back-channel) is kept, since cutting it would look jarring.
   Express mid-talk removals as multiple segments (the gap between consecutive segments is the drop)
   AND list each removed interval in drop_intervals_inside with its reason. Place every cut on a
   sentence boundary of the presenter so the remaining speech sounds continuous.
"""


SYSTEM_PROMPT = """You are a meticulous video editor's assistant. You receive a diarized transcript of a
recorded online meetup (Zoom) with ONE host and TWO presenters (talks may be Korean, English or mixed).
Lines look like: [HH:MM:SS.mmm - HH:MM:SS.mmm] spk_N: text

Your job: decide which time ranges to KEEP so that we can produce TWO separate YouTube videos, one per
presenter, each containing ONLY that presenter's talk plus the Q&A that directly follows it.

RULES
1. Identify the host (spk label that does welcomes, logistics, introductions, hand-offs, closing) and
   the two presenters (the two non-host speakers with the most continuous speaking time / content).
2. For each presenter, keep from the FIRST CONTENT SENTENCE of their talk (not the host's intro, not
   "can you see my screen?") to the END of the Q&A that directly follows their talk (include audience
   questions and the presenter's answers; stop before the host moves to the next item).
3. DROP (do not keep): host intros and housekeeping, breaks/pauses, silence longer than 20 seconds,
   audio or screen-share troubleshooting ("is it showing?", "can you hear me?"), unrelated chatter.
   Cut those out of the middle of a talk using multiple segments if needed.
4. Pad every kept segment by 1.5 seconds on BOTH ends (never before 00:00:00.000). Merge segments that
   would overlap or touch after padding.
5. Timestamps are strings HH:MM:SS.mmm. Segments within one presenter are chronological and never
   overlap. The two presenters' segments must not overlap each other either.
6. If the transcript clearly has only one presenter (or none), still return the JSON with the
   presenters array holding what you can infer (may be 1 element) and explain in rationale.
7. Write the free-text fields (rationale, reason, notes, talk_title_or_null) in Korean (한국어); keep
   names as spoken.
8. Output STRICT JSON ONLY -- no markdown fences, no commentary. Schema:
{"host_label": "spk_N" | null,
 "rationale": "short explanation of how speakers were identified",
 "presenters": [
   {"presenter_label": "spk_N",
    "inferred_name_or_null": string | null,
    "talk_title_or_null": string | null,
    "segments": [{"start": "HH:MM:SS.mmm", "end": "HH:MM:SS.mmm", "reason": "why kept"}],
    "drop_intervals_inside": [{"start": "HH:MM:SS.mmm", "end": "HH:MM:SS.mmm", "reason": "why removed"}],
    "notes": string}
 ]}
"""


def bedrock_runtime():
    return C.client("bedrock-runtime")


def converse(model_id, user_text, max_tokens=8000, system=None):
    rt = bedrock_runtime()
    kwargs = dict(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": user_text}]}],
        inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
    )
    if system:
        kwargs["system"] = [{"text": system}]
    try:
        r = rt.converse(**kwargs)
    except ClientError as e:
        # Claude Opus 5.x rejects `temperature` ("temperature is deprecated for this model"); retry without it.
        if e.response["Error"].get("Code") == "ValidationException" and "temperature" in e.response["Error"].get("Message", ""):
            kwargs["inferenceConfig"].pop("temperature", None)
            r = rt.converse(**kwargs)
        else:
            raise
    blocks = r["output"]["message"]["content"]
    text = "".join(b["text"] for b in blocks if "text" in b)  # skip reasoningContent blocks
    return text, r.get("usage", {})


def candidate_models():
    cands = list(PREFERRED)
    try:
        br = C.client("bedrock")
        profs = []
        pag = br.get_paginator("list_inference_profiles")
        for page in pag.paginate():
            profs += page["inferenceProfileSummaries"]
        ids = [p["inferenceProfileId"] for p in profs if p.get("status") == "ACTIVE"]
        sonnets = [i for i in ids if "anthropic" in i and "sonnet" in i]
        # global.* first, newest-looking first
        sonnets.sort(key=lambda i: (not i.startswith("global."), i), reverse=False)
        sonnets.sort(key=lambda i: 0 if i.startswith("global.") else 1)
        for i in reversed(sorted(s for s in sonnets if s.startswith("global."))):
            if i not in cands:
                cands.append(i)
        for i in reversed(sorted(s for s in sonnets if not s.startswith("global."))):
            if i not in cands:
                cands.append(i)
    except ClientError as e:
        C.log(f"[plan] list-inference-profiles failed: {e}")
    if KNOWN_FALLBACK not in cands:
        cands.append(KNOWN_FALLBACK)
    return cands


def probe_model():
    for mid in candidate_models():
        try:
            txt, _ = converse(mid, "reply ok", max_tokens=10)
            C.log(f"[plan] probe {mid}: {txt.strip()!r}")
            if txt.strip():
                return mid
        except ClientError as e:
            C.log(f"[plan] probe {mid} failed: {e.response['Error'].get('Code')} {e.response['Error'].get('Message', '')[:120]}")
    C.die("no Bedrock Claude model answered the probe")


# ---------- validation / normalisation ----------
def _norm_ts(v):
    return C.fmt_ts(C.parse_ts(v))


def validate_plan(plan):
    assert isinstance(plan, dict), "plan not an object"
    assert "presenters" in plan and isinstance(plan["presenters"], list), "presenters missing"
    plan.setdefault("host_label", None)
    plan.setdefault("rationale", "")
    all_iv = []
    for p in plan["presenters"]:
        for k in ("presenter_label", "segments"):
            assert k in p, f"presenter missing {k}"
        p.setdefault("inferred_name_or_null", None)
        p.setdefault("talk_title_or_null", None)
        p.setdefault("drop_intervals_inside", [])
        p.setdefault("notes", "")
        segs = []
        for s in p["segments"]:
            st, en = C.parse_ts(s["start"]), C.parse_ts(s["end"])
            assert en > st, f"segment end<=start: {s}"
            segs.append({"start": C.fmt_ts(st), "end": C.fmt_ts(en), "reason": s.get("reason", "")})
        segs.sort(key=lambda s: s["start"])
        for a_, b_ in zip(segs, segs[1:]):
            assert C.parse_ts(b_["start"]) >= C.parse_ts(a_["end"]), f"overlap within presenter {p['presenter_label']}"
        p["segments"] = segs
        for d in p["drop_intervals_inside"]:
            d["start"], d["end"] = _norm_ts(d["start"]), _norm_ts(d["end"])
        all_iv += [(C.parse_ts(s["start"]), C.parse_ts(s["end"]), p["presenter_label"]) for s in segs]
    all_iv.sort()
    for a_, b_ in zip(all_iv, all_iv[1:]):
        if a_[2] != b_[2]:
            assert b_[0] >= a_[1], f"segments of {a_[2]} and {b_[2]} overlap"
    return plan


def extract_json(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j < 0:
        raise ValueError("no JSON object in model output")
    return json.loads(text[i:j + 1])


def system_prompt(talk_only):
    if not talk_only:
        return SYSTEM_PROMPT
    # swap rules 2-3 for the talk-only variant
    return re.sub(r"\n2\. For each presenter.*?\n4\. ", "\n" + TALK_ONLY_RULES.strip("\n") + "\n4. ", SYSTEM_PROMPT, flags=re.S)


def ask_plan(model_id, transcript_text, name, talk_only=False):
    sysp = system_prompt(talk_only)
    user = (f"Recording name: {name}\n\nDIARIZED TRANSCRIPT (speaker labels spk_N, times HH:MM:SS.mmm):\n\n"
            f"{transcript_text}\n\nReturn the strict JSON plan now.")
    last_err = None
    for attempt in (1, 2):
        txt, usage = converse(model_id, user, system=sysp)
        C.log(f"[plan] attempt {attempt} usage={usage}")
        try:
            return validate_plan(extract_json(txt)), usage
        except (ValueError, AssertionError, KeyError, json.JSONDecodeError) as e:
            last_err = e
            C.log(f"[plan] invalid JSON/plan on attempt {attempt}: {e}")
            user += (f"\n\nYour previous answer was rejected: {e}. Return ONLY valid JSON matching the schema.")
    C.die(f"plan invalid after retry: {last_err}")


def chunk_transcript(text):
    if len(text) <= MAX_INPUT_CHARS:
        return [text]
    lines = text.splitlines()
    chunks, cur, n = [], [], 0
    for ln in lines:
        if n + len(ln) > MAX_INPUT_CHARS and cur:
            chunks.append("\n".join(cur))
            cur, n = [], 0
        cur.append(ln)
        n += len(ln) + 1
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def summarize_chunk(model_id, chunk, idx, total):
    """Condense a long transcript chunk to a speaker/time outline so the final planning call fits."""
    prompt = (f"This is part {idx}/{total} of a diarized meetup transcript. Produce a CONDENSED outline that "
              f"preserves, with exact HH:MM:SS.mmm timestamps: every speaker change, what each speaker is doing "
              f"(host intro / housekeeping / talk content topic / Q&A / troubleshooting / break / silence gap), "
              f"and any spoken names or talk titles. Keep the [start - end] spk_N: prefix format, one line per "
              f"unit, max 250 lines.\n\n{chunk}")
    txt, usage = converse(model_id, prompt, max_tokens=8000)
    C.log(f"[plan] chunk {idx}/{total} condensed usage={usage}")
    return txt


# ---------- markdown ----------
def seg_dur(s):
    return C.parse_ts(s["end"]) - C.parse_ts(s["start"])


def to_markdown(plan, name, model_id):
    out = [f"# 컷 편집 계획 — `{name}`", "",
           f"- 생성 모델: `{model_id}`", f"- 호스트 화자: `{plan.get('host_label')}`",
           f"- 판단 근거: {plan.get('rationale', '')}", ""]
    for i, p in enumerate(plan["presenters"], 1):
        nm = p.get("inferred_name_or_null") or "(이름 미상)"
        title = p.get("talk_title_or_null") or "(제목 미상)"
        total = sum(seg_dur(s) for s in p["segments"])
        out += [f"## 발표자 {i}: {nm} — {title}", "",
                f"- 화자 라벨: `{p['presenter_label']}`  ·  결과 영상 길이: **{C.fmt_ts(total)}**",
                f"- 메모: {p.get('notes', '')}", "",
                "### 유지 구간", "", "| # | 시작 | 끝 | 길이 | 유지 이유 |", "|---|---|---|---|---|"]
        for j, s in enumerate(p["segments"], 1):
            out.append(f"| {j} | {s['start']} | {s['end']} | {C.fmt_ts(seg_dur(s))} | {s.get('reason', '')} |")
        out += ["", "### 제거 구간 (발표 내부)", ""]
        if p["drop_intervals_inside"]:
            out += ["| 시작 | 끝 | 길이 | 제거 이유 |", "|---|---|---|---|"]
            for d in p["drop_intervals_inside"]:
                out.append(f"| {d['start']} | {d['end']} | {C.fmt_ts(seg_dur(d))} | {d.get('reason', '')} |")
        else:
            out.append("(없음)")
        out.append("")
    out += ["---", "수정 방법: `plans/<name>.cuts.json` 의 `segments` 시작/끝을 고친 뒤 "
            "`render.py <name>` 을 다시 실행하면 해당 발표자만 재렌더링됩니다."]
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name", nargs="?")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--model")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--transcript", help="local speakers.txt path (skips S3 download)")
    ap.add_argument("--talk-only", action="store_true", help="drop ALL Q&A / other speakers (mid-talk and trailing); keep only the presenter's talk")
    a = ap.parse_args()

    if a.probe:
        print(probe_model())
        return
    if not a.name:
        ap.error("name required")

    in_key = C.resolve_input_key(a.name)
    name = C.base_name(in_key)
    s3 = C.client("s3")
    plan_key, md_key = f"plans/{name}.cuts.json", f"plans/{name}.cuts.md"
    workdir = os.path.join(C.WORKSPACE, "work", name)
    os.makedirs(workdir, exist_ok=True)
    local_plan = os.path.join(workdir, f"{name}.cuts.json")
    local_md = os.path.join(workdir, f"{name}.cuts.md")

    if not a.force:
        try:
            s3.head_object(Bucket=C.BUCKET, Key=plan_key)
            C.log(f"[plan] {C.s3_uri(plan_key)} exists; reuse (--force to redo)")
            s3.download_file(C.BUCKET, plan_key, local_plan)
            print(local_plan)
            return
        except ClientError:
            pass

    if a.transcript:
        text = open(a.transcript, encoding="utf-8").read()
    else:
        txt_key = f"transcripts/{name}.speakers.txt"
        text = s3.get_object(Bucket=C.BUCKET, Key=txt_key)["Body"].read().decode("utf-8")
    if not text.strip():
        C.die("empty transcript")

    model_id = a.model or probe_model()
    chunks = chunk_transcript(text)
    if len(chunks) > 1:
        C.log(f"[plan] transcript is long ({len(text)} chars); condensing {len(chunks)} chunks first")
        text = "\n".join(summarize_chunk(model_id, c, i + 1, len(chunks)) for i, c in enumerate(chunks))

    t0 = time.time()
    plan, usage = ask_plan(model_id, text, name, talk_only=a.talk_only)
    plan["_meta"] = {"model": model_id, "source_key": in_key, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "pad_seconds": PAD_S, "usage": usage, "talk_only": a.talk_only}
    json.dump(plan, open(local_plan, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    open(local_md, "w", encoding="utf-8").write(to_markdown(plan, name, model_id))
    tagging = {"Tagging": "Project=zoom-video-edit"}
    s3.upload_file(local_plan, C.BUCKET, plan_key, ExtraArgs={"ContentType": "application/json", **tagging})
    s3.upload_file(local_md, C.BUCKET, md_key, ExtraArgs={"ContentType": "text/markdown; charset=utf-8", **tagging})
    C.log(f"[plan] model={model_id} presenters={len(plan['presenters'])} in {time.time() - t0:.0f}s -> {C.s3_uri(plan_key)}")
    for p in plan["presenters"]:
        tot = sum(seg_dur(s) for s in p["segments"])
        C.log(f"   {p['presenter_label']:6s} {p.get('inferred_name_or_null')} segs={len(p['segments'])} total={C.fmt_ts(tot)}")
    print(local_plan)


if __name__ == "__main__":
    main()
