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
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
)
from typing_extensions import Annotated

from literals import (
    CLIENTS_USERS_SECRET_LABEL_SUFFIX,
    INTERNAL_CERTS_SECRET_LABEL_SUFFIX,
    INTERNAL_USERS_SECRET_LABEL_SUFFIX,
    CharmUsers,
    RestoreStep,
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


class S3Parameters(BaseModel):
    """Validated, normalised S3 connection parameters from the s3 relation.

    Parses the s3-integrator envelope (hyphenated keys) into typed
    attributes, trimming whitespace and the separators that would corrupt
    S3 key paths, and rejecting an envelope missing a required field or
    whose bucket/endpoint/path strip to empty. Unknown integrator fields
    (``storage-class``, ``s3-uri-style``, ...) are ignored.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    bucket: str
    endpoint: str
    path: str
    access_key: str = Field(alias="access-key")
    secret_key: str = Field(alias="secret-key")
    region: str | None = None
    tls_ca_chain: list[str] = Field(alias="tls-ca-chain", default_factory=list)

    @field_validator(
        "bucket", "endpoint", "path", "access_key", "secret_key", "region", mode="before"
    )
    @classmethod
    def _strip_whitespace(cls, value: object) -> object:
        # A copy-pasted key with a trailing newline is common.
        return value.strip() if isinstance(value, str) else value

    @field_validator("tls_ca_chain", mode="before")
    @classmethod
    def _coerce_ca_chain(cls, value: object) -> object:
        # A misconfigured integrator may send a bare string; never let that
        # reject the whole envelope -- drop to no chain (boto3 uses system
        # CAs). store_tls_ca_chain does the strict PEM check before writing.
        return value if isinstance(value, list) else []

    @field_validator("endpoint")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        # A trailing "/" on the endpoint would double up in the request URL.
        return value.rstrip("/")

    @field_validator("bucket", "path")
    @classmethod
    def _strip_surrounding_slashes(cls, value: str) -> str:
        # Leading/trailing "/" would yield keys like "//<id>"; stripping also
        # collapses bucket="/" or path="/" to "" so _reject_empty catches it.
        return value.strip("/")

    @field_validator("bucket", "endpoint", "path", "access_key", "secret_key")
    @classmethod
    def _reject_empty(cls, value: str, info: ValidationInfo) -> str:
        # An empty path makes list_backups enumerate the whole bucket
        # (cross-tenant leak in a shared bucket); reject empties outright.
        if not value:
            raise ValueError(f"{info.field_name} must not be empty")
        return value


class PeerAppModel(PeerModel):
    """Model for the peer application data."""

    charmed_operator_password: InternalUsersSecret = Field(default="")
    charmed_sentinel_valkey_password: InternalUsersSecret = Field(default="")
    charmed_replication_password: InternalUsersSecret = Field(default="")
    charmed_stats_password: InternalUsersSecret = Field(default="")
    charmed_sentinel_peers_password: InternalUsersSecret = Field(default="")
    charmed_sentinel_operator_password: InternalUsersSecret = Field(default="")
    start_member: str = Field(default="")
    restart_member: str = Field(default="")
    internal_ca_certificate: InternalCertificatesSecret = Field(default="")
    internal_ca_private_key: InternalCertificatesSecret = Field(default="")
    tls_client_private_key: ExtraSecretStr = Field(default=None)
    external_client_users: ClientUsersSecret = Field(default="")
    client_user_epoch: float = Field(default=0)
    s3_credentials: ExtraSecretStr = Field(default=None)
    restore_id: str = Field(default="")
    restore_instruction: str = Field(default="")
    restore_participants: str = Field(default="")


class PeerUnitModel(PeerModel):
    """Model for the peer unit data."""

    charmed_operator_password_local_unit_copy: InternalUsersSecret = Field(default="")
    start_state: str = Field(default=StartState.NOT_STARTED.value)
    hostname: str = Field(default="")
    private_ip: str = Field(default="")
    request_start_lock: bool = Field(default=False)
    request_restart_lock: bool = Field(default=False)
    scale_down_state: str = Field(default="")
    tls_client_state: str = Field(default="")
    client_cert_ready: bool = Field(default=False)
    tls_ca_rotation: str = Field(default="")
    tls_certificate_expiring: bool = Field(default=False)
    is_valkey_healthy: bool = Field(default=True)
    is_sentinel_healthy: bool = Field(default=True)
    client_user_epoch: float = Field(default=0)
    topology_observer_pid: int = Field(default=0)
    backup_id: str = Field(default="")
    restore_step: str = Field(default="")
    restore_role: str = Field(default="")


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
        # `self.model` is only built when `self.relation` exists, so both are checked together.
        if not self.relation or self.model is None:
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
    def is_backup_in_progress(self) -> bool:
        """Check if a backup is currently being uploaded by this unit."""
        return bool(self.model.backup_id) if self.model else False

    @property
    def restore_step(self) -> RestoreStep:
        """This unit's most recently completed restore step."""
        if not self.model:
            return RestoreStep.NOT_STARTED
        return RestoreStep(self.model.restore_step or RestoreStep.NOT_STARTED.value)

    @property
    def restore_role(self) -> str:
        """`primary` or `replica`, captured at DOWNLOAD; empty if not in a restore."""
        return self.model.restore_role if self.model else ""

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
    def s3_credentials(self) -> "S3Parameters | None":
        """Return the parsed S3 connection envelope, or None if not set.

        The leader writes a JSON-serialised ``S3Parameters`` to
        ``s3_credentials``; this parses it back for BackupManager. Callers
        gate on truthiness (``if s3_credentials``), so None reads as unset.
        The stored envelope was validated before writing, so a parse failure
        here is defensive and also reads as unset.
        """
        if not self.model or not self.model.s3_credentials:
            return None
        try:
            return S3Parameters.model_validate_json(self.model.s3_credentials)
        except ValidationError:
            return None

    @property
    def restore_id(self) -> str:
        """The backup id being restored, or '' if no restore is running."""
        return self.model.restore_id if self.model else ""

    @property
    def is_restore_in_progress(self) -> bool:
        """True while a restore is coordinating (restore_id is the flag)."""
        return bool(self.model.restore_id) if self.model else False

    @property
    def restore_instruction(self) -> RestoreStep:
        """Current target step every participant should advance to."""
        if not self.model or not self.model.restore_instruction:
            return RestoreStep.NOT_STARTED
        return RestoreStep(self.model.restore_instruction)

    @property
    def restore_participants(self) -> list[str]:
        """Unit names snapshotted at initiation; the fixed barrier set."""
        if not self.model or not self.model.restore_participants:
            return []
        return self.model.restore_participants.split(",")

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
