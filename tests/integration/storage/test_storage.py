#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Integration: the logs and archive storage volumes are attached and used."""

import logging

import jubilant

from literals import Substrate
from tests.integration.helpers import (
    APP_NAME,
    IMAGE_RESOURCE,
    are_apps_active_and_agents_idle,
    get_storage_id,
)

logger = logging.getLogger(__name__)

NUM_UNITS = 3


def test_build_and_deploy(charm: str, juju: jubilant.Juju, substrate: Substrate) -> None:
    """Deploy and reach active/idle; the new storages must attach."""
    juju.deploy(
        charm,
        resources=IMAGE_RESOURCE if substrate == Substrate.K8S else None,
        num_units=NUM_UNITS,
        trust=True,
    )
    juju.wait(
        lambda status: are_apps_active_and_agents_idle(status, APP_NAME),
        timeout=900,
    )


def test_logs_and_archive_storage_attached(juju: jubilant.Juju) -> None:
    """`juju storage` lists the logs and archive volumes for unit 0."""
    unit = f"{APP_NAME}/0"
    assert get_storage_id(juju, unit, "logs") is not None
    assert get_storage_id(juju, unit, "archive") is not None


def test_log_files_present_in_logs_volume(juju: jubilant.Juju, substrate: Substrate) -> None:
    """valkey.log and sentinel.log exist in the logs volume; archive is writable."""
    unit = f"{APP_NAME}/0"
    if substrate == Substrate.K8S:
        log_dir, archive_dir, container = "/var/log/valkey", "/var/backups/valkey", "valkey"
        prefix = ["--container", container]
    else:
        log_dir = "/var/snap/charmed-valkey/common/var/log/charmed-valkey"
        archive_dir = "/var/snap/charmed-valkey/common/archive"
        prefix = []

    listing = juju.cli("ssh", *prefix, unit, f"ls -1 {log_dir}")
    assert "valkey.log" in listing
    assert "sentinel.log" in listing

    # archive mount exists and is writable by the workload user
    juju.cli("ssh", *prefix, unit, f"test -d {archive_dir}")
