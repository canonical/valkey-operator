#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Manager for authentication and authorization."""

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
from literals import CHARM_USERS_ROLE_MAP, CharmUsers, StartState
from statuses import CharmStatuses

logger = logging.getLogger(__name__)

WORKING_DIR = Path(__file__).absolute().parent


class AuthManager(ManagerStatusProtocol):
    """Manage authentication and authorization."""

    name: str = "auth"
    state: ClusterState

    def __init__(self, state: ClusterState, workload: WorkloadBase):
        self.state = state  # pyright: ignore[reportIncompatibleVariableOverride]
        self.workload = workload

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

    def configure_auth(self) -> None:
        """Configure ACL files.

        Raises:
            ValkeyConfigurationError: If there was an error during configuration.
        """
        try:
            self.update_local_valkey_admin_password()
            self.set_acl_file()
            self.set_sentinel_acl_file()
        except (ValkeyWorkloadCommandError, ValueError) as e:
            logger.error("Failed to set configuration properties: %s", e)
            self.state.unit_server.update(
                {"start_state": StartState.CONFIGURATION_ERROR.value, "request_start_lock": False}
            )
            raise ValkeyConfigurationError("Failed to set configuration") from e

    def get_statuses(self, scope: Scope, recompute: bool = False) -> list[StatusObject]:
        """Compute the config manager's statuses."""
        status_list: list[StatusObject] = []

        return status_list if status_list else [CharmStatuses.ACTIVE_IDLE.value]
