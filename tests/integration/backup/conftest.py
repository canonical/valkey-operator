#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for S3 backup integration tests, backed by MicroCeph.

MicroCeph's RGW is fronted with a self-signed TLS certificate generated here,
so the suite exercises the charm's full S3-over-TLS path (CA-chain
distribution + boto3 verification), not just plaintext S3. The certificate's
SAN covers the host's routable IP because the charm units (k8s pods / lxd
machines) reach the gateway over that address -- never loopback, which from a
unit resolves to the unit itself.

Every MicroCeph step is idempotent so the suite can be re-run locally without
tearing the cluster down between runs.
"""

from __future__ import annotations

import base64
import json
import secrets
import socket
import subprocess
import time
from pathlib import Path

import boto3
import pytest
from botocore.config import Config

RGW_SSL_PORT = 445
# Self-signed cert + key, persisted so repeated local runs reuse the exact
# material the already-running gateway serves -- regenerating without
# re-enabling RGW would break TLS verification against the old certificate.
_CERT_DIR = Path.home() / ".cache" / "valkey-itest"
_CERT = _CERT_DIR / "rgw-cert.pem"
_KEY = _CERT_DIR / "rgw-key.pem"


def _run(*cmd: str, **kwargs) -> str:
    return subprocess.check_output(cmd, text=True, **kwargs).strip()


def _ok(*cmd: str) -> bool:
    """Return True if the command exits 0 (idempotent check-then-act helper)."""
    return subprocess.run(cmd, capture_output=True).returncode == 0


def _host_ip() -> str:
    """First routable IPv4 of this host, reachable from charm units."""
    return _run("hostname", "-I").split()[0]


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex((host, port)) == 0


def _ensure_microceph() -> None:
    """Install + bootstrap MicroCeph with OSDs; each step idempotent."""
    if not _ok("snap", "list", "microceph"):
        _run("sudo", "snap", "install", "microceph", "--channel=squid/stable")
    if not _ok("sudo", "microceph", "status"):
        _run("sudo", "microceph", "cluster", "bootstrap")
        _run("sudo", "microceph", "disk", "add", "loop,4G,3", "--wipe")


def _ensure_rgw_tls(host_ip: str) -> str:
    """Serve RGW over TLS on RGW_SSL_PORT with a SAN-correct self-signed cert.

    Returns the certificate PEM. It is self-signed, so it is also the CA chain
    handed to s3-integrator.
    """
    have_cert = _CERT.exists() and _KEY.exists()
    if not have_cert:
        _CERT_DIR.mkdir(parents=True, exist_ok=True)
        _run(
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", _KEY.as_posix(), "-out", _CERT.as_posix(),
            "-days", "3650", "-subj", "/CN=valkey-microceph-rgw",
            "-addext", f"subjectAltName=IP:{host_ip},IP:127.0.0.1,DNS:localhost",
        )  # fmt: skip
    # (Re)configure RGW when the cert is freshly minted or the gateway is down:
    # a new certificate must replace whatever the running gateway presents.
    if not have_cert or not _port_open(host_ip, RGW_SSL_PORT):
        subprocess.run(["sudo", "microceph", "disable", "rgw"], capture_output=True)
        _run(
            "sudo", "microceph", "enable", "rgw",
            f"--ssl-port={RGW_SSL_PORT}",
            f"--ssl-certificate={base64.b64encode(_CERT.read_bytes()).decode()}",
            f"--ssl-private-key={base64.b64encode(_KEY.read_bytes()).decode()}",
        )  # fmt: skip
    for _ in range(30):
        if _port_open(host_ip, RGW_SSL_PORT):
            break
        time.sleep(1)
    return _CERT.read_text()


def _ensure_user(uid: str = "test") -> tuple[str, str]:
    """Get-or-create an RGW user; return (access_key, secret_key)."""
    if _ok("sudo", "radosgw-admin", "user", "info", f"--uid={uid}"):
        out = _run("sudo", "radosgw-admin", "user", "info", f"--uid={uid}")
    else:
        out = _run(
            "sudo", "radosgw-admin", "user", "create",
            f"--uid={uid}", f"--display-name={uid}",
        )  # fmt: skip
    keys = json.loads(out)["keys"][0]
    return keys["access_key"], keys["secret_key"]


@pytest.fixture(scope="module")
def microceph() -> dict:
    """Install MicroCeph, serve TLS RGW, and return its S3 connection params."""
    _ensure_microceph()
    host_ip = _host_ip()
    cert = _ensure_rgw_tls(host_ip)
    access_key, secret_key = _ensure_user()

    return {
        "endpoint": f"https://{host_ip}:{RGW_SSL_PORT}",
        "access-key": access_key,
        "secret-key": secret_key,
        "bucket": f"valkey-backup-{secrets.token_hex(4)}",
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
