#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Implementation of WorkloadBase for running Valkey on VMs."""

import logging
import subprocess
import time
from typing import List, override

from charmlibs import pathops, snap
from tenacity import (
    Retrying,
    retry,
    retry_if_exception_type,
    retry_if_result,
    stop_after_attempt,
    wait_fixed,
)

from common.exceptions import (
    ValkeyServiceNotAliveError,
    ValkeyServicesFailedToStartError,
    ValkeyWorkloadCommandError,
)
from core.base_workload import TLSPaths, WorkloadBase
from literals import (
    SNAP_ACL_FILE,
    SNAP_COMMON_PATH,
    SNAP_CONFIG_FILE,
    SNAP_CURRENT_PATH,
    SNAP_NAME,
    SNAP_REVISION,
    SNAP_SENTINEL_ACL_FILE,
    SNAP_SENTINEL_CONFIG_FILE,
    SNAP_SENTINEL_SERVICE,
    SNAP_SERVICE,
)

logger = logging.getLogger(__name__)


class ValkeyVmWorkload(WorkloadBase):
    """Implementation of WorkloadBase for running a Valkey container on VM."""

    def __init__(self) -> None:
        for attempt in Retrying(stop=stop_after_attempt(5), wait=wait_fixed(5)):
            with attempt:
                self.valkey = snap.SnapCache()[SNAP_NAME]

        self.root_dir = pathops.LocalPath("/")
        self.config_file = self.root_dir / SNAP_CURRENT_PATH / SNAP_CONFIG_FILE
        self.sentinel_config_file = self.root_dir / SNAP_CURRENT_PATH / SNAP_SENTINEL_CONFIG_FILE
        self.acl_file = self.root_dir / SNAP_CURRENT_PATH / SNAP_ACL_FILE
        self.sentinel_acl_file = self.root_dir / SNAP_CURRENT_PATH / SNAP_SENTINEL_ACL_FILE
        self.working_dir = self.root_dir / SNAP_COMMON_PATH / "var/lib/charmed-valkey"
        self.tls_dir = self.root_dir / SNAP_CURRENT_PATH / "tls"
        self.tls_paths: TLSPaths = TLSPaths(tls_root=self.tls_dir)
        self.valkey_service = SNAP_SERVICE
        self.sentinel_service = SNAP_SENTINEL_SERVICE
        self.cli = "charmed-valkey.cli"
        self.user = "snap_daemon"

    @property
    @override
    def can_connect(self) -> bool:
        try:
            return bool(self.valkey.services[self.valkey_service])
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
            self.valkey.start(services=[self.valkey_service, self.sentinel_service])
        except snap.SnapError as e:
            logger.exception(str(e))
            raise ValkeyServicesFailedToStartError(f"Failed to start Valkey services: {e}") from e

        # The service might start but fail to load and die immediately
        # On k8s starting the services will wait (poll) for them to be started.
        # We do the same here to make sure the services are alive after start.
        if not self.wait_for_services_to_be_alive(duration=3):
            logger.error("Valkey service is not alive after start.")
            raise ValkeyServiceNotAliveError("Valkey service is not alive after start.")

    @override
    def restart(self, service: str) -> None:
        try:
            self.valkey.restart(services=[service])
        except snap.SnapError as e:
            logger.exception(str(e))
            raise ValkeyServicesFailedToStartError(
                "Failed to restart service %s: %s", service, e
            ) from e

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
            return output.stdout, output.stderr
        except subprocess.CalledProcessError as e:
            logger.error("Command failed with %s, %s", e.returncode, e.stderr)
            raise ValkeyWorkloadCommandError(e)
        except subprocess.TimeoutExpired as e:
            logger.error("Command timed out: %s", str(e.stderr))
            raise ValkeyWorkloadCommandError(e)

    @override
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(1),
        retry=retry_if_result(lambda healthy: not healthy),
        retry_error_callback=lambda _: False,
    )
    def alive(self) -> bool:
        try:
            return bool(self.valkey.services[self.valkey_service]["active"]) and bool(
                self.valkey.services[self.sentinel_service]["active"]
            )
        except KeyError:
            return False

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(1),
        retry=retry_if_result(lambda healthy: not healthy),
        retry_error_callback=lambda _: False,
    )
    def wait_for_services_to_be_alive(self, duration: float = 30, delay: float = 0.1) -> bool:
        """Poll until the Valkey services are alive for at least `duration` seconds.

        Args:
            duration (float): The maximum duration to poll for the services to be alive. Default is 30 seconds.
            delay (float): The delay between each poll attempt in seconds. Default is 0.1 seconds.

        Returns:
                bool: True if the services are alive within the poll duration, False otherwise.
        """
        deadline = time.time() + duration
        while time.time() < deadline:
            if not self.alive():
                return False

            time.sleep(delay)
        return True
