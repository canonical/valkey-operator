#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Collection of global literals for the Valkey charm."""

from enum import StrEnum

CHARM = "valkey"
CONTAINER = "valkey"

SNAP_NAME = "charmed-valkey"
SNAP_REVISION = 16
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

PEER_RELATION = "valkey-peers"
STATUS_PEERS_RELATION = "status-peers"


CLIENT_PORT = 6379
SENTINEL_PORT = 26379

PRIMARY_NAME = "primary"
QUORUM_NUMBER = 2
INTERNAL_USERS_PASSWORD_CONFIG = "system-users"
INTERNAL_USERS_SECRET_LABEL_SUFFIX = "internal_users_secret"


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
    CharmUsers.VALKEY_SENTINEL: "+client +config +info +publish +subscribe +monitor +ping +replicaof +failover +script|kill +multi +exec &__sentinel__:hello",
    CharmUsers.VALKEY_REPLICA: "+psync +replconf +ping",
    CharmUsers.VALKEY_MONITORING: "-@all +@connection +memory -readonly +strlen +config|get +xinfo +pfcount -quit +zcard +type +xlen -readwrite -command +client -wait +scard +llen +hlen +get +eval +slowlog +cluster|info +cluster|slots +cluster|nodes -hello -echo +info +latency +scan -reset -auth -asking",
    CharmUsers.SENTINEL_ADMIN: "~* +@all",
    CharmUsers.SENTINEL_CHARM_ADMIN: "~* +@all",
}


class Substrate(StrEnum):
    """Substrate types."""

    VM = "vm"
    K8S = "k8s"
