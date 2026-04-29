#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Manager for all config related tasks."""

import hashlib
import logging
import secrets
import string
from pathlib import Path

from data_platform_helpers.advanced_statuses.models import StatusObject
from data_platform_helpers.advanced_statuses.protocol import ManagerStatusProtocol
from data_platform_helpers.advanced_statuses.types import Scope

from common.exceptions import ValkeyConfigurationError, ValkeyWorkloadCommandError
from core.base_workload import WorkloadBase
from core.cluster_state import ClusterState
from literals import (
    CHARM_USERS_ROLE_MAP,
    CLIENT_PORT,
    PRIMARY_NAME,
    SENTINEL_PORT,
    SENTINEL_TLS_PORT,
    TLS_PORT,
    CharmUsers,
    StartState,
    Substrate,
    TLSState,
)
from statuses import CharmStatuses

logger = logging.getLogger(__name__)

WORKING_DIR = Path(__file__).absolute().parent


class ConfigManager(ManagerStatusProtocol):
    """Manage cluster members, authorization and other server related tasks."""

    name: str = "config"
    state: ClusterState

    def __init__(self, state: ClusterState, workload: WorkloadBase):
        self.state = state
        self.workload = workload

    def get_config_properties(self, primary_endpoint: str) -> dict[str, str]:
        """Assemble the config properties.

        Returns:
            Dictionary of properties to be written to the config file.
        """
        config_properties = {}
        if not self.state.unit_server.model or not self.state.cluster.model:
            return config_properties

        # load the config properties provided from the template in this repo
        # it does NOT load the file from disk in the charm unit in order to avoid config drift
        with open(f"{WORKING_DIR}/config-template/valkey.conf") as config:
            # The valkey.conf file contains a number of directives that have a very simple format:
            # keyword argument1 argument2 ... argumentN
            for line in config:
                if not line or line.startswith("#"):
                    # ignore comments and empty lines
                    continue
                try:
                    key, value = line.split(" ", 1)
                except ValueError:
                    key = line.strip()
                    value = ""
                config_properties[key.strip()] = value.strip()

        # Adjust default values
        config_properties["loglevel"] = "verbose"
        config_properties["aclfile"] = self.workload.acl_file.as_posix()
        config_properties["dir"] = self.workload.working_dir.as_posix()

        # avoid potentially inconsistent save overwriting the save triggered by the charm on shutdown
        config_properties["shutdown-on-sigint"] = "nosave"
        config_properties["shutdown-on-sigterm"] = "nosave"

        config_properties["bind"] = self.state.endpoint

        # replica related config
        replica_config = self._generate_replica_config(primary_endpoint=primary_endpoint)
        config_properties.update(replica_config)

        # TLS related configuration
        tls_config = self.generate_tls_config()
        config_properties.update(tls_config)

        return config_properties

    def _generate_replica_config(self, primary_endpoint: str) -> dict[str, str]:
        """Generate the config properties related to replica configuration based on the current cluster state."""
        local_unit_endpoint = self.state.endpoint
        replica_config = {
            "primaryuser": CharmUsers.VALKEY_REPLICA.value,
            "primaryauth": self.state.cluster.internal_users_credentials.get(
                CharmUsers.VALKEY_REPLICA.value, ""
            ),
            "replica-announce-ip": local_unit_endpoint,
        }
        if primary_endpoint != local_unit_endpoint:
            # set replicaof
            logger.debug("Setting replicaof to primary %s", primary_endpoint)
            # internal communication always uses peer TLS (`tls-replication=yes`)
            replica_config["replicaof"] = f"{primary_endpoint} {TLS_PORT}"
        return replica_config

    def set_config_properties(self, primary_endpoint: str) -> None:
        """Write the config properties to the config file."""
        logger.debug("Writing configuration")
        self.workload.write_config_file(
            config=self.get_config_properties(primary_endpoint=primary_endpoint)
        )

    def generate_tls_config(self) -> dict[str, str]:
        """Return the TLS configuration based on the current state."""
        tls_config = {
            "port": str(CLIENT_PORT),
            "tls-port": str(TLS_PORT),
            "tls-cert-file": self.workload.tls_paths.client_cert.as_posix(),
            "tls-key-file": self.workload.tls_paths.client_key.as_posix(),
            "tls-ca-cert-dir": self.workload.tls_paths.ca_certs_dir.as_posix(),
            "tls-replication": "yes",
            "tls-auth-clients": "optional",
            "tls-auth-clients-user": "CN",
        }

        if (
            self.state.unit_server.tls_client_state in [TLSState.TLS, TLSState.TO_TLS]
            and self.state.unit_server.model.client_cert_ready
        ):
            # if client TLS is enabled, we shut down the default port to discard non-TLS traffic
            tls_config["port"] = "0"

        return tls_config

    def generate_sentinel_tls_config(self) -> dict[str, str]:
        """Return the TLS configuration for sentinel based on the current state."""
        tls_config = {
            "port": str(SENTINEL_PORT),
            "tls-port": str(SENTINEL_TLS_PORT),
            "tls-cert-file": self.workload.tls_paths.client_cert.as_posix(),
            "tls-key-file": self.workload.tls_paths.client_key.as_posix(),
            "tls-ca-cert-dir": self.workload.tls_paths.ca_certs_dir.as_posix(),
            "tls-replication": "yes",
            "tls-auth-clients": "optional",
            "tls-auth-clients-user": "CN",
        }

        if (
            self.state.unit_server.tls_client_state in [TLSState.TLS, TLSState.TO_TLS]
            and self.state.unit_server.model.client_cert_ready
        ):
            # if client TLS is enabled, we shut down the default port to discard non-TLS traffic
            tls_config["port"] = "0"

        return tls_config

    def set_acl_file(self, passwords: dict[str, str] | None = None) -> None:
        """Write the ACL file with appropriate user permissions.

        Args:
            passwords (dict[str, str] | None): Optional dictionary of passwords to use. If not provided,
                the passwords from the cluster state will be used.
        """
        logger.debug("Writing ACL configuration")
        acl_content = "user default off\n"
        for user in CharmUsers:
            # only process VALKEY users
            # Sentinel users should be in the sentinel acl file
            if "VALKEY_" not in user.name:
                continue
            acl_content += self._get_internal_user_acl_line(user, passwords=passwords)
        acl_content += self._get_client_user_acl_lines()
        self.workload.write_file(
            acl_content,
            self.workload.acl_file,
            user=self.workload.user,
            group=self.workload.user,
        )

    def _get_internal_user_acl_line(
        self, user: CharmUsers, passwords: dict[str, str] | None = None
    ) -> str:
        """Generate an ACL line for a given internal user.

        Args:
            user (CharmUsers): Internal User for which to generate the ACL line.
            passwords (dict[str, str] | None): Optional dictionary of passwords to use. If not provided,
                the passwords from the cluster state will be used.

        Returns:
            str: ACL line for the internal user.
        """
        passwords = passwords or self.state.cluster.internal_users_credentials
        if not (password := passwords.get(user.value, "")):
            raise ValueError(f"No password found for user {user}")
        password_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return f"user {user.value} on #{password_hash} {CHARM_USERS_ROLE_MAP[user]}\n"

    def _get_client_user_acl_lines(self, for_sentinel: bool = False) -> str:
        """Generate the ACL lines for all external client users.

        Returns:
            str: ACL lines for the external client users.
        """
        sentinel_base_permissions = "-@all +auth +client +command +hello +ping +role "
        sentinel_sentinel_permissions = "+sentinel|get-master-addr-by-name +sentinel|master +sentinel|masters +sentinel|replicas +sentinel|sentinels"
        acl_content = ""

        if not (external_client_users := self.state.cluster.external_users_credentials):
            return acl_content

        for username, values in external_client_users.items():
            permissions = f"-@all +@read +@write +@keyspace +@pubsub +@transaction +info ~{values['resource']} &{values['resource']}"
            if for_sentinel:
                permissions = sentinel_base_permissions + sentinel_sentinel_permissions
            password_hash = hashlib.sha256(values["password"].encode("utf-8")).hexdigest()
            acl_content += f"user {username} on #{password_hash} {permissions}\n"

        return acl_content

    def get_sentinel_config_properties(
        self, primary_endpoint: str
    ) -> dict[str, str | dict[str, str]]:
        """Assemble the sentinel config properties.

        Returns:
            Dictionary of sentinel properties to be written to the config file.
        """
        config_properties = {}
        if not self.state.unit_server.model or not self.state.cluster.model:
            return config_properties
        sentinel_properties = {}

        # load the config properties provided from the template in this repo
        # it does NOT load the file from disk in the charm unit in order to avoid config drift
        with open(f"{WORKING_DIR}/config-template/sentinel.conf") as config:
            # The sentinel.conf file contains a number of directives that have a simple format:
            # keyword argument1 argument2 ... argumentN
            # sentinel keyword argument1 argument2 ... argumentN
            for line in config:
                line = line.strip().lower()
                if not line or line.startswith("#"):
                    # ignore comments and empty lines
                    continue
                elif line.startswith("sentinel "):
                    try:
                        # some config options for sentinel start with "sentinel" followed by the
                        # directive and its arguments, for example "sentinel monitor mymaster
                        key, value = line.split(" ", 2)[1:]
                    except ValueError:
                        key = line.strip().split(" ", 1)[1]
                        value = ""
                    sentinel_properties[key.strip()] = value.strip().replace(
                        "mymaster", PRIMARY_NAME
                    )
                else:
                    try:
                        # other config options that are not specific to sentinel
                        # just have the format "keyword argument1 argument2 ... argumentN",
                        # for example "port 6379"
                        key, value = line.split(" ", 1)
                    except ValueError:
                        key = line.strip()
                        value = ""
                    config_properties[key.strip()] = value.strip()

        config_properties["aclfile"] = self.workload.sentinel_acl_file.as_posix()

        # sentinel configs
        config_properties["sentinel"] = sentinel_properties | self._generate_sentinel_configs(
            primary_endpoint=primary_endpoint
        )

        # tls config
        tls_config = self.generate_sentinel_tls_config()
        config_properties.update(tls_config)

        return config_properties

    def _generate_sentinel_configs(self, primary_endpoint: str) -> dict[str, str]:
        """Generate the sentinel config properties based on the current cluster state."""
        sentinel_configs = {}
        sentinel_configs["monitor"] = f"{PRIMARY_NAME} {primary_endpoint} {TLS_PORT} {self.quorum}"
        # auth settings
        # auth-user is used by sentinel to authenticate to the valkey primary
        sentinel_configs["auth-user"] = f"{PRIMARY_NAME} {CharmUsers.VALKEY_SENTINEL.value}"
        sentinel_configs["auth-pass"] = (
            f"{PRIMARY_NAME} {self.state.cluster.internal_users_credentials.get(CharmUsers.VALKEY_SENTINEL.value, '')}"
        )
        # sentinel admin user settings used by sentinel for its own authentication
        sentinel_configs["sentinel-user"] = f"{CharmUsers.SENTINEL_ADMIN.value}"
        sentinel_configs["sentinel-pass"] = (
            f"{self.state.cluster.internal_users_credentials.get(CharmUsers.SENTINEL_ADMIN.value, '')}"
        )
        # TODO consider making these configs adjustable via charm config
        sentinel_configs["down-after-milliseconds"] = f"{PRIMARY_NAME} 30000"
        sentinel_configs["failover-timeout"] = f"{PRIMARY_NAME} 180000"
        sentinel_configs["parallel-syncs"] = f"{PRIMARY_NAME} 1"
        if self.state.substrate == Substrate.K8S:
            sentinel_configs["resolve-hostnames"] = "yes"
            sentinel_configs["announce-hostnames"] = "yes"
            sentinel_configs["announce-ip"] = self.state.unit_server.model.hostname
        return sentinel_configs

    def set_sentinel_config_properties(self, primary_endpoint: str) -> None:
        """Write sentinel configuration file."""
        logger.debug("Writing Sentinel configuration")

        sentinel_config = self.get_sentinel_config_properties(primary_endpoint=primary_endpoint)

        sentinel_config_string = "\n".join(
            f"sentinel {key} {value}" for key, value in sentinel_config["sentinel"].items()
        )
        other_config_string = "\n".join(
            f"{key} {value}" for key, value in sentinel_config.items() if key != "sentinel"
        )
        full_config_string = f"{other_config_string}\n{sentinel_config_string}"

        # on k8s we need to set the ownership of the sentinel config file to the non-root user that the valkey process runs as in order for sentinel to be able to read/write it
        self.workload.write_file(
            full_config_string,
            self.workload.sentinel_config_file,
            mode=0o600,
            user=self.workload.user,
            group=self.workload.user,
        )

    def set_sentinel_acl_file(self, passwords: dict[str, str] | None = None) -> None:
        """Write the Sentinel ACL file with appropriate user permissions.

        Args:
            passwords (dict[str, str] | None): Optional dictionary of passwords to use. If not provided,
                the passwords from the cluster state will be used.
        """
        logger.debug("Writing Sentinel ACL configuration")
        acl_content = "user default off\n"
        for user in CharmUsers:
            # only process VALKEY users
            # Sentinel users should be in the sentinel acl file
            if "VALKEY_" in user.name:
                continue
            acl_content += self._get_internal_user_acl_line(user, passwords=passwords)
        acl_content += self._get_client_user_acl_lines(for_sentinel=True)
        self.workload.write_file(
            acl_content,
            self.workload.sentinel_acl_file,
            user=self.workload.user,
            group=self.workload.user,
        )

    def generate_password(self) -> str:
        """Create randomized string for use as app passwords.

        Returns:
            str: String of 32 randomized letter+digit characters
        """
        return "".join([secrets.choice(string.ascii_letters + string.digits) for _ in range(32)])

    def update_local_valkey_admin_password(self) -> None:
        """Update the local unit's valkey admin password in the state."""
        self.state.unit_server.update(
            {
                "charmed_operator_password_local_unit_copy": self.state.cluster.internal_users_credentials.get(
                    CharmUsers.VALKEY_ADMIN.value
                )
            }
        )

    def configure_services(self, primary_endpoint: str) -> None:
        """Start Valkey and Sentinel services.

        Raises:
            ValkeyConfigurationError: If there was an error during configuration.
        """
        try:
            self.update_local_valkey_admin_password()
            self.set_config_properties(primary_endpoint=primary_endpoint)
            self.set_acl_file()
            self.set_sentinel_config_properties(primary_endpoint=primary_endpoint)
            self.set_sentinel_acl_file()
        except (ValkeyWorkloadCommandError, ValueError) as e:
            logger.error("Failed to set configuration properties: %s", e)
            self.state.unit_server.update(
                {"start_state": StartState.CONFIGURATION_ERROR.value, "request_start_lock": False}
            )
            raise ValkeyConfigurationError("Failed to set configuration") from e

    @property
    def quorum(self) -> int:
        """Calculate the quorum based on the number of units in the cluster."""
        num_units = len([server for server in self.state.servers if server.is_active])
        return (num_units // 2) + 1

    def get_statuses(self, scope: Scope, recompute: bool = False) -> list[StatusObject]:
        """Compute the config manager's statuses."""
        status_list: list[StatusObject] = []

        return status_list if status_list else [CharmStatuses.ACTIVE_IDLE.value]
