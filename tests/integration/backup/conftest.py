#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for S3 backup integration tests, backed by MicroCeph."""

from __future__ import annotations

import json
import secrets
import subprocess
from pathlib import Path

import boto3
import pytest
from botocore.config import Config


def _run(*cmd: str, **kwargs) -> str:
    return subprocess.check_output(cmd, text=True, **kwargs).strip()


@pytest.fixture(scope="module")
def microceph() -> dict:
    """Install MicroCeph snap, bootstrap, enable rgw with TLS, create a user + bucket."""
    _run("sudo", "snap", "install", "microceph", "--channel=squid/stable")
    _run("sudo", "microceph", "cluster", "bootstrap")
    _run("sudo", "microceph", "disk", "add", "loop,4G,3", "--wipe")
    _run("sudo", "microceph", "enable", "rgw", "--ssl-port=445", "--ssl-certificate=auto")

    user_json = _run(
        "sudo",
        "radosgw-admin",
        "user",
        "create",
        "--uid=test",
        "--display-name=test",
    )
    user = json.loads(user_json)
    access_key = user["keys"][0]["access_key"]
    secret_key = user["keys"][0]["secret_key"]

    bucket_name = f"valkey-backup-{secrets.token_hex(4)}"
    cert = Path("/var/snap/microceph/current/conf/ssl-cert.pem").read_text()

    return {
        "endpoint": "https://127.0.0.1:445",
        "access-key": access_key,
        "secret-key": secret_key,
        "bucket": bucket_name,
        "region": "default",
        "path": "valkey",
        "tls-ca-chain": [cert],
    }


@pytest.fixture(scope="module")
def s3_bucket(microceph):
    """Return a boto3 Bucket resource for the test bucket, creating it eagerly."""
    s3 = boto3.resource(
        "s3",
        region_name=microceph["region"],
        endpoint_url=microceph["endpoint"],
        aws_access_key_id=microceph["access-key"],
        aws_secret_access_key=microceph["secret-key"],
        config=Config(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
        verify=False,
    )
    bucket = s3.Bucket(microceph["bucket"])
    bucket.create()
    bucket.wait_until_exists()
    return bucket


@pytest.fixture(scope="module")
def s3_secret_content(microceph) -> dict:
    return {
        "access-key": microceph["access-key"],
        "secret-key": microceph["secret-key"],
    }
