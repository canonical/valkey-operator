#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Collection of state objects for the Valkey relations, apps and units."""

import logging
from typing import Any, final

import ops
from charmlibs.interfaces.tls_certificates import (
    Certificate,
    PrivateKey,
)
from charms.data_platform_libs.v1.data_interfaces import (
    ExtraSecretStr,
    OpsOtherPeerUnitRepositoryInterface,
    OpsPeerRepositoryInterface,
    OpsPeerUnitRepositoryInterface,
    OptionalSecretStr,
    PeerModel,
)
from pydantic import Field
from typing_extensions import Annotated

from literals import (
    INTERNAL_USERS_SECRET_LABEL_SUFFIX,
    INTERNET_CERTS_SECRET_LABEL_SUFFIX,
    CharmUsers,
    StartState,
    TLSCARotationState,
    TLSState,
)

logger = logging.getLogger(__name__)

InternalUsersSecret = Annotated[
    OptionalSecretStr, Field(exclude=True, default=None), INTERNAL_USERS_SECRET_LABEL_SUFFIX
]
InternalCertificatesSecret = Annotated[
    OptionalSecretStr, Field(exclude=True, default=None), INTERNET_CERTS_SECRET_LABEL_SUFFIX
]


class PeerAppModel(PeerModel):
    """Model for the peer application data."""

    charmed_operator_password: InternalUsersSecret = Field(default="")
    charmed_sentinel_valkey_password: InternalUsersSecret = Field(default="")
    charmed_replication_password: InternalUsersSecret = Field(default="")
    charmed_stats_password: InternalUsersSecret = Field(default="")
    charmed_sentinel_peers_password: InternalUsersSecret = Field(default="")
    charmed_sentinel_operator_password: InternalUsersSecret = Field(default="")
    starting_member: str = Field(default="")
    internal_ca_certificate: InternalCertificatesSecret = Field(default="")
    internal_ca_private_key: InternalCertificatesSecret = Field(default="")
    tls_client_private_key: ExtraSecretStr = Field(default=None)


class PeerUnitModel(PeerModel):
    """Model for the peer unit data."""

    charmed_operator_password_local_unit_copy: InternalUsersSecret = Field(default="")
    start_state: str = Field(default=StartState.NOT_STARTED.value)
    hostname: str = Field(default="")
    private_ip: str = Field(default="")
    request_start_lock: bool = Field(default=False)
    tls_client_state: str = Field(default="")
    client_cert_ready: bool = Field(default=False)
    tls_ca_rotation: str = Field(default="")
    tls_certificate_expiring: bool = Field(default=False)


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

    def update(self, items: dict[str, Any]) -> None:
        """Write to relation data."""
        if not self.relation:
            logger.warning(
                f"Fields {list(items.keys())} were attempted to be written on the relation before it exists."
            )
            return

        delete_fields = [key for key in items if not items[key]]
        update_content = {k: items[k] for k in items if k not in delete_fields}

        model = self.data_interface.build_model(self.relation.id)
        for field, value in update_content.items():
            setattr(model, field.replace("-", "_"), value)

        for field in delete_fields:
            setattr(model, field.replace("-", "_"), None)

        self.data_interface.write_model(self.relation.id, model)


@final
class ValkeyServer(RelationState):
    """State/Relation data collection for a unit."""

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
    def model(self) -> PeerUnitModel | None:
        """The peer relation model for this unit."""
        return self.data_interface.build_model(self.relation.id) if self.relation else None

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
    def valkey_admin_password(self) -> str:
        """Retrieve the password for the valkey admin user."""
        if not self.model:
            return ""
        return self.model.charmed_operator_password_local_unit_copy or ""

    @property
    def tls_client_state(self) -> TLSState:
        """The current TLS state of the Valkey server for client TLS."""
        if not self.model:
            return TLSState.NO_TLS

        return TLSState(self.model.tls_client_state or TLSState.NO_TLS.value)

    @property
    def tls_ca_rotation_state(self) -> TLSCARotationState:
        """Check if a TLS CA rotation is in progress."""
        if not self.model:
            return TLSCARotationState.NO_ROTATION

        return TLSCARotationState(
            self.model.tls_ca_rotation or TLSCARotationState.NO_ROTATION.value
        )


@final
class ValkeyCluster(RelationState):
    """State/Relation data collection for the Valkey application."""

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
    def model(self) -> PeerAppModel | None:
        """The peer relation model for this application."""
        return self.data_interface.build_model(self.relation.id) if self.relation else None

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

    @property
    def internal_ca_certificate(self) -> Certificate | None:
        """Retrieve the internal CA certificate."""
        if not self.model or not self.model.internal_ca_certificate:
            return None

        return Certificate.from_string(self.model.internal_ca_certificate)

    @property
    def internal_ca_private_key(self) -> PrivateKey | None:
        """Retrieve the internal CA private key."""
        if not self.model or not self.model.internal_ca_private_key:
            return None

        return PrivateKey.from_string(self.model.internal_ca_private_key)

    @property
    def tls_client_private_key(self) -> PrivateKey | None:
        """Retrieve the private key for client TLS."""
        if self.model and (private_key := self.model.tls_client_private_key):
            private_key = PrivateKey(raw=private_key)
            return private_key

        return None
