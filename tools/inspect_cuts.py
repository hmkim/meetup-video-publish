#!/usr/bin/env python3
"""inspect_cuts.py <transcribe.json> -- print word timings around given windows and silence gaps.
usage: inspect_cuts.py file.json  "00:07:44-00:08:02" "00:35:40-00:35:58" ...  [--gaps 15]
"""
import json, sys

def hms(s):
    h = int(s // 3600); m = int(s % 3600 // 60); sec = s - h * 3600 - m * 60
    return f"{h:02d}:{m:02d}:{sec:06.3f}"

def parse(t):
    h, m, s = t.split(":"); return int(h) * 3600 + int(m) * 60 + float(s)

path = sys.argv[1]; args = sys.argv[2:]
gap_thr = 15.0
if "--gaps" in args:
    i = args.index("--gaps"); gap_thr = float(args[i + 1]); del args[i:i + 2]
d = json.load(open(path))
res = d["results"]
items = [it for it in res["items"] if it["type"] == "pronunciation"]
# speaker per item
spk = {}
for seg in res.get("speaker_labels", {}).get("segments", []):
    for it in seg.get("items", []):
        spk[it["start_time"]] = it["speaker_label"]
print(f"items={len(items)} first={hms(float(items[0]['start_time']))} last_end={hms(float(items[-1]['end_time']))}")
for w in args:
    a, b = (parse(x) for x in w.split("-"))
    print(f"\n--- window {hms(a)} - {hms(b)}")
    for it in items:
        st, en = float(it["start_time"]), float(it["end_time"])
        if a <= st <= b:
            print(f"{hms(st)}-{hms(en)} {spk.get(it['start_time'],'?')}: {it['alternatives'][0]['content']}")
print(f"\n--- gaps > {gap_thr}s")
prev = None
for it in items:
    st, en = float(it["start_time"]), float(it["end_time"])
    if prev is not None and st - prev > gap_thr:
        print(f"{hms(prev)} -> {hms(st)}  ({st - prev:.1f}s)")
    prev = en
