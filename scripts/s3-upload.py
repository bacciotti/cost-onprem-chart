#!/usr/bin/env python3
"""Upload a directory to S3 with forced IPv4 resolution.

Bypasses broken IPv6 in split-site labs by filtering AF_INET6 from
socket.getaddrinfo before any connections are made.  SNI and Host
headers remain correct because the original hostname is preserved.

The target S3 bucket for this project's perf artifacts is typically
**public / anonymous** on a shared, internal S3-compatible endpoint — no
AWS credentials are required. TLS verification is skipped by default
because that endpoint uses a certificate that is not in the system trust
store.

Usage:
    python3 s3-upload.py sync  <local-dir> <s3-uri> [--endpoint-url URL]
    python3 s3-upload.py cp    <local-file> <s3-uri> [--endpoint-url URL]
    python3 s3-upload.py ls    <s3-uri>              [--endpoint-url URL]

Environment:
    S3_ENDPOINT          – default --endpoint-url
    S3_NO_VERIFY_SSL     – disable TLS verify  (default: true — untrusted certs are common on internal S3 endpoints)
    S3_NO_SIGN_REQUEST   – anonymous access     (default: true — bucket is public, no creds needed)
    S3_UPLOAD_TIMEOUT    – per-file timeout in seconds (default: 60)
"""
import os
import socket
import sys

_orig_getaddrinfo = socket.getaddrinfo

def _ipv4_only_getaddrinfo(*args, **kwargs):
    results = _orig_getaddrinfo(*args, **kwargs)
    ipv4 = [r for r in results if r[0] == socket.AF_INET]
    return ipv4 if ipv4 else results

socket.getaddrinfo = _ipv4_only_getaddrinfo

import argparse
import mimetypes
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", message="Unverified HTTPS request")

try:
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config as BotoConfig
except ImportError:
    sys.exit("boto3 is required: pip install boto3")


def _build_client(endpoint_url: str, verify: bool, sign: bool):
    cfg = BotoConfig(
        signature_version=UNSIGNED if not sign else None,
        s3={"addressing_style": "path"},
        connect_timeout=10,
        read_timeout=int(os.environ.get("S3_UPLOAD_TIMEOUT", "60")),
        retries={"max_attempts": 2},
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url or None,
        config=cfg,
        verify=verify,
    )


def _parse_s3_uri(uri: str):
    """Split an s3:// URI into (bucket, key/prefix), preserving any trailing
    slash exactly as given.

    The trailing slash is semantically meaningful: for `ls`, a prefix ending
    in "/" means "list the contents of this directory" — stripping it before
    a Delimiter="/" listing makes every key immediately below collapse into
    a single CommonPrefix identical to the query itself (since the very next
    character after the un-slashed prefix in every matching key is "/"),
    which hides all actual objects. For `cp`, a trailing slash means "treat
    this as a destination directory and keep the source filename" (see
    cmd_cp). Callers that want a bare prefix/key (no trailing slash) should
    simply not include one in the URI.
    """
    if not uri.startswith("s3://"):
        sys.exit(f"Invalid S3 URI: {uri}")
    parts = uri[5:].split("/", 1)
    bucket = parts[0]
    prefix = parts[1] if len(parts) > 1 else ""
    return bucket, prefix


def cmd_ls(client, s3_uri: str):
    bucket, prefix = _parse_s3_uri(s3_uri)
    paginator = client.get_paginator("list_objects_v2")
    count = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            print(f"                           PRE {cp['Prefix']}")
            count += 1
        for obj in page.get("Contents", []):
            print(f"{obj['LastModified']}  {obj['Size']:>10}  {obj['Key']}")
            count += 1
    return 0 if count else 1


def cmd_cp(client, src: str, dst: str):
    """Copy a file to or from S3, direction inferred from which side is an s3:// URI.

    cp <local-file> <s3-uri>   uploads
    cp <s3-uri> <local-file>   downloads
    cp <s3-uri> -              downloads to stdout
    """
    src_is_s3 = src.startswith("s3://")
    dst_is_s3 = dst.startswith("s3://")

    if src_is_s3 and dst_is_s3:
        sys.exit("cp does not support s3-to-s3 copies; use --copy-object directly")
    if not src_is_s3 and not dst_is_s3:
        sys.exit(f"Neither {src!r} nor {dst!r} is an s3:// URI")

    if dst_is_s3:
        bucket, key = _parse_s3_uri(dst)
        p = Path(src)
        if not p.is_file():
            sys.exit(f"Not a file: {src}")
        if not key or key.endswith("/"):
            key = (key + "/" if key and not key.endswith("/") else key) + p.name
        content_type = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        client.upload_file(str(p), bucket, key, ExtraArgs={"ContentType": content_type})
        print(f"upload: {src} -> s3://{bucket}/{key}")
    else:
        bucket, key = _parse_s3_uri(src)
        if not key or key.endswith("/"):
            sys.exit(f"Not a single object (looks like a prefix): {src}")
        if dst == "-":
            obj = client.get_object(Bucket=bucket, Key=key)
            sys.stdout.buffer.write(obj["Body"].read())
        else:
            client.download_file(bucket, key, dst)
            print(f"download: s3://{bucket}/{key} -> {dst}")
    return 0


def cmd_sync(client, local_dir: str, s3_uri: str):
    bucket, prefix = _parse_s3_uri(s3_uri)
    src = Path(local_dir)
    if not src.is_dir():
        sys.exit(f"Not a directory: {local_dir}")

    files = sorted(f for f in src.rglob("*") if f.is_file())
    uploaded = 0
    errors = 0
    prefix = prefix.rstrip("/")
    for f in files:
        rel = f.relative_to(src)
        key = f"{prefix}/{rel}" if prefix else str(rel)
        content_type = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
        try:
            client.upload_file(str(f), bucket, key, ExtraArgs={"ContentType": content_type})
            uploaded += 1
        except Exception as e:
            print(f"ERROR uploading {rel}: {e}", file=sys.stderr)
            errors += 1

    print(f"upload: {uploaded} file(s) to s3://{bucket}/{prefix}/", end="")
    if errors:
        print(f" ({errors} error(s))", end="")
    print()
    return 1 if errors else 0


def main():
    parser = argparse.ArgumentParser(description="S3 upload with forced IPv4")
    parser.add_argument("command", choices=["sync", "cp", "ls"])
    parser.add_argument("src", nargs="?")
    parser.add_argument("dst", nargs="?")
    parser.add_argument("--endpoint-url", default=os.environ.get("S3_ENDPOINT", ""))
    # The perf-results bucket is typically public and its endpoint uses an
    # untrusted cert, so both default to true.  Override with "false" to
    # re-enable verification or request signing when targeting a different
    # S3 backend.
    parser.add_argument("--no-verify-ssl", action="store_true",
                        default=os.environ.get("S3_NO_VERIFY_SSL", "true").lower() == "true")
    parser.add_argument("--no-sign-request", action="store_true",
                        default=os.environ.get("S3_NO_SIGN_REQUEST", "true").lower() == "true")

    args = parser.parse_args()
    client = _build_client(args.endpoint_url, not args.no_verify_ssl, not args.no_sign_request)

    if args.command == "ls":
        return cmd_ls(client, args.src or args.dst or "")
    elif args.command == "cp":
        return cmd_cp(client, args.src, args.dst)
    elif args.command == "sync":
        return cmd_sync(client, args.src, args.dst)


if __name__ == "__main__":
    sys.exit(main() or 0)
