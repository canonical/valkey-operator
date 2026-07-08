#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""End-to-end S3 backup integration test against MicroCeph."""

from __future__ import annotations

import time

import jubilant

from literals import Substrate
from tests.integration.backup.helpers import BACKUP_ID_RE, deploy_and_relate_s3
from tests.integration.helpers import APP_NAME


def test_backup_and_list(
    charm: str,
    juju: jubilant.Juju,
    substrate: Substrate,
    microceph: dict,
    s3_bucket,
) -> None:
    deploy_and_relate_s3(juju, charm, substrate, microceph)

    # Three distinct units exercise the any-unit backup guarantee.
    units = list(juju.status().get_units(APP_NAME))
    assert len(units) >= 3, units

    # Backup from the first unit.
    task0 = juju.run(units[0], "create-backup")
    assert task0.success, task0.stderr
    backup_id_0 = task0.results["backup-id"]
    assert BACKUP_ID_RE.match(backup_id_0), backup_id_0

    # Backup from a second unit (distinct id, exercises any-unit guarantee).
    time.sleep(2)
    task1 = juju.run(units[1], "create-backup")
    assert task1.success, task1.stderr
    backup_id_1 = task1.results["backup-id"]
    assert backup_id_1 != backup_id_0

    # List from a third unit. Newest first.
    listing = juju.run(units[2], "list-backups")
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
