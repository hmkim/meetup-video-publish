#!/usr/bin/env python3
"""Print a presigned PUT URL for s3://<bucket>/<key>. Usage: presign_put.py <bucket> <key> <seconds>"""
import os
import sys
import boto3
from botocore.config import Config

bucket, key, secs = sys.argv[1], sys.argv[2], int(sys.argv[3])
s3 = boto3.client("s3", region_name=os.environ.get("ZVE_REGION", "ap-northeast-2"), config=Config(signature_version="s3v4"))
url = s3.generate_presigned_url(
    "put_object",
    Params={"Bucket": bucket, "Key": key},
    ExpiresIn=secs,
    HttpMethod="PUT",
)
print(url)
