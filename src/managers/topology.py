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

from core.base_workload import WorkloadBase
from core.cluster_state import ClusterState
from literals import (
    CLIENT_PORT,
    TLS_PORT,
    TOPOLOGY_OBSERVER_LOGFILE,
    SNAP_TOPOLOGY_OBSERVER_LOGFILE,
    CharmUsers,
    Substrate,
)

logger = logging.getLogger(__name__)

LOG_FILE_PATH = "/var/log/topology_observer.log"


class TopologyManager:
    """Observe the topology for Valkey Sentinel."""

    name: str = "topology_observer"
    state: ClusterState

    def __init__(self, state: ClusterState, workload: WorkloadBase):
        self.state = state
        self.workload = workload

    def start_observer(self) -> None:
        """Start the topology observer as a subprocess."""
        if observer_pid := self.state.unit_server.model.topology_observer_pid:
            try:
                # check if the process already runs
                os.kill(int(observer_pid), 0)
                return
            except OSError:
                logger.debug("Topology observer not running")
                pass

        # Generate the venv path based on the existing lib path
        env = os.environ.copy()
        env.pop('JUJU_CONTEXT_ID', None)
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
        port = TLS_PORT if self.state.unit_server.is_tls_enabled else CLIENT_PORT
        valkey_hosts = ",".join(sorted([f"{server}:{port}" for server in started_servers]))

        # Store current TLS CA cert on operator container
        tls_ca_cert = self.workload.read_file(self.workload.tls_paths.client_ca)
        tls_ca_cert_file = "/etc/ssl/certs/Valkey_CA.pem"
        path = Path(tls_ca_cert_file)
        path.write_text(tls_ca_cert)

        logging.info("Starting topology observer")
        pid = subprocess.Popen(  # noqa: S603
            [
                "/usr/bin/python3",
                "scripts/cluster_topology_observer.py",
                valkey_hosts,
                CharmUsers.VALKEY_ADMIN.value, # username
                self.state.unit_server.valkey_admin_password, # password
                str(self.state.unit_server.is_tls_enabled),
                tls_ca_cert_file,
                self.state.unit_server.unit_name,
                self.state.charm.charm_dir,
            ],
            # File shouldn't close
            stdout=open(LOG_FILE_PATH, "a"),  # noqa: SIM115
            stderr=subprocess.STDOUT,
            env=env,
        ).pid

        self.state.unit_server.update({"topology_observer_pid": pid})
        logging.info(f"Started topology observer process with PID {pid}")

    def stop_observer(self) -> None:
        """Stop the topology observer."""
        if not (observer_pid := self.state.unit_server.model.topology_observer_pid):
            logger.debug("Topology observer already stopped")
            return

        logger.debug("Stopping topology observer")
        try:
            os.kill(int(observer_pid), signal.SIGTERM)
            logger.info("Topology observer stopped")
            self.state.unit_server.update({"topology_observer_pid": ""})
        except OSError:
            pass

    def restart_observer(self) -> None:
        """Stop and start the topology observer to pickup host changes."""
        self.stop_observer()
        self.start_observer()
