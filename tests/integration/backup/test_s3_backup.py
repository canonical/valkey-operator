#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""End-to-end S3 backup integration test against MicroCeph."""

from __future__ import annotations

import re
import time

import jubilant
import pytest

APP_NAME = "valkey"
S3_INTEGRATOR_APP = "s3-integrator"
BACKUP_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@pytest.fixture(scope="module")
def juju() -> jubilant.Juju:
    return jubilant.Juju()


def _wait_active(juju: jubilant.Juju, *apps: str, timeout: int = 600) -> None:
    juju.wait(
        lambda status: all(
            app in status.apps
            and status.apps[app].is_active
            and all(unit.is_active for unit in status.apps[app].units.values())
            for app in apps
        ),
        timeout=timeout,
    )


def test_backup_and_list(juju: jubilant.Juju, microceph: dict, s3_bucket) -> None:
    juju.deploy(APP_NAME, num_units=3, trust=True, base="ubuntu@24.04")
    juju.deploy(S3_INTEGRATOR_APP, channel="latest/edge")

    juju.config(
        S3_INTEGRATOR_APP,
        {
            "bucket": microceph["bucket"],
            "endpoint": microceph["endpoint"],
            "region": microceph["region"],
            "path": microceph["path"],
            "s3-uri-style": "path",
        },
    )

    secret_id = juju.cli(
        "add-secret",
        "s3-creds",
        f"access-key={microceph['access-key']}",
        f"secret-key={microceph['secret-key']}",
    ).strip()
    juju.cli("grant-secret", "s3-creds", S3_INTEGRATOR_APP)
    juju.config(S3_INTEGRATOR_APP, {"credentials": secret_id})

    _wait_active(juju, S3_INTEGRATOR_APP)
    juju.integrate(APP_NAME, S3_INTEGRATOR_APP)
    _wait_active(juju, APP_NAME, S3_INTEGRATOR_APP)

    # Backup from unit/0
    task0 = juju.run(f"{APP_NAME}/0", "create-backup")
    assert task0.success, task0.stderr
    backup_id_0 = task0.results["backup-id"]
    assert BACKUP_ID_RE.match(backup_id_0), backup_id_0

    # Backup from unit/1 (different unit, separate id, exercises any-unit guarantee)
    time.sleep(2)
    task1 = juju.run(f"{APP_NAME}/1", "create-backup")
    assert task1.success, task1.stderr
    backup_id_1 = task1.results["backup-id"]
    assert backup_id_1 != backup_id_0

    # List from unit/2. Newest first.
    listing = juju.run(f"{APP_NAME}/2", "list-backups")
    assert listing.success
    table = listing.results["backups"]
    assert backup_id_0 in table
    assert backup_id_1 in table
    assert table.index(backup_id_1) < table.index(backup_id_0)

    # Verify objects exist in the bucket.
    keys = [obj.key for obj in s3_bucket.objects.filter(Prefix=microceph["path"])]
    assert any(backup_id_0 in k for k in keys)
    assert any(backup_id_1 in k for k in keys)

    # Validate RDB magic bytes for the first object.
    obj = next(
        o for o in s3_bucket.objects.filter(Prefix=microceph["path"]) if backup_id_0 in o.key
    )
    head = obj.get()["Body"].read(9)
    assert head.startswith(b"REDIS") or head.startswith(b"VALKEY"), head
