"""
Reading and writing files in the R2 bucket.

R2 is Cloudflare's file storage, and it speaks the same protocol as Amazon S3.
That means we can use boto3 (Amazon's S3 library) and simply point it at
Cloudflare's endpoint instead of Amazon's.

S3 wants an "access key ID" and a "secret access key" rather than an API
token. Cloudflare lets you derive both from a normal API token:

    access key ID     = the token's ID
    secret access key = SHA-256 hash of the token's value

https://developers.cloudflare.com/r2/api/tokens/
"""

import hashlib
from datetime import datetime
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig


class Storage:
    def __init__(self, account_id: str, token_id: str, token: str, bucket: str):
        self.bucket = bucket
        self.s3 = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=token_id,
            aws_secret_access_key=hashlib.sha256(token.encode()).hexdigest(),
            region_name="auto",  # R2 has no regions; "auto" is what it expects
            # Newer boto3 versions add extra checksums to every upload by
            # default. Only send them when an operation requires it, which is
            # the safest choice for S3-compatible services like R2.
            config=BotoConfig(
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    def upload(self, key: str, path: Path, content_type: str) -> None:
        # The Content-Type is stored with the file, and the Worker passes it on
        # to browsers so they know to render HTML instead of downloading it.
        self.s3.upload_file(str(path), self.bucket, key, ExtraArgs={"ContentType": content_type})

    def list_files(self, prefix: str = "") -> list[dict]:
        """
        Every file whose key starts with `prefix`, as dicts like
        {"key": "demo.example.com/index.html", "size": 1234, "modified": datetime}.
        """
        files = []
        # S3 returns at most 1000 keys per request; the paginator fetches the rest.
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                files.append({"key": item["Key"], "size": item["Size"], "modified": item["LastModified"]})
        return files

    def delete(self, keys: list[str]) -> None:
        # delete_objects accepts at most 1000 keys per call.
        for start in range(0, len(keys), 1000):
            batch = keys[start:start + 1000]
            self.s3.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )

    def check_access(self) -> None:
        """Write, then delete, a tiny file. Raises if the credentials don't work."""
        key = f"_ship-check-{datetime.now():%Y%m%d%H%M%S}"
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=b"ok")
        self.s3.delete_object(Bucket=self.bucket, Key=key)
