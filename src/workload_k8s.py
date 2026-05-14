#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Implementation of WorkloadBase for running Valkey on K8s."""

import collections
import logging
import signal
import threading
from typing import override

import ops
from charmlibs import pathops
from tenacity import retry, retry_if_result, stop_after_attempt, wait_fixed

from common.exceptions import (
    ValkeyServiceNotAliveError,
    ValkeyServicesCouldNotBeStoppedError,
    ValkeyServicesFailedToStartError,
    ValkeyWorkloadCommandError,
)
from core.base_workload import ProcessHandle, TLSPaths, WorkloadBase
from literals import (
    ACL_FILE,
    CHARM,
    CONFIG_FILE,
    SENTINEL_ACL_FILE,
    SENTINEL_CONFIG_FILE,
)

logger = logging.getLogger(__name__)


class _K8sProcessHandle:
    """ProcessHandle backed by Pebble exec (K8s substrate).

    stdout is streamed to the caller unbuffered. stderr is drained on a
    daemon thread into a bounded buffer (last 64 chunks) so a chatty child
    cannot fill the pipe and block -- ``wait()`` therefore returns only the
    tail of stderr. ``wait()`` returns the exit code WITHOUT calling
    ``wait_output()``: the latter would buffer the entire stdout (the whole
    RDB) into the charm container's memory. When Pebble itself fails to run
    the change, ``wait()`` returns -1 as the returncode sentinel.
    ``kill()`` sends SIGKILL but does not block waiting for the exit.
    """

    def __init__(self, process: ops.pebble.ExecProcess):
        self._process = process
        self.stdout = process.stdout
        self._stderr_buf: collections.deque[bytes] = collections.deque(maxlen=64)
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        if self._process.stderr is None:
            return
        try:
            for chunk in iter(lambda: self._process.stderr.read(4096), b""):
                self._stderr_buf.append(chunk)
        except Exception:
            logger.exception("stderr drain thread failed")

    def wait(self) -> tuple[int, str]:
        try:
            self._process.wait()
            rc = 0
        except ops.pebble.ExecError as e:
            rc = e.exit_code
        except ops.pebble.ChangeError as e:
            logger.error("Pebble exec did not complete: %s", e)
            rc = -1
        self._stderr_thread.join(timeout=5)
        return rc, b"".join(self._stderr_buf).decode("utf-8", "replace")

    def kill(self) -> None:
        try:
            self._process.send_signal(signal.SIGKILL)
        except ops.pebble.ConnectionError as e:
            # Pebble itself is unreachable -- the exec may still be running
            # and we have lost the ability to stop it. A real problem.
            logger.error("Cannot reach Pebble to SIGKILL the exec: %s", e)
        except ops.pebble.Error as e:
            # Most often the exec has already finished, so there is nothing
            # to signal. kill() is called on cleanup paths after the process
            # may have exited on its own, so this is expected -- DEBUG, not
            # WARNING, to avoid alarming noise on the happy path.
            logger.debug("SIGKILL to Pebble exec was a no-op (likely already exited): %s", e)


class ValkeyK8sWorkload(WorkloadBase):
    """Implementation of WorkloadBase for running a Valkey container on K8s."""

    def __init__(self, container: ops.Container | None) -> None:
        if not container:
            raise AttributeError("Container is required.")

        self.container = container
        self.root_dir = pathops.ContainerPath("/", container=self.container)
        self.config_file = self.root_dir / CONFIG_FILE
        self.sentinel_config_file = self.root_dir / SENTINEL_CONFIG_FILE
        self.acl_file = self.root_dir / ACL_FILE
        self.sentinel_acl_file = self.root_dir / SENTINEL_ACL_FILE
        # todo: update this path once directories in the rock are complying with the standard
        self.working_dir = self.root_dir / "var/lib/valkey"
        self.tls_dir = self.root_dir / "var/lib/valkey/tls"
        self.tls_paths: TLSPaths = TLSPaths(tls_root=self.tls_dir)
        self.valkey_service = "valkey"
        self.sentinel_service = "valkey-sentinel"
        self.metric_service = "metric_exporter"
        self.cli = "valkey-cli"
        self.user = "_daemon_"

    @property
    @override
    def can_connect(self) -> bool:
        return self.container.can_connect()

    @property
    def pebble_layer(self) -> ops.pebble.Layer:
        """Create the Pebble configuration layer for Valkey."""
        layer_config: ops.pebble.LayerDict = {
            "summary": "Valkey layer",
            "description": "Valkey layer",
            "services": {
                self.valkey_service: {
                    "override": "replace",
                    "summary": "Valkey service",
                    "command": f"valkey-server {self.config_file.as_posix()}",
                    "user": self.user,
                    "group": self.user,
                    "startup": "enabled",
                },
                self.sentinel_service: {
                    "override": "replace",
                    "summary": "Valkey sentinel service",
                    "command": f"valkey-sentinel {self.sentinel_config_file.as_posix()}",
                    "user": self.user,
                    "group": self.user,
                    "startup": "enabled",
                },
                self.metric_service: {
                    "override": "replace",
                    "summary": "Valkey metric exporter",
                    "command": "bin/redis_exporter",
                    "user": self.user,
                    "group": self.user,
                    "startup": "enabled",
                },
            },
        }
        return ops.pebble.Layer(layer_config)

    @override
    def start(self) -> None:
        try:
            self.container.add_layer(CHARM, self.pebble_layer, combine=True)
            self.container.restart(self.valkey_service, self.sentinel_service, self.metric_service)
        except ops.pebble.ChangeError as e:
            raise ValkeyServicesFailedToStartError(f"Failed to start Valkey services: {e}") from e
        if not self.alive():
            raise ValkeyServiceNotAliveError("Valkey service is not alive after start.")

    @override
    def restart(self, service: str) -> None:
        try:
            self.container.restart(service)
        except ops.pebble.ChangeError as e:
            raise ValkeyServicesFailedToStartError(
                "Failed to start service %s: %s", service, e
            ) from e

    @override
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(1),
        retry=retry_if_result(lambda healthy: not healthy),
        retry_error_callback=lambda _: False,
    )
    def alive(self) -> bool:
        for service_name in [
            self.valkey_service,
            self.sentinel_service,
            self.metric_service,
        ]:
            service = self.container.get_service(service_name)
            if not service.is_running():
                return False
        return True

    @override
    def exec(
        self, command: list[str], env: dict[str, str] | None = None
    ) -> tuple[str, str | None]:
        try:
            process = self.container.exec(
                command=command,
                environment=env,
            )
            return process.wait_output()
        except ops.pebble.APIError as e:
            logger.error("Command failed with %s, %s", e.code, e.body)
            raise ValkeyWorkloadCommandError(e)
        except ops.pebble.ExecError as e:
            logger.error("Command failed with: %s, %s", e.exit_code, e.stdout)
            raise ValkeyWorkloadCommandError(e)

    @override
    def exec_stream(
        self, command: list[str], env: dict[str, str] | None = None
    ) -> ProcessHandle:
        return _K8sProcessHandle(
            self.container.exec(
                command=command, encoding=None, timeout=None, environment=env
            )
        )

    @override
    def stop(self) -> None:
        try:
            self.container.stop(self.valkey_service, self.sentinel_service, self.metric_service)
        except (
            ops.pebble.ChangeError,
            ops.pebble.TimeoutError,
            ops.pebble.ConnectionError,
            ops.pebble.APIError,
        ) as e:
            logger.error("Failed to stop Valkey services: %s", e)
            raise ValkeyServicesCouldNotBeStoppedError(
                f"Failed to stop Valkey services: {e}"
            ) from e
