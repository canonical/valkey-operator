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
from literals import ACL_FILE, CHARM_USERS_ROLE_MAP, CLIENT_PORT, CharmUsers
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
        # port
        config_properties["port"] = str(CLIENT_PORT)

        # bind to all interfaces
        config_properties["bind"] = "0.0.0.0 -::1"

        # Use the ACL file
        config_properties["aclfile"] = str(ACL_FILE)

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
        return f"user {user.value} on #{password_hash} {CHARM_USERS_ROLE_MAP[user]}\n"

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
