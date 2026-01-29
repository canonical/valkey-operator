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
    ACL_FILE,
    CHARM_USER,
    CHARM_USERS_ROLE_MAP,
    CLIENT_PORT,
    DATA_DIR,
    PRIMARY_NAME,
    QUORUM_NUMBER,
    SENTINEL_CONFIG_FILE,
    SENTINEL_PORT,
    CharmUsers,
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

    @property
    def config_properties(self) -> dict[str, str]:
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
        # dir
        config_properties["dir"] = DATA_DIR
        # port
        config_properties["port"] = str(CLIENT_PORT)

        # bind to all interfaces
        config_properties["bind"] = "0.0.0.0 -::1"

        # Use the ACL file
        config_properties["aclfile"] = ACL_FILE

        # # logfile location
        # config_properties["logfile"] = VALKEY_LOG_FILE

        logger.debug(
            "primary: %s, hostname: %s",
            self.state.cluster.model.primary_ip,
            self.state.unit_server.model.hostname,
        )
        # replicaof
        if (
            self.state.cluster.model.primary_ip
            and self.state.cluster.model.primary_ip != self.state.unit_server.model.private_ip
        ):
            # set replicaof
            logger.debug("Setting replicaof to primary %s", self.state.cluster.model.primary_ip)
            config_properties["replicaof"] = f"{self.state.cluster.model.primary_ip} {CLIENT_PORT}"
            config_properties["primaryuser"] = "replication-user"
            config_properties["primaryauth"] = "testpassword"  # TODO make this configurable

        return config_properties

    def set_config_properties(self) -> None:
        """Write the config properties to the config file."""
        logger.debug("Writing configuration")
        self.workload.write_config_file(config=self.config_properties)

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
            if "VALKEY_" not in str(user):
                continue
            acl_content += self._get_user_acl_line(user, passwords=passwords)
        self.workload.write_file(acl_content, ACL_FILE)

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
        acl_line = f"user {user.value} on #{password_hash} {CHARM_USERS_ROLE_MAP[user]}\n"
        return acl_line

    def set_sentinel_config(self) -> None:
        """Write sentinel configuration file."""
        if not self.state.cluster.model or not self.state.cluster.model.primary_ip:
            logger.warning("Cannot write sentinel config without primary details set")
            return
        if not (
            charmed_sentinel_valkey_password := self.state.cluster.internal_users_credentials.get(
                CharmUsers.VALKEY_SENTINEL.value
            )
        ):
            logger.warning("Cannot write sentinel config without sentinel user credentials set")
            return
        logger.debug("Writing Sentinel configuration")

        sentinel_config = f"port {SENTINEL_PORT}\n"
        # TODO consider adding quorum calculation based on number of units
        sentinel_config += f"sentinel monitor {PRIMARY_NAME} {self.state.cluster.model.primary_ip} {CLIENT_PORT} {QUORUM_NUMBER}\n"
        sentinel_config += (
            f"sentinel auth-user {PRIMARY_NAME} {CharmUsers.VALKEY_SENTINEL.value}\n"
        )
        sentinel_config += (
            f"sentinel auth-pass {PRIMARY_NAME} {charmed_sentinel_valkey_password}\n"
        )
        # TODO consider making these configs adjustable via charm config
        sentinel_config += f"sentinel down-after-milliseconds {PRIMARY_NAME} 30000\n"
        sentinel_config += f"sentinel failover-timeout {PRIMARY_NAME} 180000\n"
        sentinel_config += f"sentinel parallel-syncs {PRIMARY_NAME} 1\n"

        self.workload.write_file(
            sentinel_config, SENTINEL_CONFIG_FILE, mode=0o600, user=CHARM_USER, group=CHARM_USER
        )

    def generate_password(self) -> str:
        """Create randomized string for use as app passwords.

        Returns:
            str: String of 32 randomized letter+digit characters
        """
        return "".join([secrets.choice(string.ascii_letters + string.digits) for _ in range(32)])

    def get_statuses(self, scope: Scope, recompute: bool = False) -> list[StatusObject]:
        """Compute the config manager's statuses."""
        status_list: list[StatusObject] = []

        return status_list if status_list else [CharmStatuses.ACTIVE_IDLE.value]
