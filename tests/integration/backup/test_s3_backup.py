#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""End-to-end S3 backup integration test against MicroCeph."""

from __future__ import annotations

import base64
import re
import time

import jubilant

from literals import Substrate
from tests.integration.helpers import (
    APP_NAME,
    IMAGE_RESOURCE,
    are_apps_active_and_agents_idle,
)

S3_INTEGRATOR_APP = "s3-integrator"
BACKUP_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_backup_and_list(
    charm: str,
    juju: jubilant.Juju,
    substrate: Substrate,
    microceph: dict,
    s3_bucket,
) -> None:
    juju.deploy(
        charm,
        resources=IMAGE_RESOURCE if substrate == Substrate.K8S else None,
        num_units=3,
        trust=True,
    )
    juju.deploy(S3_INTEGRATOR_APP, channel="2/edge")

    # s3-integrator 2/edge takes credentials as a Juju secret (the
    # sync-s3-credentials action is gone in v2): create + grant the secret,
    # then point the `credentials` config at its URI.
    creds = juju.add_secret(
        name="s3-creds",
        content={"access-key": microceph["access-key"], "secret-key": microceph["secret-key"]},
    )
    juju.grant_secret(identifier=creds, app=S3_INTEGRATOR_APP)

    # s3-integrator base64-decodes tls-ca-chain, so the charm can verify
    # MicroCeph's self-signed RGW endpoint over TLS. Without it the charm
    # falls back to the system trust store and every S3 call fails.
    ca_chain = base64.b64encode(microceph["tls-ca-chain"][0].encode()).decode()
    juju.config(
        S3_INTEGRATOR_APP,
        {
            "credentials": creds,
            "bucket": microceph["bucket"],
            "endpoint": microceph["endpoint"],
            "region": microceph["region"],
            "path": microceph["path"],
            "s3-uri-style": "path",
            "tls-ca-chain": ca_chain,
        },
    )

    # Require agents idle as well as workloads active: after `integrate` the
    # workloads stay "active" while the relation hooks (the leader's
    # create_bucket + credential storage) are still running, so a workload-only
    # wait can return before S3 is actually wired up.
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, S3_INTEGRATOR_APP, idle_period=30
        ),
        timeout=1000,
        delay=5,
        successes=3,
    )
    juju.integrate(APP_NAME, S3_INTEGRATOR_APP)
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(
            status, APP_NAME, S3_INTEGRATOR_APP, idle_period=30
        ),
        timeout=1000,
        delay=5,
        successes=3,
    )

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
