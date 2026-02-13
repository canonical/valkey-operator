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

from core.base_workload import WorkloadBase
from core.cluster_state import ClusterState
from literals import (
    CHARM_USERS_ROLE_MAP,
    CLIENT_PORT,
    PRIMARY_NAME,
    QUORUM_NUMBER,
    SENTINEL_PORT,
    CharmUsers,
    Substrate,
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

    def get_config_properties(self, primary_ip: str) -> dict[str, str]:
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
        config_properties["port"] = str(CLIENT_PORT)
        config_properties["loglevel"] = "verbose"
        config_properties["aclfile"] = self.workload.acl_file.as_posix()
        config_properties["dir"] = self.workload.working_dir.as_posix()

        # bind to all interfaces
        if self.state.substrate == Substrate.VM:
            config_properties["bind"] = self.state.bind_address
        else:
            config_properties["bind"] = "0.0.0.0 -::1"

        # replica related config
        replica_config = self.generate_replica_config(primary_ip=primary_ip)
        config_properties.update(replica_config)

        return config_properties

    def generate_replica_config(self, primary_ip):
        """Generate the config properties related to replica configuration based on the current cluster state."""
        replica_config = {
            "primaryuser": CharmUsers.VALKEY_REPLICA.value,
            "primaryauth": self.state.cluster.internal_users_credentials.get(
                CharmUsers.VALKEY_REPLICA.value, ""
            ),
        }
        if primary_ip != self.state.unit_server.model.private_ip:
            # set replicaof
            logger.debug("Setting replicaof to primary %s", primary_ip)
            replica_config["replicaof"] = f"{primary_ip} {CLIENT_PORT}"
        return replica_config

    def set_config_properties(self, primary_ip: str) -> None:
        """Write the config properties to the config file."""
        logger.debug("Writing configuration")
        self.workload.write_config_file(config=self.get_config_properties(primary_ip=primary_ip))

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
            acl_content += self._get_user_acl_line(user, passwords=passwords)
        self.workload.write_file(
            acl_content,
            self.workload.acl_file,
            user=self.workload.user,
            group=self.workload.user,
        )

    def _get_user_acl_line(self, user: CharmUsers, passwords: dict[str, str] | None = None) -> str:
        """Generate an ACL line for a given user.

        Args:
            user (CharmUsers): User for which to generate the ACL line.
            passwords (dict[str, str] | None): Optional dictionary of passwords to use. If not provided,
                the passwords from the cluster state will be used.

        Returns:
            str: ACL line for the user.
        """
        passwords = passwords or self.state.cluster.internal_users_credentials
        if not (password := passwords.get(user.value, "")):
            raise ValueError(f"No password found for user {user}")
        password_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return f"user {user.value} on #{password_hash} {CHARM_USERS_ROLE_MAP[user]}\n"

    def set_sentinel_config_properties(self, primary_ip: str) -> None:
        """Write sentinel configuration file."""
        logger.debug("Writing Sentinel configuration")

        sentinel_config = f"port {SENTINEL_PORT}\n"

        sentinel_config += f"aclfile {self.workload.sentinel_acl_file.as_posix()}\n"
        # TODO consider adding quorum calculation based on number of units
        sentinel_config += (
            f"sentinel monitor {PRIMARY_NAME} {primary_ip} {CLIENT_PORT} {QUORUM_NUMBER}\n"
        )
        # auth settings
        # auth-user is used by sentinel to authenticate to the valkey primary
        sentinel_config += (
            f"sentinel auth-user {PRIMARY_NAME} {CharmUsers.VALKEY_SENTINEL.value}\n"
        )
        sentinel_config += f"sentinel auth-pass {PRIMARY_NAME} {self.state.cluster.internal_users_credentials.get(CharmUsers.VALKEY_SENTINEL.value, '')}\n"
        # sentinel admin user settings used by sentinel for its own authentication
        sentinel_config += f"sentinel sentinel-user {CharmUsers.SENTINEL_ADMIN.value}\n"
        sentinel_config += f"sentinel sentinel-pass {self.state.cluster.internal_users_credentials.get(CharmUsers.SENTINEL_ADMIN.value, '')}\n"
        # TODO consider making these configs adjustable via charm config
        sentinel_config += f"sentinel down-after-milliseconds {PRIMARY_NAME} 30000\n"
        sentinel_config += f"sentinel failover-timeout {PRIMARY_NAME} 180000\n"
        sentinel_config += f"sentinel parallel-syncs {PRIMARY_NAME} 1\n"

        # on k8s we need to set the ownership of the sentinel config file to the non-root user that the valkey process runs as in order for sentinel to be able to read/write it
        self.workload.write_file(
            sentinel_config,
            self.workload.sentinel_config,
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
            acl_content += self._get_user_acl_line(user, passwords=passwords)
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

    def update_local_valkey_admin(self) -> None:
        """Update the local unit's valkey admin password in the state."""
        if not (
            app_password := self.state.cluster.internal_users_credentials.get(
                CharmUsers.VALKEY_ADMIN.value
            )
        ):
            logger.warning("No valkey admin password found to update local unit state")
            return
        self.state.unit_server.update(
            {f"{CharmUsers.VALKEY_ADMIN.value.replace('-', '_')}_password": app_password}
        )

    def get_statuses(self, scope: Scope, recompute: bool = False) -> list[StatusObject]:
        """Compute the config manager's statuses."""
        status_list: list[StatusObject] = []

        return status_list if status_list else [CharmStatuses.ACTIVE_IDLE.value]
