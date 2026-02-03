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
    CLIENT_PORT,
    INTERNAL_USER,
    SNAP_ACL_FILE,
    SNAP_COMMON_PATH,
    SNAP_CURRENT_PATH,
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

    @property
    def config_properties(self) -> dict[str, str]:
        """Assemble the config properties.

        Returns:
            Dictionary of properties to be written to the config file.
        """
        config_properties = {}

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
        config_properties["bind"] = "0.0.0.0 -::1"
        config_properties["port"] = str(CLIENT_PORT)
        config_properties["loglevel"] = "verbose"

        if self.state.substrate == Substrate.VM:
            config_properties["dir"] = f"{SNAP_COMMON_PATH}/var/lib/charmed-valkey"
            config_properties["aclfile"] = f"{SNAP_CURRENT_PATH}/{SNAP_ACL_FILE}"
        else:
            config_properties["dir"] = "/var/lib/valkey"
            config_properties["aclfile"] = str(ACL_FILE)

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
        # write the ACL file
        acl_content = "user default off\n"
        acl_content += f"user {INTERNAL_USER} on #{charmed_operator_password_hash} ~* +@all\n"
        if self.state.substrate == Substrate.VM:
            self.workload.write_file(acl_content, f"{SNAP_CURRENT_PATH}/{SNAP_ACL_FILE}")
        else:
            self.workload.write_file(acl_content, ACL_FILE)

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
