#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Collection of state objects for the Valkey relations, apps and units."""

import logging
from typing import Any, final

import ops
from charms.data_platform_libs.v1.data_interfaces import (
    OpsOtherPeerUnitRepositoryInterface,
    OpsPeerRepositoryInterface,
    OpsPeerUnitRepositoryInterface,
    OptionalSecretStr,
    PeerModel,
)
from pydantic import Field
from typing_extensions import Annotated

from literals import CharmUsers, ScaleDownState, StartState

logger = logging.getLogger(__name__)

InternalUsersSecret = Annotated[
    OptionalSecretStr, Field(exclude=True, default=None), "internal_users_secret"
]


class PeerAppModel(PeerModel):
    """Model for the peer application data."""

    charmed_operator_password: InternalUsersSecret = Field(default="")
    charmed_sentinel_valkey_password: InternalUsersSecret = Field(default="")
    charmed_replication_password: InternalUsersSecret = Field(default="")
    charmed_stats_password: InternalUsersSecret = Field(default="")
    charmed_sentinel_peers_password: InternalUsersSecret = Field(default="")
    charmed_sentinel_operator_password: InternalUsersSecret = Field(default="")
    start_member: str = Field(default="")
    scale_down_member: str = Field(default="")


class PeerUnitModel(PeerModel):
    """Model for the peer unit data."""

    charmed_operator_password_local_unit_copy: InternalUsersSecret = Field(default="")
    start_state: str = Field(default=StartState.NOT_STARTED.value)
    hostname: str = Field(default="")
    private_ip: str = Field(default="")
    request_start_lock: bool = Field(default=False)
    scale_down_state: str = Field(default="")


class RelationState:
    """Relation state object."""

    def __init__(
        self,
        relation: ops.model.Relation | None,
        data_interface: OpsPeerRepositoryInterface[PeerAppModel]
        | OpsPeerUnitRepositoryInterface[PeerUnitModel]
        | OpsOtherPeerUnitRepositoryInterface[PeerUnitModel],
        component: ops.model.Unit | ops.model.Application | None,
    ):
        self.relation = relation
        self.data_interface = data_interface
        self.component = component
        self.model = self.data_interface.build_model(self.relation.id) if self.relation else None

    def update(self, items: dict[str, Any]) -> None:
        """Write to relation data."""
        if not self.relation:
            logger.warning(
                f"Fields {list(items.keys())} were attempted to be written on the relation before it exists."
            )
            return

        delete_fields = [key for key in items if not items[key]]
        update_content = {k: items[k] for k in items if k not in delete_fields}

        for field, value in update_content.items():
            setattr(self.model, field.replace("-", "_"), value)

        for field in delete_fields:
            setattr(self.model, field.replace("-", "_"), None)

        self.data_interface.write_model(self.relation.id, self.model)


@final
class ValkeyServer(RelationState):
    """State/Relation data collection for a unit."""

    model: PeerUnitModel

    def __init__(
        self,
        relation: ops.model.Relation | None,
        data_interface: OpsPeerUnitRepositoryInterface[PeerUnitModel]
        | OpsOtherPeerUnitRepositoryInterface[PeerUnitModel],
        component: ops.model.Unit,
    ):
        super().__init__(relation, data_interface, component)
        self.data_interface = data_interface
        self.unit = component

    @property
    def unit_id(self) -> int:
        """The id of the unit from the unit name."""
        return int(self.unit.name.split("/")[1])

    @property
    def unit_name(self) -> str:
        """The unit's name."""
        return self.unit.name

    @property
    def is_started(self) -> bool:
        """Check if the unit has started."""
        return self.model.start_state == StartState.STARTED.value if self.model else False

    @property
    def is_being_removed(self) -> bool:
        """Check if the unit is being removed from the cluster."""
        return (
            self.model.scale_down_state != ScaleDownState.NO_SCALE_DOWN.value
            if self.model
            else False
        )

    @property
    def is_active(self) -> bool:
        """Check if the unit is started and not being removed."""
        return self.is_started and not self.is_being_removed

    @property
    def valkey_admin_password(self) -> str:
        """Retrieve the password for the valkey admin user."""
        if not self.model:
            return ""
        return self.model.charmed_operator_password_local_unit_copy or ""


@final
class ValkeyCluster(RelationState):
    """State/Relation data collection for the Valkey application."""

    model: PeerAppModel

    def __init__(
        self,
        relation: ops.model.Relation | None,
        data_interface: OpsPeerRepositoryInterface[PeerAppModel],
        component: ops.model.Application,
    ):
        super().__init__(relation, data_interface, component)
        self.app = component
        self.data_interface = data_interface

    @property
    def internal_users_credentials(self) -> dict[str, str]:
        """Retrieve the credentials for the internal admin users."""
        passwords = {}
        if not self.model:
            return passwords

        for user in CharmUsers:
            if password := getattr(self.model, f"{user.value.replace('-', '_')}_password", ""):
                passwords[user.value] = password
        return passwords
