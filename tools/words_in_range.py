#!/usr/bin/env python3
"""Print word-level Transcribe items (with speaker) inside time ranges: words_in_range.py <json> <start-end>..."""
import json, sys
def p(t):
    h, m, s = t.split(":"); return int(h)*3600+int(m)*60+float(s)
d = json.load(open(sys.argv[1], encoding="utf-8"))
items = [i for i in d["results"]["items"] if i["type"] == "pronunciation"]
for rng in sys.argv[2:]:
    a, b = (p(x) for x in rng.split("-"))
    print(f"### {rng}")
    line, cur_spk, cur_t = [], None, None
    for it in items:
        st = float(it["start_time"])
        if st < a or st > b: continue
        spk = it.get("speaker_label")
        if spk != cur_spk:
            if line: print(f"{cur_t:9.2f} {cur_spk}: {' '.join(line)}")
            line, cur_spk, cur_t = [], spk, st
        line.append(f"{it['alternatives'][0]['content']}@{st:.1f}" if len(line) % 12 == 0 else it["alternatives"][0]["content"])
    if line: print(f"{cur_t:9.2f} {cur_spk}: {' '.join(line)}")
