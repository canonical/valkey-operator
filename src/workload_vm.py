#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Implementation of WorkloadBase for running Valkey on VMs."""

import logging
import subprocess
from typing import List, override

from charmlibs import pathops, snap
from tenacity import Retrying, retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from common.exceptions import ValkeyWorkloadCommandError
from core.base_workload import WorkloadBase
from literals import (
    SNAP_ACL_FILE,
    SNAP_COMMON_PATH,
    SNAP_CONFIG_FILE,
    SNAP_CURRENT_PATH,
    SNAP_NAME,
    SNAP_REVISION,
    SNAP_SENTINEL_ACL_FILE,
    SNAP_SENTINEL_CONFIG_FILE,
    SNAP_SERVICE,
)

logger = logging.getLogger(__name__)


class ValkeyVmWorkload(WorkloadBase):
    """Implementation of WorkloadBase for running a Valkey container on VM."""

    def __init__(self) -> None:
        for attempt in Retrying(stop=stop_after_attempt(5), wait=wait_fixed(5)):
            with attempt:
                self.valkey = snap.SnapCache()[SNAP_NAME]

        self.root = pathops.LocalPath("/")
        self.config_file = self.root / SNAP_CURRENT_PATH / SNAP_CONFIG_FILE
        self.sentinel_config = self.root / SNAP_CURRENT_PATH / SNAP_SENTINEL_CONFIG_FILE
        self.acl_file = self.root / SNAP_CURRENT_PATH / SNAP_ACL_FILE
        self.sentinel_acl_file = self.root / SNAP_CURRENT_PATH / SNAP_SENTINEL_ACL_FILE
        self.working_dir = self.root / SNAP_COMMON_PATH / "var/lib/charmed-valkey"
        self.cli = "charmed-valkey.cli"

    @property
    @override
    def can_connect(self) -> bool:
        try:
            return bool(self.valkey.services[SNAP_SERVICE])
        except KeyError:
            return False

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(5),
        reraise=True,
        retry=retry_if_exception_type(RuntimeError),
    )
    def install(self, revision: str | None = None, retry_and_raise: bool = True) -> bool:
        """Install the valkey snap from the snap store.

        Args:
            revision (str | None): the snap revision to install
            retry_and_raise (bool): whether to retry in case of errors. Will raise if the error
                persists.

        Returns:
            True if successfully installed, False if errors occur and `retry_and_raise` is False.
        """
        if not revision:
            revision = str(SNAP_REVISION)

        try:
            # as long as 26.04 is not stable, we need to install the core26 snap from edge
            snap.add("core26", channel="edge")

            self.valkey.ensure(snap.SnapState.Present, revision=revision)
            self.valkey.hold()
            return True
        except snap.SnapError as e:
            logger.error(str(e))
            if retry_and_raise:
                raise RuntimeError
            return False

    @override
    def start(self) -> None:
        try:
            self.valkey.start(services=[SNAP_SERVICE])
        except snap.SnapError as e:
            logger.exception(str(e))

    @override
    def exec(self, command: List[str]) -> tuple[str, str | None]:
        try:
            output = subprocess.run(
                command,
                check=True,
                text=True,
                capture_output=True,
                timeout=10,
            )
            logger.debug("Executed command: %s, got output: %s", " ".join(command), output.stdout)
            return output.stdout, output.stderr
        except subprocess.CalledProcessError as e:
            logger.error("Command failed with %s, %s", e.returncode, e.stderr)
            raise ValkeyWorkloadCommandError(e)
        except subprocess.TimeoutExpired as e:
            logger.error("Command '%s' timed out: %s", command, str(e.stderr))
            raise ValkeyWorkloadCommandError(e)

    @override
    def alive(self) -> bool:
        """Check if the Valkey service is running."""
        try:
            return bool(self.valkey.services[SNAP_SERVICE]["active"])
        except KeyError:
            return False
