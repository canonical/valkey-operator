#!/usr/bin/env python3
# Copyright 2026 rene.radoi@canonical.com
# See LICENSE file for licensing details.

"""Charm the application."""

import asyncio
import enum
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import ops
from charmlibs.interfaces.tls_certificates import (
    CertificateRequestAttributes,
    TLSCertificatesRequiresV4,
)
from charms.data_platform_libs.v0.data_interfaces import DatabaseCreatedEvent, DatabaseRequires
from client import ValkeyClient
from continuous_writes import DaemonConfig
from dpcharmlibs.interfaces import (
    DataContractV1,
    RequirerCommonModel,
    ResourceCreatedEvent,
    ResourceEndpointsChangedEvent,
    ResourceProviderModel,
    ResourceRequirerEventHandler,
    ValkeyResponseModel,
    build_model,
)

logger = logging.getLogger(__name__)

SERVICE_NAME = "some-service"  # Name of Pebble service that runs in the workload container.


def _wait_for_pid_exit(
    pid: int, poll_interval: int = 1, max_attempts: int = 10, force_kill: bool = True
) -> bool:
    """Wait for a process to exit.

    Returns True if the process exited cleanly within max_attempts, False otherwise.
    If force_kill is True and the process is still running after max_attempts, sends SIGKILL.
    """
    for attempt in range(max_attempts):
        time.sleep(poll_interval)
        try:
            os.kill(pid, 0)  # signal 0 checks existence without sending a signal
        except ProcessLookupError:
            logger.info("Daemon PID %d exited after %d second(s).", pid, attempt * poll_interval)
            return True
        except OSError:
            pass  # EPERM — process exists but unowned; treat as still running

    logger.warning(
        "Daemon PID %d did not exit after %d second(s).",
        pid,
        max_attempts * poll_interval,
    )
    if force_kill:
        logger.warning("Sending SIGKILL to daemon PID %d.", pid)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return False


class CWPath(enum.Enum):
    """Paths used by the continuous-writes daemon."""

    CONFIG = Path("/tmp/cw_config.json")
    STATE = Path("/tmp/cw_state.json")
    PID = Path("/tmp/cw_daemon.pid")
    CERT = Path("/tmp/cw_client.pem")
    KEY = Path("/tmp/cw_client.key")
    CA = Path("/tmp/cw_client_ca.pem")


class RequirerCharm(ops.CharmBase):
    """Charm that acts as client for Valkey."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)

        if self.config.get("data-interfaces-version") == 0:
            self.data_interfaces_version = 0
        else:
            self.data_interfaces_version = 1

        self.certificates = TLSCertificatesRequiresV4(
            self,
            "certificates",
            certificate_requests=[
                CertificateRequestAttributes(
                    common_name="requirer-charm",
                    sans_ip=frozenset({socket.gethostbyname(socket.gethostname())}),
                    sans_dns=frozenset({self.unit.name, socket.gethostname()}),
                )
            ],
        )

        if self.data_interfaces_version == 1:
            self.valkey_interface = ResourceRequirerEventHandler(
                charm=self,
                relation_name="valkey-client",
                requests=[
                    RequirerCommonModel(resource="requirer-charm:*"),
                    RequirerCommonModel(resource="*"),
                ],
                response_model=ValkeyResponseModel,
            )
            self.framework.observe(
                self.valkey_interface.on.resource_created, self._on_resource_created
            )
        else:
            self.valkey_interface = DatabaseRequires(
                charm=self,
                relation_name="valkey-client",
                database_name="requirer-charm:*",
            )
            self.framework.observe(
                self.valkey_interface.on.database_created, self._on_database_created
            )

        # Event observers
        framework.observe(self.on.start, self._on_start)
        framework.observe(self.on.set_action, self._on_set_action)
        framework.observe(self.on.get_action, self._on_get_action)
        framework.observe(self.on.get_credentials_action, self._on_get_credentials_action)
        framework.observe(
            self.on.start_continuous_writes_action, self._on_start_continuous_writes_action
        )
        framework.observe(
            self.on.stop_continuous_writes_action, self._on_stop_continuous_writes_action
        )
        framework.observe(self.valkey_interface.on.endpoints_changed, self._on_endpoints_changed)

    @property
    def valkey_relation(self) -> ops.Relation | None:
        if not (relations := self.valkey_interface.relations):
            return None

        return relations[0]

    @property
    def remote_responses(self) -> list[ResourceProviderModel] | None:
        """Return the remote response model."""
        if not self.valkey_relation:
            return None

        return build_model(
            self.valkey_interface.interface.repository(
                self.valkey_relation.id, self.valkey_relation.app
            ),
            DataContractV1[ResourceProviderModel],
        ).requests

    @property
    def _use_config(self) -> bool:
        """Return True when connection-source is set to "config"."""
        return self.config.get("connection-source") == "config"

    @property
    def credentials(self) -> dict[str | None, str | None]:
        """Retrieve the client credentials from config or relation."""
        if self._use_config:
            username = str(self.config["username"]) or None
            password = str(self.config["password"]) or None
            return {username: password}

        if self.data_interfaces_version == 0:
            if not self.valkey_relation:
                return {"": None}

            return {
                self.valkey_interface.fetch_relation_field(
                    self.valkey_relation.id, "username"
                ): self.valkey_interface.fetch_relation_field(self.valkey_relation.id, "password")
            }

        remote_responses = self.remote_responses
        if not remote_responses:
            return {"": None}

        credentials = {}
        for response in remote_responses:
            credentials.update({response.username: response.password})

        return credentials

    @property
    def primary_endpoint(self) -> str | None:
        """Retrieve the write-endpoints from config or relation."""
        if self._use_config:
            return str(self.config["endpoints"]) or None

        if self.data_interfaces_version == 0:
            if not self.valkey_relation:
                return None

            return self.valkey_interface.fetch_relation_field(self.valkey_relation.id, "endpoints")

        remote_responses = self.remote_responses
        if not remote_responses:
            return None

        return remote_responses[0].endpoints

    @property
    def tls_enabled(self) -> bool:
        """Retrieve the TLS flag from config or relation."""
        if self._use_config:
            return bool(self.config.get("tls-enabled"))

        if self.data_interfaces_version == 0:
            if not self.valkey_relation:
                return False

            return (
                self.valkey_interface.fetch_relation_field(self.valkey_relation.id, "tls")
                == "true"
            )

        remote_responses = self.remote_responses
        if not remote_responses:
            return False

        return remote_responses[0].tls

    @property
    def tls_ca_cert(self) -> str | None:
        """Retrieve the TLS CA cert from config or relation."""
        if self._use_config:
            return str(self.config["ca-cert"]) or None

        if self.data_interfaces_version == 0:
            if not self.valkey_relation:
                return None

            return self.valkey_interface.fetch_relation_field(self.valkey_relation.id, "tls-ca")

        remote_responses = self.remote_responses
        if not remote_responses:
            return None

        return remote_responses[0].tls_ca

    @property
    def certificate(self) -> str | None:
        """Retrieve the client certificate from config or the certificates relation."""
        if self._use_config:
            return str(self.config["cert"]) or None

        certificates, _ = self.certificates.get_assigned_certificates()
        if not certificates:
            return None

        return certificates[0].certificate.raw

    @property
    def private_key(self) -> str | None:
        """Retrieve the client private key from config or the certificates relation."""
        if self._use_config:
            return str(self.config["key"]) or None

        _, private_key = self.certificates.get_assigned_certificates()
        if not private_key:
            return None

        return private_key.raw

    def get_valkey_client(self, user: str) -> ValkeyClient:
        """Get a valkey client."""
        return ValkeyClient(
            username=user,
            password=self.credentials.get(user),
            host=self.primary_endpoint.split(":")[0],
            port=int(self.primary_endpoint.split(":")[1]),
            tls_cert=self.certificate.encode() if self.tls_enabled else None,
            tls_key=self.private_key.encode() if self.tls_enabled else None,
            tls_ca_cert=self.tls_ca_cert.encode() if self.tls_enabled else None,
        )

    def _on_start(self, event: ops.StartEvent) -> None:
        """Handle start event."""
        self.unit.status = ops.ActiveStatus()

    def _on_set_action(self, event: ops.ActionEvent) -> None:
        """Handle set action."""
        if not self.valkey_relation:
            event.fail("The action can be run only after relation is created.")
            event.set_results({"ok": False})
            return

        key = str(event.params.get("key", ""))
        value = str(event.params.get("value", ""))
        user = str(event.params.get("user", ""))
        if not key or not value or not user:
            event.fail("Parameters key, value and user are required.")
            event.set_results({"ok": False})
            return

        client = self.get_valkey_client(user)
        try:
            asyncio.run(client.set_key(key, value))
            event.set_results({"ok": True})
        except Exception as e:
            event.fail(f"Failed to write data: {e}")
            logger.error("Failed to write data: %s", e)

    def _on_get_action(self, event: ops.ActionEvent) -> None:
        """Handle get action."""
        if not self.valkey_relation:
            event.fail("The action can be run only after relation is created.")
            event.set_results({"ok": False})
            return

        key = str(event.params.get("key", ""))
        user = str(event.params.get("user", ""))
        if not key or not user:
            event.fail("Parameters key and user are required.")
            event.set_results({"ok": False})
            return

        client = self.get_valkey_client(user)
        try:
            value = asyncio.run(client.get_key(key))
            event.set_results(
                {
                    "ok": True,
                    "result": value,
                }
            )
        except Exception as e:
            event.fail(f"Failed to read data: {e}")
            logger.error("Failed to read data: %s", e)

    def _on_get_credentials_action(self, event: ops.ActionEvent) -> None:
        """Return the credentials an action response."""
        if not self.valkey_relation:
            event.fail("The action can be run only after relation is created.")
            event.set_results({"ok": False})
            return

        credentials = self.credentials
        usernames = ",".join(list(credentials.keys()))
        event.set_results(
            {
                "ok": True,
                "usernames": usernames,
            }
        )

    def _on_start_continuous_writes_action(self, event: ops.ActionEvent) -> None:
        """Handle start-continuous-writes action."""
        if not self._use_config and not self.valkey_relation:
            event.fail(
                "The action can be run only after a relation is created or connection-source is set to 'config'."
            )
            return

        if not self.primary_endpoint:
            event.fail("No primary endpoint available.")
            return

        if not self.credentials:
            event.fail("No credentials available.")
            return

        if self.tls_enabled:
            if not self.certificate or not self.private_key or not self.tls_ca_cert:
                event.fail("TLS is enabled but certificates are not yet available.")
                return

        sleep_interval = float(event.params.get("sleep-interval", 1.0))

        # Stop any running daemon first
        if CWPath.PID.value.exists():
            try:
                pid = int(CWPath.PID.value.read_text().strip())
                os.kill(pid, signal.SIGTERM)
                time.sleep(1)
            except (ProcessLookupError, ValueError, OSError):
                pass
            CWPath.PID.value.unlink(missing_ok=True)

        # Clear previous state so the new run starts fresh
        CWPath.STATE.value.unlink(missing_ok=True)

        # Resolve the first available credential from the relation
        username, password = next(iter(self.credentials.items()))

        tls_config = None
        if self.tls_enabled:
            CWPath.CERT.value.write_bytes(self.certificate.encode())
            CWPath.KEY.value.write_bytes(self.private_key.encode())
            CWPath.CA.value.write_bytes(self.tls_ca_cert.encode())
            from continuous_writes import TlsConfig

            tls_config = TlsConfig(
                cert_path=str(CWPath.CERT.value),
                key_path=str(CWPath.KEY.value),
                ca_path=str(CWPath.CA.value),
            )

        DaemonConfig(
            endpoints=self.primary_endpoint,
            username=username,
            password=password,
            tls=tls_config,
            initial_count=0,
        ).to_file(CWPath.CONFIG.value)

        daemon_script = Path(__file__).parent / "continuous_writes.py"
        proc = subprocess.Popen(
            [sys.executable, str(daemon_script), str(CWPath.CONFIG.value), str(sleep_interval)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        logger.info("Started continuous-writes daemon with PID %d", proc.pid)
        event.set_results({"ok": True, "pid": proc.pid})

    def _on_stop_continuous_writes_action(self, event: ops.ActionEvent) -> None:
        """Handle stop-continuous-writes action."""
        if not CWPath.PID.value.exists():
            event.fail("No continuous-writes daemon is running (PID file not found).")
            return

        try:
            pid = int(CWPath.PID.value.read_text().strip())
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            logger.warning("Daemon PID %s was not running; reading last state.", pid)
        except ValueError:
            event.fail("PID file contained invalid data.")
            return
        except OSError as exc:
            event.fail(f"Failed to signal daemon: {exc}")
            return

        # Wait for the daemon to exit and flush its final state, with retries
        if not _wait_for_pid_exit(pid):
            logger.warning("Daemon PID %d had to be force-killed; state file may be incomplete.", pid)

        if not CWPath.STATE.value.exists():
            event.fail("State file not found — the daemon may not have written anything.")
            return

        try:
            state = json.loads(CWPath.STATE.value.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            event.fail(f"Failed to read state file: {exc}")
            return

        logger.info(
            "Stopped continuous-writes daemon. last_written=%d, count=%d",
            state["last_written"],
            state["count"],
        )
        event.set_results(
            {
                "ok": True,
                "last-written-value": state["last_written"],
                "count": state["count"],
            }
        )

    def _on_resource_created(self, event: ResourceCreatedEvent[ResourceProviderModel]) -> None:
        """Handle resource created event."""
        logger.info("Resource created")

    def _on_endpoints_changed(
        self, event: ResourceEndpointsChangedEvent[ResourceProviderModel]
    ) -> None:
        """Handle endpoints changed event."""
        logger.info("Valkey endpoints have been changed")

    def _on_database_created(self, event: DatabaseCreatedEvent) -> None:
        """Handle the event triggered by data-interfaces v0."""
        logger.info("Database created")


if __name__ == "__main__":  # pragma: nocover
    ops.main(RequirerCharm)
