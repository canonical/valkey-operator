#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Implementation of WorkloadBase for running Valkey on VMs."""

import logging
import subprocess
from typing import List, override

from charmlibs import pathops, snap
from tenacity import Retrying, retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from core.base_workload import WorkloadBase
from literals import SNAP_CONFIG_FILE, SNAP_CURRENT_PATH, SNAP_NAME, SNAP_REVISION, SNAP_SERVICE

logger = logging.getLogger(__name__)


class ValkeyVmWorkload(WorkloadBase):
    """Implementation of WorkloadBase for running a Valkey container on VM."""

    def __init__(self) -> None:
        for attempt in Retrying(stop=stop_after_attempt(5), wait=wait_fixed(5)):
            with attempt:
                self.valkey = snap.SnapCache()[SNAP_NAME]

        self.config_file = pathops.LocalPath(f"{SNAP_CURRENT_PATH}/{SNAP_CONFIG_FILE}")

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
            revision = SNAP_REVISION

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
    def write_config_file(self, config: dict[str, str]) -> None:
        config_string = "\n".join(f"{str(key)}{' '}{str(value)}" for key, value in config.items())

        path = self.config_file
        path.write_text(config_string)

    @override
    def write_file(self, content: str, path: str) -> None:
        """Write content to a file on disk.

        Args:
            content (str): The content to be written.
            path (str): The file path where the content should be written.
        """
        file_path = pathops.LocalPath(path)
        file_path.write_text(content)

    @override
    def exec(self, command: List[str]) -> str:
        try:
            output = subprocess.run(
                command,
                check=True,
                text=True,
                capture_output=True,
                timeout=10,
            ).stdout.strip()
            logger.debug(output)
            return output
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            logger.error(e)
            raise
