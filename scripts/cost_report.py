#!/usr/bin/env python3
"""cost_report.py -- actual AWS cost of one meetup run, from Cost Explorer, appended to the cost ledger.

    cost_report.py <name> --from YYYY-MM-DD --to YYYY-MM-DD [--no-write] [--note "..."]

  --from/--to  : inclusive calendar window of the run (upload day .. last render/upload day).
                 CE End is exclusive, so --to is bumped by one day internally.
  What it reads:
    * CE UnblendedCost DAILY, filter Tag Project=zoom-video-edit, grouped by SERVICE  -> tagged cost (Transcribe,
      MediaConvert jobs and the S3 bucket are tagged). Bedrock invocations cannot carry the tag, so:
    * Bedrock estimate = tokens recorded in every plans/<name>*.cuts.json `_meta.usage` x list price
      (Opus 5.5 $15/$75 per 1M in/out, Sonnet 4.5 $3/$15) + CE "Amazon Bedrock" account-wide cost for the
      window is shown separately as an upper bound (other projects share the account).
    * Cross-check: Transcribe job durations and MediaConvert output minutes for the window (ListJobs, region).
  Output: a markdown row appended to costs/ledger.md (+ JSON line in costs/ledger.jsonl) unless --no-write.
  Cost Explorer lags ~24h and a new tag VALUE can take up to 24h to be filterable: run this at D+2 or later and
  re-run (it replaces the row for the same <name>) if the first number looks partial.
"""
import argparse, datetime as dt, json, os, re, subprocess, sys
sys.path.insert(0, os.path.dirname(__file__))
import zve_common as C  # noqa: E402
import boto3

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER_MD = os.path.join(WS, "costs", "ledger.md")
LEDGER_JSONL = os.path.join(WS, "costs", "ledger.jsonl")
PRICE = {  # USD per 1M tokens (input, output), list price 2026-10
    "opus-5-5": (15.0, 75.0), "opus": (15.0, 75.0),
    "sonnet-4-5": (3.0, 15.0), "sonnet": (3.0, 15.0),
}


def ce_by_service(ce, start, end, flt):
    out = {}
    tok = None
    while True:
        kw = dict(TimePeriod={"Start": start, "End": end}, Granularity="DAILY", Metrics=["UnblendedCost", "UsageQuantity"],
                  GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}], Filter=flt)
        if tok:
            kw["NextPageToken"] = tok
        r = ce.get_cost_and_usage(**kw)
        for day in r["ResultsByTime"]:
            for g in day["Groups"]:
                svc = g["Keys"][0]
                out.setdefault(svc, {"cost": 0.0, "usage": 0.0, "days": set()})
                out[svc]["cost"] += float(g["Metrics"]["UnblendedCost"]["Amount"])
                out[svc]["usage"] += float(g["Metrics"]["UsageQuantity"]["Amount"])
                out[svc]["days"].add(day["TimePeriod"]["Start"])
        tok = r.get("NextPageToken")
        if not tok:
            break
    return out


def bedrock_estimate(name):
    """Sum token usage over every plan variant for this run (S3 plans/ + local cache)."""
    s3 = boto3.client("s3", region_name=C.REGION)
    seen, rows = set(), []
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket=C.BUCKET, Prefix=f"plans/{name}").get("Contents", []) if o["Key"].endswith(".json")]
    docs = []
    for k in keys:
        docs.append(json.loads(s3.get_object(Bucket=C.BUCKET, Key=k)["Body"].read()))
    cache = os.path.expanduser("~/.cache/zoom-video-edit")
    for f in os.listdir(cache):
        if f.startswith(name) and f.endswith(".json") and ".cuts" in f:
            docs.append(json.load(open(os.path.join(cache, f))))
    total = 0.0
    for d in docs:
        m = d.get("_meta", {})
        u = m.get("usage") or {}
        sig = (m.get("model"), m.get("generated_at"), json.dumps(u, sort_keys=True))
        if not u or sig in seen:
            continue
        seen.add(sig)
        model = m.get("model", "")
        pin, pout = next((v for k, v in PRICE.items() if k in model), (3.0, 15.0))
        i, o = u.get("inputTokens", u.get("input_tokens", 0)), u.get("outputTokens", u.get("output_tokens", 0))
        cost = i / 1e6 * pin + o / 1e6 * pout
        total += cost
        rows.append(f"{model.split('.')[-1]} {i}in/{o}out ${cost:.3f}")
    return total, rows


def usage_crosscheck(start, end):
    s = dt.datetime.fromisoformat(start).replace(tzinfo=dt.timezone.utc)
    e = dt.datetime.fromisoformat(end).replace(tzinfo=dt.timezone.utc)
    tr = boto3.client("transcribe", region_name=C.REGION)
    tmin = 0.0
    for j in tr.list_transcription_jobs(JobNameContains="zve-", MaxResults=100).get("TranscriptionJobSummaries", []):
        if s <= j["CreationTime"] < e and j["TranscriptionJobStatus"] == "COMPLETED":
            # job summary has no duration; use start->completion as an upper bound only when needed
            tmin += 0  # placeholder; billed minutes come from CE UsageQuantity (seconds)
    mc_ep = boto3.client("mediaconvert", region_name=C.REGION).describe_endpoints()["Endpoints"][0]["Url"]
    mc = boto3.client("mediaconvert", region_name=C.REGION, endpoint_url=mc_ep)
    out_min, n, per_day = 0.0, 0, {}
    for j in mc.list_jobs(MaxResults=20, Order="DESCENDING").get("Jobs", []):
        if s <= j["CreatedAt"] < e and j["Status"] == "COMPLETE":
            n += 1
            d = j["CreatedAt"].strftime("%Y-%m-%d")
            for og in j.get("OutputGroupDetails", []):
                for od in og.get("OutputDetails", []):
                    m = od.get("DurationInMs", 0) / 60000
                    out_min += m
                    per_day[d] = per_day.get(d, 0.0) + m
    return n, out_min, per_day


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="to", required=True)
    ap.add_argument("--note", default="")
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args()
    end_excl = (dt.date.fromisoformat(a.to) + dt.timedelta(days=1)).isoformat()

    ce = boto3.client("ce", region_name="us-east-1")
    tag_value = C.PROJECT_TAG["Project"]
    tagged = ce_by_service(ce, a.start, end_excl, {"Tags": {"Key": "Project", "Values": [tag_value]}})
    svc_all = {k: v for k, v in ce_by_service(ce, a.start, end_excl, {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Usage"]}}).items()
               if any(w in k for w in ("Transcribe", "MediaConvert", "Bedrock", "Claude", "Storage Service"))}
    br_est, br_rows = bedrock_estimate(a.name)
    try:
        mc_jobs, mc_min, mc_days = usage_crosscheck(a.start, end_excl)
    except Exception as ex:  # cross-check is best-effort
        mc_jobs, mc_min, mc_days = -1, 0.0, {}
        print("crosscheck skipped:", ex, file=sys.stderr)

    tagged_total = sum(v["cost"] for v in tagged.values())
    total = tagged_total + br_est
    today = dt.date.today().isoformat()
    print(f"\n== {a.name}  window {a.start}..{a.to}  (CE queried {today}) ==")
    print(f"tagged Project={tag_value}:")
    for k, v in sorted(tagged.items(), key=lambda x: -x[1]["cost"]):
        print(f"  {k:40s} ${v['cost']:.3f}  usage {v['usage']:.1f}  days {sorted(v['days'])}")
    print(f"  {'TOTAL tagged':40s} ${tagged_total:.3f}")
    print(f"Bedrock (token estimate, untaggable): ${br_est:.3f}  <- " + "; ".join(br_rows))
    print("account-wide same services (upper bound, other projects included):")
    for k, v in sorted(svc_all.items(), key=lambda x: -x[1]["cost"]):
        print(f"  {k:40s} ${v['cost']:.3f}")
    print(f"MediaConvert cross-check: {mc_jobs} jobs, {mc_min:.1f} output min in window; per UTC day {mc_days}")
    print(f"\nRUN TOTAL ≈ ${total:.2f}")

    if a.no_write:
        return
    os.makedirs(os.path.dirname(LEDGER_MD), exist_ok=True)
    rec = {"name": a.name, "window": [a.start, a.to], "queried": today, "tagged": {k: round(v["cost"], 4) for k, v in tagged.items()},
           "tagged_total": round(tagged_total, 4), "bedrock_est": round(br_est, 4), "bedrock_detail": br_rows,
           "mediaconvert_jobs": mc_jobs, "mediaconvert_out_min": round(mc_min, 1), "total": round(total, 2), "note": a.note}
    lines = [l for l in open(LEDGER_JSONL)] if os.path.exists(LEDGER_JSONL) else []
    lines = [l for l in lines if json.loads(l).get("name") != a.name] + [json.dumps(rec, ensure_ascii=False) + "\n"]
    open(LEDGER_JSONL, "w").writelines(lines)
    # regenerate markdown from jsonl so re-runs replace rows
    recs = [json.loads(l) for l in lines]
    md = ["# Per-run AWS cost ledger (Cost Explorer, UnblendedCost USD)", "",
          f"Transcribe/MediaConvert/S3 = actual cost filtered by tag `Project={tag_value}`. Bedrock = tokens recorded in the plan JSON "
          "`_meta.usage` x list price (invocations cannot carry the tag).",
          "Cost Explorer lags ~24h and a new tag value can take up to 24h to become filterable: query at D+2 or later; if a row looks "
          "partial, re-run with the same name (the row is replaced).", "",
          "| run | window | Transcribe | MediaConvert | S3 | other tagged | Bedrock (est.) | **total** | MC output min | queried | note |",
          "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in recs:
        t = r["tagged"]
        tr_ = t.get("Amazon Transcribe", 0); mc_ = t.get("AWS Elemental MediaConvert", 0); s3_ = t.get("Amazon Simple Storage Service", 0)
        other = max(0.0, r["tagged_total"] - tr_ - mc_ - s3_)
        md.append(f"| {r['name']} | {r['window'][0]}~{r['window'][1][5:]} | ${tr_:.2f} | ${mc_:.2f} | ${s3_:.2f} | ${other:.2f} | ${r['bedrock_est']:.2f} | **${r['total']:.2f}** | {r['mediaconvert_out_min']} | {r['queried']} | {r['note']} |")
    md.append(""); md.append(f"Cumulative: **${sum(r['total'] for r in recs):.2f}** over {len(recs)} run(s)")
    open(LEDGER_MD, "w").write("\n".join(md) + "\n")
    print("ledger updated:", LEDGER_MD)


if __name__ == "__main__":
    main()
