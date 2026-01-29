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
    CLIENT_PORT,
    DATA_DIR,
    INTERNAL_USER,
    PRIMARY_NAME,
    QUORUM_NUMBER,
    SENTINEL_CONFIG_FILE,
    SENTINEL_PORT,
    SENTINEL_USER,
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

    def set_acl_file(self, charmed_operator_password: str = "") -> None:
        """Write the ACL file with appropriate user permissions.

        Args:
            charmed_operator_password (str): Password for the charmed-operator user. If not provided,
                the password from the cluster state will be used.
        """
        logger.debug("Writing ACL configuration")
        if not charmed_operator_password:
            charmed_operator_password = self.state.cluster.internal_user_credentials.get(
                INTERNAL_USER, ""
            )
        # sha256 hash the password
        charmed_operator_password_hash = hashlib.sha256(
            charmed_operator_password.encode("utf-8")
        ).hexdigest()
        # sentinel user
        charmed_replication_password = self.state.cluster.internal_user_credentials.get(
            SENTINEL_USER, ""
        )
        charmed_replication_password_hash = hashlib.sha256(
            charmed_replication_password.encode("utf-8")
        ).hexdigest()
        # write the ACL file
        acl_content = "user default off\n"
        acl_content += f"user {INTERNAL_USER} on #{charmed_operator_password_hash} ~* +@all\n"
        acl_content += f"user {SENTINEL_USER} on #{charmed_replication_password_hash} +client +config +info +publish +subscribe +monitor +ping +replicaof +failover +script|kill +multi +exec &__sentinel__:hello\n"
        # TODO make the replication user password configurable
        acl_content += "user replication-user on >testpassword +psync +replconf +ping\n"
        self.workload.write_file(acl_content, ACL_FILE)

    def set_sentinel_config(self) -> None:
        """Write sentinel configuration file."""
        if not self.state.cluster.model or not self.state.cluster.model.primary_ip:
            logger.warning("Cannot write sentinel config without primary details set")
            return
        if not (
            charmed_replication_password := self.state.cluster.internal_user_credentials.get(
                SENTINEL_USER
            )
        ):
            logger.warning("Cannot write sentinel config without sentinel user credentials set")
            return
        logger.debug("Writing Sentinel configuration")

        sentinel_config = f"port {SENTINEL_PORT}\n"
        # TODO consider adding quorum calculation based on number of units
        sentinel_config += f"sentinel monitor {PRIMARY_NAME} {self.state.cluster.model.primary_ip} {CLIENT_PORT} {QUORUM_NUMBER}\n"
        sentinel_config += f"sentinel auth-user {PRIMARY_NAME} {SENTINEL_USER}\n"
        sentinel_config += f"sentinel auth-pass {PRIMARY_NAME} {charmed_replication_password}\n"
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
