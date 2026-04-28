#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Collection of global literals for the Valkey charm."""

from enum import StrEnum

CHARM = "valkey"
CONTAINER = "valkey"

SNAP_NAME = "charmed-valkey"
SNAP_REVISIONS = {"x86_64": 16, "aarch64": 15}
SNAP_SERVICE = "server"
SNAP_SENTINEL_SERVICE = "sentinel"
SNAP_COMMON_PATH = "var/snap/charmed-valkey/common"
SNAP_CURRENT_PATH = "var/snap/charmed-valkey/current"
SNAP_CONFIG_FILE = "etc/charmed-valkey/valkey.conf"
SNAP_SENTINEL_CONFIG_FILE = "etc/charmed-valkey/sentinel.conf"
SNAP_ACL_FILE = "etc/charmed-valkey/users.acl"
SNAP_SENTINEL_ACL_FILE = "etc/charmed-valkey/sentinel-users.acl"

# todo: update these paths once directories in the rock are complying with the standard
CONFIG_FILE = "var/lib/valkey/valkey.conf"
SENTINEL_CONFIG_FILE = "var/lib/valkey/sentinel.conf"
ACL_FILE = "var/lib/valkey/users.acl"
SENTINEL_ACL_FILE = "var/lib/valkey/sentinel-users.acl"

# todo: these paths require root access, should be moved to dedicated user directories
TOPOLOGY_OBSERVER_LOG_FILE = "/var/log/topology_observer.log"
TOPOLOGY_OBSERVER_TLS_CA_FILE = "/etc/ssl/certs/valkey_ca.pem"

PEER_RELATION = "valkey-peers"
STATUS_PEERS_RELATION = "status-peers"
CLIENT_TLS_RELATION_NAME = "client-certificates"
CERTIFICATE_TRANSFER_RELATION = "certificate-transfer"
EXTERNAL_CLIENTS_RELATION = "valkey-client"

CLIENT_PORT = 6379
TLS_PORT = 6380
SENTINEL_PORT = 26379
SENTINEL_TLS_PORT = 26380

PRIMARY_NAME = "primary"
QUORUM_NUMBER = 2
INTERNAL_USERS_PASSWORD_CONFIG = "system-users"
INTERNAL_USERS_SECRET_LABEL_SUFFIX = "internal_users_secret"
CLIENTS_USERS_SECRET_LABEL_SUFFIX = "client_users_secret"
INTERNAL_CERTS_SECRET_LABEL_SUFFIX = "internal_certificates_secret"
TLS_CLIENT_PRIVATE_KEY_CONFIG = "tls-client-private-key"

DATA_STORAGE = "data"


# As per the valkey users spec
# https://docs.google.com/document/d/1EImKKHK3wLY73-D1M2ItpHe88NHeB-Iq2M3lz7AQB7E
class CharmUsers(StrEnum):
    """Enumeration of Valkey charm users."""

    VALKEY_ADMIN = "charmed-operator"
    VALKEY_SENTINEL = "charmed-sentinel-valkey"
    VALKEY_REPLICA = "charmed-replication"
    VALKEY_MONITORING = "charmed-stats"

    # Sentinel users
    SENTINEL_ADMIN = "charmed-sentinel-peers"
    SENTINEL_CHARM_ADMIN = "charmed-sentinel-operator"


CHARM_USERS_ROLE_MAP = {
    CharmUsers.VALKEY_ADMIN: "~* +@all",
    CharmUsers.VALKEY_SENTINEL: "+subscribe +publish +failover +script|kill +ping +info +multi +slaveof +config +client +exec &__sentinel__:hello",
    CharmUsers.VALKEY_REPLICA: "+psync +replconf +ping",
    CharmUsers.VALKEY_MONITORING: "-@all +@connection +memory -readonly +strlen +config|get +xinfo +pfcount -quit +zcard +type +xlen -readwrite -command +client -wait +scard +llen +hlen +get +eval +slowlog +cluster|info +cluster|slots +cluster|nodes -hello -echo +info +latency +scan -reset -auth -asking",
    CharmUsers.SENTINEL_ADMIN: "~* +@all",
    CharmUsers.SENTINEL_CHARM_ADMIN: "~* +@all",
}


class Substrate(StrEnum):
    """Substrate types."""

    VM = "vm"
    K8S = "k8s"


class StartState(StrEnum):
    """Start states for the service."""

    NOT_STARTED = "not_started"
    WAITING_TO_START = "waiting_to_start"
    WAITING_FOR_PRIMARY_START = "waiting_for_primary_start"
    CONFIGURATION_ERROR = "configuration_error"
    STARTING_WAITING_VALKEY = "starting_waiting_valkey"
    STARTING_WAITING_SENTINEL = "starting_waiting_sentinel"
    STARTING_WAITING_REPLICA_SYNC = "starting_waiting_replica_sync"
    ERROR_ON_START = "error_on_start"
    STARTED = "started"


class ScaleDownState(StrEnum):
    """Scale down states for the service."""

    NO_SCALE_DOWN = ""
    WAIT_FOR_LOCK = "wait_for_lock"
    WAIT_TO_FAILOVER = "wait_to_failover"
    STOP_SERVICES = "stopping_services"
    RESET_SENTINEL = "reset_sentinel"
    HEALTH_CHECK = "health_check"
    GOING_AWAY = "going_away"


class TLSState(StrEnum):
    """TLS states."""

    NO_TLS = "no-tls"
    TO_TLS = "to-tls"
    TLS = "tls"
    TO_NO_TLS = "to-no-tls"


class TLSCARotationState(StrEnum):
    """TLS CA Rotation state."""

    NO_ROTATION = "no-rotation"
    NEW_CA_DETECTED = "new-ca-detected"
    NEW_CA_ADDED = "new-ca-added"
    CA_UPDATED = "ca-updated"


class K8sService(StrEnum):
    """Services managed by the charm in Kubernetes."""

    PRIMARY = "primary"
    REPLICAS = "replicas"
