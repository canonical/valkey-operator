#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Collection of global literals for the Valkey charm."""

from enum import StrEnum

CHARM = "valkey"
CONTAINER = "valkey"

SNAP_NAME = "valkey-charmed"
SNAP_LOGS_SLOT = f"{SNAP_NAME}:logs"
SNAP_SERVICE = "server"
SNAP_SENTINEL_SERVICE = "sentinel"
METRICS_SERVICE = "metrics-exporter"
LOGROTATE_SERVICE = "logrotate"
LOGROTATE_CONFIG_FILE = "logrotate.conf"
LOGROTATE_VM_CONFIG_FILE = "etc/logrotate.d/charmed-valkey"
LOGROTATE_CRON_FILE = "etc/cron.d/charmed-valkey"
SNAP_COMMON_PATH = f"var/snap/{SNAP_NAME}/common"
SNAP_CURRENT_PATH = f"var/snap/{SNAP_NAME}/current"
SNAP_CONFIG_FILE = "etc/valkey/valkey.conf"
SNAP_SENTINEL_CONFIG_FILE = "etc/valkey/sentinel.conf"
SNAP_ACL_FILE = "etc/valkey/users.acl"
SNAP_SENTINEL_ACL_FILE = "etc/valkey/sentinel-users.acl"
METRICS_EXPORTER_ENV_FILE = "etc/valkey/metrics-exporter.env"

COS_DIR = "src/cos"
METRICS_RULES_DIR = f"{COS_DIR}/alert_rules/prometheus"
LOGS_RULES_DIR = f"{COS_DIR}/alert_rules/loki"
DASHBOARDS_DIR = f"{COS_DIR}/grafana_dashboards"

CONFIG_FILE = "var/lib/valkey/valkey.conf"
SENTINEL_CONFIG_FILE = "var/lib/valkey/sentinel.conf"
ACL_FILE = "var/lib/valkey/users.acl"
SENTINEL_ACL_FILE = "var/lib/valkey/sentinel-users.acl"
LOGROTATE_STATE_FILE = "var/lib/valkey/logrotate.status"

TOPOLOGY_OBSERVER_LOG_FILENAME = "topology_observer.log"
TOPOLOGY_OBSERVER_LOG_MAX_BYTES = 10 * 1024**2
TOPOLOGY_OBSERVER_LOG_BACKUPS = 3
TOPOLOGY_OBSERVER_TLS_CA_FILENAME = "valkey_ca.pem"
TOPOLOGY_OBSERVER_PID_FILENAME = "topology_observer.pid"

# The non-root K8s charm user can write under /var/lib/juju but not /var/lib
ROLLINGOPS_BASE_DIR = "/var/lib/juju/rollingops"

PEER_RELATION = "valkey-peers"
STATUS_PEERS_RELATION = "status-peers"
ROLLINGOPS_PEER_RELATION = "rollingops-peers"
CLIENT_TLS_RELATION_NAME = "client-certificates"
CERTIFICATE_TRANSFER_RELATION = "certificate-transfer"
LDAP_CA_CERT_RELATION = "ldap-ca-cert"
LDAP_RELATION = "ldap"
LDAP_GROUP_PLACEHOLDER_PROBE = "group-name-probe"
EXTERNAL_CLIENTS_RELATION = "valkey-client"
METRICS_ENDPOINT_RELATION = "metrics-endpoint"
GRAFANA_DASHBOARD_RELATION = "grafana-dashboard"
LOGGING_RELATION = "logging"
COS_AGENT_RELATION = "cos-agent"
S3_RELATION_NAME = "s3-credentials"
AZURE_RELATION_NAME = "azure-credentials"
GCS_RELATION_NAME = "gcs-credentials"

VALKEY_LOGS_SERVICE = "valkey-logs"
SENTINEL_LOGS_SERVICE = "sentinel-logs"
# azure-storage-integrator connection-protocol values that designate an https/http
# Blob endpoint. abfs/abfss designate ADLS-Gen2 (*.dfs.*), served by the datalake
# SDK rather than the Blob SDK, so they are rejected up front.
AZURE_HTTPS_PROTOCOLS = frozenset({"https", "wasbs"})
AZURE_HTTP_PROTOCOLS = frozenset({"http", "wasb"})
AZURE_REJECTED_PROTOCOLS = frozenset({"abfs", "abfss"})
# gcs-integrator storage-class values; applied only when the charm creates the bucket.
GCS_STORAGE_CLASSES = frozenset({"STANDARD", "NEARLINE", "COLDLINE", "ARCHIVE"})
# Every backup backend: its integrator relation -> the app databag field its
# validated credentials envelope is stored under. Relation discovery, the
# mutual-exclusion check, credential selection and the action guards all read
# this, so a new backend is one entry here plus its model, backend and wiring.
BACKUP_CREDENTIAL_FIELDS: dict[str, str] = {
    S3_RELATION_NAME: "s3_credentials",
    AZURE_RELATION_NAME: "azure_credentials",
    GCS_RELATION_NAME: "gcs_credentials",
}
BACKUP_ID_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# Structured code create-backup reports when an object already occupies the
# generated id. Ids have one-second resolution, so two units starting a backup
# in the same second would otherwise replace each other's snapshot.
BACKUP_EXISTS_CODE = "BackupAlreadyExists"
# Action error code for a GCS service-account key the SDK cannot parse.
GCS_INVALID_KEY_CODE = "InvalidServiceAccountKey"
BACKUP_CA_FILENAME = "s3_ca_chain.pem"
PRE_RESTORE_SUFFIX = ".pre-restore"
# Normal down-after (rendered by ConfigManager, reasserted by resume_failover);
# suppression raises it to a day so the primary's restart isn't seen as a failure.
SENTINEL_DOWN_AFTER_MS = 30000
SENTINEL_DOWN_AFTER_SUPPRESSED_MS = 86_400_000
# Bounded waits: big enough for a large RDB load/resync, small enough that a stuck
# restore self-terminates (freeing the cluster-wide hold) fast. Raise for bigger data.
RESTORE_LOAD_TIMEOUT_S = 600
RESTORE_RESYNC_TIMEOUT_S = 900
# TIMEOUT for execs on the workload commands
EXEC_TIMEOUT_S = 10
# Bound for one pass over the whole dataset on disk (SAVE, BGSAVE, AOF rewrite, valkey-check-aof).
# The scale-down lock TTL is derived from it. A 1 GB SAVE took about 12 s on a 100 MB/s disk.
DATASET_IO_TIMEOUT_S = 300
# Scale-down lock wait. The TTL covers the AOF rewrite and BGSAVE waits, SAVE and failover.
SCALE_DOWN_LOCK_TIMEOUT_S = 300
SCALE_DOWN_LOCK_TTL_S = 4 * DATASET_IO_TIMEOUT_S
SCALE_DOWN_LOCK_RETRY_INTERVAL_S = 5
SCALE_DOWN_LOCK_ID = "scale-down"
# rollingops callback ids
START_OPERATION_ID = "start"
RESTART_OPERATION_ID = "restart"

DEFAULT_RDB_SAVE_POINTS = "900 1 300 100 60 10000"
# Valkey's multi-part AOF layout inside the data dir (appenddirname and its manifest)
AOF_DIR_NAME = "appendonlydir"
AOF_MANIFEST_NAME = "appendonly.aof.manifest"
AOF_TEMP_FILE_PREFIX = "temp-"
RDB_FILE_NAME = "dump.rdb"
# One archive slot for the last corrupted AOF a self-heal moved aside
SNAP_CHECK_AOF_CLI = f"{SNAP_NAME}.check-aof"
CHECK_AOF_CLI = "valkey-check-aof"
# Valkey log lines around a start attempt, and the ones it logs before exiting on a corrupted AOF
# (aof.c, rdb.c). Open, permission and I/O errors are left out, since moving the AOF can't fix them.
VALKEY_START_LOG_LINE = "Valkey is starting"
VALKEY_READY_LOG_LINE = "Ready to accept connections"
# A start logs about 15 lines before it is ready or fails, plus client connections while it loads
VALKEY_LOG_TAIL_LINES = 10_000
AOF_LOAD_FATAL_LOG_LINES = (
    "Bad file format reading the append only file",
    "Error reading the append only file",
    "Unexpected end of file reading the append only file",
    "Fatal error: the truncated file is not the last file",
    "FATAL AOF MANIFEST FILE ERROR",
    "Error reading the RDB base file",
    "Terminating server after rdb file reading failure",
    # a file listed in the manifest is missing
    "length. stat: No such file or directory",
)

CLIENT_PORT = 6379
TLS_PORT = 6380
SENTINEL_PORT = 26379
SENTINEL_TLS_PORT = 26380
METRICS_PORT = 9121

PRIMARY_NAME = "primary"
QUORUM_NUMBER = 2
INTERNAL_USERS_PASSWORD_CONFIG = "system-users"
INTERNAL_USERS_SECRET_LABEL_SUFFIX = "internal_users_secret"
CLIENTS_USERS_SECRET_LABEL_SUFFIX = "client_users_secret"
INTERNAL_CERTS_SECRET_LABEL_SUFFIX = "internal_certificates_secret"
TLS_CLIENT_PRIVATE_KEY_CONFIG = "tls-client-private-key"

DATA_STORAGE = "data"
DATA_STORAGE_PATH = "var/lib/valkey"
LOG_STORAGE = "logs"
LOG_STORAGE_PATH = "var/log/valkey"
ARCHIVE_STORAGE = "archive"
ARCHIVE_STORAGE_PATH = "var/backups/valkey"

VALKEY_LOG_FILE = "valkey.log"
SENTINEL_LOG_FILE = "sentinel.log"


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


# States after the services were started, while the unit waits to become healthy.
STARTING_STATES = (
    StartState.STARTING_WAITING_VALKEY.value,
    StartState.STARTING_WAITING_SENTINEL.value,
    StartState.STARTING_WAITING_REPLICA_SYNC.value,
)


class RestoreStep(StrEnum):
    """Steps of the restore coordination workflow (ordered).

    RESTORE is one fused barrier (primary suppresses failover, downloads, swaps
    in the RDB); download and restore aren't split since no unit has work between.
    """

    NOT_STARTED = ""
    RESTORE = "restore"
    RESYNC = "resync"
    COMPLETED = "completed"


class RestoreFailure(StrEnum):
    """Per-unit restore failure kind, recorded on the failing unit's databag.

    UNHEALTHY marks a cluster that never became ready (surfaces RESTORE_UNHEALTHY);
    FAILED is any other failure.
    """

    FAILED = "failed"
    UNHEALTHY = "unhealthy"


class ScaleDownState(StrEnum):
    """Scale down states for the service."""

    NO_SCALE_DOWN = ""
    WAIT_FOR_LOCK = "wait_for_lock"
    WAIT_TO_FAILOVER = "wait_to_failover"
    STOP_SERVICES = "stopping_services"
    RESET_SENTINEL = "reset_sentinel"
    HEALTH_CHECK = "health_check"
    GOING_AWAY = "going_away"


class PersistenceMode(StrEnum):
    """Values of the `persistence-mode` config option."""

    RDB = "RDB"
    AOF = "AOF"
    RDB_AOF = "RDB+AOF"
    NONE = "None"


class AppendFsync(StrEnum):
    """Values of the `aof-appendfsync` config option."""

    ALWAYS = "always"
    EVERYSEC = "everysec"
    NO = "no"


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
