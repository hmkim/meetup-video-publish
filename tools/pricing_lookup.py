#!/usr/bin/env python3
"""Look up ap-northeast-2 list prices used in README: Transcribe batch, MediaConvert basic/pro tier, Bedrock Sonnet 4.5."""
import json
import sys

sys.path.insert(0, __file__.rsplit("/tools/", 1)[0] + "/scripts")
import zve_common as C  # noqa: E402

pr = C.client("pricing", region="us-east-1")


def products(service, filters):
    out = []
    pag = pr.get_paginator("get_products")
    for page in pag.paginate(ServiceCode=service, Filters=[{"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in filters.items()]):
        out += [json.loads(p) for p in page["PriceList"]]
    return out


def price_rows(prods):
    rows = []
    for p in prods:
        a = p["product"]["attributes"]
        for term in p["terms"].get("OnDemand", {}).values():
            for dim in term["priceDimensions"].values():
                rows.append((a, dim))
    return rows


print("== Transcribe (Asia Pacific (Seoul))")
for a, d in price_rows(products("transcribe", {"location": "Asia Pacific (Seoul)"})):
    print(f"  {a.get('usagetype'):45s} {a.get('operation', ''):25s} {d['pricePerUnit'].get('USD')} /{d['unit']:8s} {d['description'][:70]}")

print("\n== MediaConvert (Asia Pacific (Seoul))  [filtered to 1080p/SD/720p, basic+pro tier, <=30fps]")
for a, d in price_rows(products("AWSElementalMediaConvert", {"location": "Asia Pacific (Seoul)"})):
    desc = d["description"]
    if "AVC" in desc or "avc" in a.get("usagetype", "").lower() or "Basic" in desc or "Audio" in desc:
        if any(k in desc for k in ("1080", "720", "SD", "Audio", "Basic")) and ("30fps" in desc.replace(" ", "") or "Audio" in desc or "<=30" in desc):
            print(f"  {a.get('usagetype'):50s} {d['pricePerUnit'].get('USD')} /{d['unit']:8s} {desc[:110]}")

print("\n== Bedrock Claude Sonnet 4.5 (any region rows found)")
for a, d in price_rows(products("AmazonBedrock", {"model": "Claude Sonnet 4.5"} )):
    if "Seoul" in a.get("location", "") or "Global" in a.get("location", "") or "US East (N. Virginia)" in a.get("location", ""):
        print(f"  {a.get('location'):28s} {a.get('inferenceType', ''):20s} {a.get('usagetype'):55s} {d['pricePerUnit'].get('USD')} /{d['unit']}")
