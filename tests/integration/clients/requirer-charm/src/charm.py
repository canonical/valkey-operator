#!/usr/bin/env python3
# Copyright 2026 rene.radoi@canonical.com
# See LICENSE file for licensing details.

"""Charm the application."""

import asyncio
import base64
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
from continuous_writes import DaemonConfig, TlsConfig
from continuous_writes import clear as cw_clear
from cw_helpers import CWPath, cw_llen, wait_for_pid_exit
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
        framework.observe(self.on.execute_action, self._on_execute_action)
        framework.observe(self.on.get_credentials_action, self._on_get_credentials_action)
        framework.observe(
            self.on.start_continuous_writes_action, self._on_start_continuous_writes_action
        )
        framework.observe(
            self.on.stop_continuous_writes_action, self._on_stop_continuous_writes_action
        )
        framework.observe(
            self.on.clear_continuous_writes_action, self._on_clear_continuous_writes_action
        )
        framework.observe(
            self.on.get_continuous_writes_state_action,
            self._on_get_continuous_writes_state_action,
        )
        framework.observe(
            self.on.assert_continuous_writes_increasing_action,
            self._on_assert_continuous_writes_increasing_action,
        )
        framework.observe(self.valkey_interface.on.endpoints_changed, self._on_endpoints_changed)
        framework.observe(self.on.config_changed, self._on_config_changed)

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
    def credentials(self) -> dict[str, str | None]:
        """Retrieve the client credentials from config or relation."""
        if self._use_config:
            username = str(self.config["username"]) or ""
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
            raw = str(self.config["cacert"])
            return base64.b64decode(raw).decode() if raw else None

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
            raw = str(self.config["cert"])
            return base64.b64decode(raw).decode() if raw else None

        certificates, _ = self.certificates.get_assigned_certificates()
        if not certificates:
            return None

        return certificates[0].certificate.raw

    @property
    def private_key(self) -> str | None:
        """Retrieve the client private key from config or the certificates relation."""
        if self._use_config:
            raw = str(self.config["key"])
            return base64.b64decode(raw).decode() if raw else None

        _, private_key = self.certificates.get_assigned_certificates()
        if not private_key:
            return None

        return private_key.raw

    def get_valkey_client(self, user: str) -> ValkeyClient:
        """Get a valkey client."""
        if not self.primary_endpoint:
            raise ValueError("No endpoint available.")
        if not self.credentials:
            raise ValueError("No credentials available.")
        if self.tls_enabled and (
            not self.certificate or not self.private_key or not self.tls_ca_cert
        ):
            raise ValueError("TLS is enabled but certificates are not yet available.")
        return ValkeyClient(
            username=user,
            password=self.credentials.get(user),
            endpoints=self.primary_endpoint.split(","),
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

    def _on_execute_action(self, event: ops.ActionEvent) -> None:
        """Handle execute action."""
        if not self._use_config and not self.valkey_relation:
            event.fail(
                "The action can be run only after a relation is created or connection-source is set to 'config'."
            )
            event.set_results({"ok": False})
            return

        command = str(event.params.get("command", ""))
        if not command:
            event.fail("Parameter command is required.")
            event.set_results({"ok": False})
            return

        user, _ = next(iter(self.credentials.items()))
        args = command.split()
        client = self.get_valkey_client(user)
        try:
            result = asyncio.run(client.execute_command(args))
            event.set_results({"ok": True, "result": result})
        except Exception as e:
            event.fail(f"Failed to execute command: {e}")
            logger.error("Failed to execute command: %s", e)

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
        clear_existing = bool(event.params.get("clear-existing", True))

        # Fail if a daemon is already running
        if CWPath.PID.value.exists():
            try:
                pid = int(CWPath.PID.value.read_text().strip())
                os.kill(pid, 0)  # check existence without signalling
                event.fail(f"Continuous-writes daemon is already running with PID {pid}.")
                return
            except ProcessLookupError:
                # Stale PID file — clean up and proceed
                CWPath.PID.value.unlink(missing_ok=True)
            except ValueError:
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
            clear_existing=clear_existing,
        ).to_file(CWPath.CONFIG.value)

        daemon_script = Path(__file__).parent / "continuous_writes.py"
        log_file = CWPath.LOG.value.open("w")
        proc = subprocess.Popen(
            [sys.executable, str(daemon_script), str(CWPath.CONFIG.value), str(sleep_interval)],
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
        log_file.close()
        logger.info(
            "Started continuous-writes daemon with PID %d (log: %s)",
            proc.pid,
            CWPath.LOG.value,
        )
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
        if not wait_for_pid_exit(pid):
            logger.warning(
                "Daemon PID %d had to be force-killed; state file may be incomplete.", pid
            )

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

        if bool(event.params.get("clear", False)):
            try:
                daemon_config = DaemonConfig.from_file(CWPath.CONFIG.value)
                asyncio.run(cw_clear(daemon_config))
            except Exception as exc:
                logger.warning("Failed to clear continuous-writes data: %s", exc)

        event.set_results(
            {
                "ok": True,
                "last-written-value": state["last_written"],
                "count": state["count"],
            }
        )

    def _on_clear_continuous_writes_action(self, event: ops.ActionEvent) -> None:
        """Handle clear-continuous-writes action."""
        if not CWPath.CONFIG.value.exists():
            event.fail("No continuous-writes config found — run start-continuous-writes first.")
            return

        try:
            daemon_config = DaemonConfig.from_file(CWPath.CONFIG.value)
            asyncio.run(cw_clear(daemon_config))
        except Exception as exc:
            event.fail(f"Failed to clear continuous-writes data: {exc}")
            return

        event.set_results({"ok": True})

    def _on_get_continuous_writes_state_action(self, event: ops.ActionEvent) -> None:
        """Handle get-continuous-writes-state action."""
        if not CWPath.STATE.value.exists():
            event.fail("State file not found — the daemon may not have written anything yet.")
            return

        try:
            state = json.loads(CWPath.STATE.value.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            event.fail(f"Failed to read state file: {exc}")
            return

        event.set_results(
            {
                "ok": True,
                "last-written-value": state["last_written"],
                "count": state["count"],
            }
        )

    def _on_assert_continuous_writes_increasing_action(self, event: ops.ActionEvent) -> None:
        """Handle assert-continuous-writes-increasing action."""
        if not CWPath.CONFIG.value.exists():
            event.fail("No continuous-writes config found — run start-continuous-writes first.")
            return

        try:
            config = DaemonConfig.from_file(CWPath.CONFIG.value)
        except Exception as exc:
            event.fail(f"Failed to load continuous-writes config: {exc}")
            return

        try:
            count_before = asyncio.run(cw_llen(config))
        except Exception as exc:
            event.fail(f"Failed to read list length from Valkey: {exc}")
            return

        wait = float(event.params.get("wait", 10.0))
        time.sleep(wait)

        try:
            count_after = asyncio.run(cw_llen(config))
        except Exception as exc:
            event.fail(f"Failed to read list length from Valkey after wait: {exc}")
            return

        if count_after <= count_before:
            event.fail(
                f"Writes are not increasing: list length was {count_before} before and"
                f" {count_after} after {wait}s."
            )
            return

        event.set_results(
            {
                "ok": True,
                "count-before": count_before,
                "count-after": count_after,
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

    def _on_config_changed(self, event: ops.ConfigChangedEvent) -> None:
        """Hot-reload the continuous-writes daemon when endpoints config changes."""
        if not self._use_config or not CWPath.PID.value.exists():
            return

        try:
            current_config = DaemonConfig.from_file(CWPath.CONFIG.value)
        except Exception:
            return

        if current_config.endpoints == self.primary_endpoint:
            return

        logger.info(
            "Endpoints changed from %s to %s; reloading continuous-writes daemon.",
            current_config.endpoints,
            self.primary_endpoint,
        )

        username, password = next(iter(self.credentials.items()))
        tls_config = current_config.tls
        if self.tls_enabled and self.certificate and self.private_key and self.tls_ca_cert:
            CWPath.CERT.value.write_bytes(self.certificate.encode())
            CWPath.KEY.value.write_bytes(self.private_key.encode())
            CWPath.CA.value.write_bytes(self.tls_ca_cert.encode())
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

        try:
            pid = int(CWPath.PID.value.read_text().strip())
            os.kill(pid, signal.SIGUSR1)
            logger.info("Sent SIGUSR1 to continuous-writes daemon PID %d.", pid)
        except (ProcessLookupError, ValueError, OSError) as exc:
            logger.warning("Failed to send SIGUSR1 to daemon: %s", exc)


if __name__ == "__main__":  # pragma: nocover
    ops.main(RequirerCharm)
