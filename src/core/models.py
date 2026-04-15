#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Collection of state objects for the Valkey relations, apps and units."""

import json
import logging
from typing import Any, final

import ops
from charmlibs.interfaces.tls_certificates import (
    Certificate,
    PrivateKey,
)
from dpcharmlibs.interfaces import (
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
    CLIENTS_USERS_SECRET_LABEL_SUFFIX,
    INTERNAL_CERTS_SECRET_LABEL_SUFFIX,
    INTERNAL_USERS_SECRET_LABEL_SUFFIX,
    CharmUsers,
    ScaleDownState,
    StartState,
    Substrate,
    TLSCARotationState,
    TLSState,
)

logger = logging.getLogger(__name__)

InternalUsersSecret = Annotated[
    OptionalSecretStr, Field(exclude=True, default=None), INTERNAL_USERS_SECRET_LABEL_SUFFIX
]
ClientUsersSecret = Annotated[
    OptionalSecretStr, Field(exclude=True, default=None), CLIENTS_USERS_SECRET_LABEL_SUFFIX
]
InternalCertificatesSecret = Annotated[
    OptionalSecretStr, Field(exclude=True, default=None), INTERNAL_CERTS_SECRET_LABEL_SUFFIX
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
    internal_ca_certificate: InternalCertificatesSecret = Field(default="")
    internal_ca_private_key: InternalCertificatesSecret = Field(default="")
    tls_client_private_key: ExtraSecretStr = Field(default=None)
    external_client_users: ClientUsersSecret = Field(default="")
    client_user_epoch: float = Field(default=0)


class PeerUnitModel(PeerModel):
    """Model for the peer unit data."""

    charmed_operator_password_local_unit_copy: InternalUsersSecret = Field(default="")
    start_state: str = Field(default=StartState.NOT_STARTED.value)
    hostname: str = Field(default="")
    private_ip: str = Field(default="")
    request_start_lock: bool = Field(default=False)
    scale_down_state: str = Field(default="")
    tls_client_state: str = Field(default="")
    client_cert_ready: bool = Field(default=False)
    tls_ca_rotation: str = Field(default="")
    tls_certificate_expiring: bool = Field(default=False)
    client_user_epoch: float = Field(default=0)


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
                "Fields %s were attempted to be written on the relation before it exists.",
                list(items.keys()),
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
            self.model.scale_down_state == ScaleDownState.GOING_AWAY.value if self.model else False
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

    @property
    def tls_client_state(self) -> TLSState:
        """The current TLS state of the Valkey server for client TLS."""
        if not self.model:
            return TLSState.NO_TLS

        return TLSState(self.model.tls_client_state or TLSState.NO_TLS.value)

    @property
    def is_tls_enabled(self) -> bool:
        """Check if TLS is enabled for client connections."""
        return self.tls_client_state in [TLSState.TLS, TLSState.TO_NO_TLS]

    def get_endpoint(self, substrate: Substrate) -> str:
        """Return the endpoint to be used by other units to connect to this unit.

        On VM-based substrates, this should be the private IP address.
        On Kubernetes, this should be the hostname of the unit.
        """
        return self.model.private_ip if substrate == Substrate.VM else self.model.hostname

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

    @property
    def external_users_credentials(self) -> dict[str, dict[str, str]] | None:
        """Retrieve the user credentials for external clients from the state and return as dict.

        Example:
            "external-client-users":
                "{
                    "relation-3-0cbbc9781f189ea5":
                    {"resource": "test:*", "password": "mypassword"},
                    "relation-4-08154711":
                    {"resource": "another_keyspace:*", "password": "anotherpassword"}
                }"
        """
        if not self.model or not (external_clients := self.model.external_client_users):
            return None

        return json.loads(external_clients)

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
            return PrivateKey(raw=private_key)

        return None
