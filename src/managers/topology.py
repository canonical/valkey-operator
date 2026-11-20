#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Manager for Cluster Topology."""

import logging
import os
import signal
import subprocess
from pathlib import Path
from sys import version_info

from common.exceptions import (
    ValkeyServicesCouldNotBeStoppedError,
    ValkeyServicesFailedToStartError,
)
from core.base_workload import WorkloadBase
from core.cluster_state import ClusterState
from literals import (
    LOG_STORAGE,
    SENTINEL_PORT,
    SENTINEL_TLS_PORT,
    TOPOLOGY_OBSERVER_LOGS_SERVICE,
    TOPOLOGY_OBSERVER_PID_FILENAME,
    TOPOLOGY_OBSERVER_TLS_CA_FILENAME,
    CharmUsers,
    Substrate,
)

logger = logging.getLogger(__name__)


class TopologyManager:
    """Observe the topology for Valkey Sentinel."""

    name: str = "topology_observer"
    state: ClusterState

    def __init__(self, state: ClusterState, workload: WorkloadBase):
        self.state = state
        self.workload = workload

    @property
    def _tls_ca_file_path(self) -> Path:
        """Return the path to the topology observer TLS CA file."""
        return self.state.charm.charm_dir / TOPOLOGY_OBSERVER_TLS_CA_FILENAME

    @property
    def _pid_file_path(self) -> Path:
        """Return the path to the topology observer pid file."""
        return self.state.charm.charm_dir / TOPOLOGY_OBSERVER_PID_FILENAME

    def start_observer(self) -> None:
        """Start the topology observer as a subprocess."""
        if self.state.substrate == Substrate.K8S:
            # before the early return, so a restarted valkey container gets its forwarding back
            try:
                self.workload.start(TOPOLOGY_OBSERVER_LOGS_SERVICE, check_alive=False)
            except ValkeyServicesFailedToStartError as e:
                # e.g. a refreshed pod before the full start adds this revision's layer
                logger.warning("Cannot forward the topology observer log: %s", e)

        if self._read_observer_pid() != 0:
            return

        # Generate the venv path based on the existing lib path
        env = os.environ.copy()
        env.pop("JUJU_CONTEXT_ID", None)
        for loc in env["PYTHONPATH"].split(":"):
            path = Path(loc)
            venv_path = (
                path
                / ".."
                / "venv"
                / "lib"
                / f"python{version_info.major}.{version_info.minor}"
                / "site-packages"
            )
            if path.stem == "lib":
                env["PYTHONPATH"] = f"{venv_path.resolve()}:{env['PYTHONPATH']}"
                break

        # Gather Valkey hosts for connection
        started_servers = [
            unit.get_endpoint(self.state.substrate)
            for unit in self.state.servers
            if unit.is_active
        ]
        port = SENTINEL_TLS_PORT if self.state.unit_server.is_tls_enabled else SENTINEL_PORT
        hosts = ",".join(sorted([f"{server}:{port}" for server in started_servers]))

        if self.state.unit_server.is_tls_enabled:
            # Store current TLS CA cert on operator container
            tls_ca_cert = self.workload.read_file(self.workload.tls_paths.client_ca)
            self._tls_ca_file_path.write_text(tls_ca_cert)

        logging.info("Starting topology observer")
        pid = subprocess.Popen(  # noqa: S603
            [
                "/usr/bin/python3",
                "src/scripts/topology_observer.py",
                hosts,
                CharmUsers.SENTINEL_CHARM_ADMIN.value,  # username
                self.state.cluster.internal_users_credentials.get(
                    CharmUsers.SENTINEL_CHARM_ADMIN.value, ""
                ),  # password
                str(self.state.unit_server.is_tls_enabled),
                self.state.unit_server.unit_name,
                self.state.charm.charm_dir,
                # the logs storage survives pod restarts and refreshes, unlike the charm dir.
                # On K8s this is the charm container's mount, the workload's is elsewhere.
                self.state.charm.model.storages[LOG_STORAGE][0].location,
            ],
            # The observer writes its own rotating log file.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        ).pid

        self._pid_file_path.write_text(str(pid))
        logging.info(f"Started topology observer process with PID {pid}")

    def stop_observer(self) -> None:
        """Stop the topology observer."""
        if self.state.substrate == Substrate.K8S:
            # before the early return, the forwarding outlives a restarted charm container
            try:
                self.workload.stop(TOPOLOGY_OBSERVER_LOGS_SERVICE)
            except ValkeyServicesCouldNotBeStoppedError as e:
                logger.warning("Cannot stop forwarding the topology observer log: %s", e)

        if (observer_pid := self._read_observer_pid()) == 0:
            logger.debug("Topology observer already stopped")
            return

        logger.debug("Stopping topology observer")
        try:
            os.kill(int(observer_pid), signal.SIGTERM)
            logger.info("Topology observer stopped")
        except OSError:
            pass
        finally:
            self._pid_file_path.unlink()

    def restart_observer(self) -> None:
        """Stop and start the topology observer to pickup host changes."""
        self.stop_observer()
        self.start_observer()

    def _read_observer_pid(self) -> int:
        """Return the pid of the running topology observer, or 0 if none."""
        try:
            pid = int(self._pid_file_path.read_text())
            # the pid file outlives a crashed observer and the kernel can reuse its pid
            if "topology_observer.py" in Path(f"/proc/{pid}/cmdline").read_text():
                return pid
        except (FileNotFoundError, PermissionError):
            pass
        return 0
