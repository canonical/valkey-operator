#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared helpers and fixtures for observability integration tests."""

import logging
import re
import shutil
import subprocess

import jubilant

from literals import METRICS_PORT
from tests.integration.helpers import APP_NAME

logger = logging.getLogger(__name__)

NUM_UNITS = 3
COS_CHANNEL = "2/stable"
OTELCOL_K8S_APP = "otelcol-k8s"
OTELCOL_VM_APP = "otelcol"
PROMETHEUS_APP = "prometheus"
GRAFANA_APP = "grafana"
LOKI_APP = "loki"


def read_metrics(juju: jubilant.Juju, unit_name: str, host: str = "127.0.0.1") -> str:
    """Fetch metrics from exporter on the given unit, falling back to python3 urllib if curl is absent."""
    cmd = (
        f"curl -sf http://{host}:{METRICS_PORT}/metrics 2>/dev/null "
        f"|| python3 -c \"import urllib.request; print(urllib.request.urlopen('http://{host}:{METRICS_PORT}/metrics').read().decode())\""
    )
    return juju.ssh(target=unit_name, command=cmd)


def probe_port(juju: jubilant.Juju, unit_name: str, host: str, port: int = METRICS_PORT) -> bool:
    """Check if a specific port is reachable on the given host address."""
    cmd = (
        f'python3 -c "import urllib.request; '
        f"urllib.request.urlopen('http://{host}:{port}/metrics', timeout=2)\""
    )
    try:
        juju.ssh(target=unit_name, command=cmd)
        return True
    except Exception:
        return False


def get_relation_data(juju: jubilant.Juju, unit_name: str, endpoint: str) -> dict:
    """Retrieve relation data dictionary for a specific endpoint from juju show_unit."""
    unit_info = juju.show_unit(unit_name)
    for rel in unit_info.relation_info:
        if rel.endpoint == endpoint:
            return {
                "relation-id": rel.relation_id,
                "endpoint": rel.endpoint,
                "related-endpoint": rel.related_endpoint,
                "application-data": rel.app_data,
                "related-units": {k: {"data": v.data} for k, v in rel.related_units.items()},
            }
    return {}


def get_subordinate_relation_data(juju: jubilant.Juju, principal_unit: str, endpoint: str) -> dict:
    """Retrieve relation data from the subordinate unit attached to principal_unit."""
    status = juju.status()
    unit_status = status.apps[APP_NAME].units.get(principal_unit)
    if not unit_status or not unit_status.subordinates:
        return {}

    subordinate_unit = list(unit_status.subordinates.keys())[0]
    return get_relation_data(juju, subordinate_unit, endpoint)


def is_integrated(juju: jubilant.Juju, app_name: str, endpoint: str, target_app: str) -> bool:
    """Check if an application endpoint is currently integrated with target_app."""
    status = juju.status()
    if app_name not in status.apps:
        return False
    relations = status.apps[app_name].relations.get(endpoint, [])
    return any(rel.related_app == target_app for rel in relations)


def assert_redis_up_and_single_primary(metrics_by_unit: dict[str, str]) -> None:
    """Assert all units report redis_up 1 and exactly one unit reports instance_role="master"."""
    master_count = 0
    for unit_name, metrics_output in metrics_by_unit.items():
        assert re.search(r"^redis_up(?:\{[^}]*\})?\s+1", metrics_output, re.MULTILINE), (
            f"Expected 'redis_up 1' on {unit_name}, got:\n{metrics_output[:500]}"
        )
        if re.search(r'redis_up\{[^}]*instance_role="master"[^}]*\}\s+1', metrics_output):
            master_count += 1

    assert master_count == 1, (
        f"Expected exactly 1 master reported across exporter instances, found {master_count}"
    )


def ensure_k8s_dns_resolution(juju: jubilant.Juju, app_name: str) -> None:
    """Configure VM units to resolve Kubernetes cluster.local domain names."""
    # The DNS pod's ClusterIP is not routable from the VM units (only routes through
    # kube-proxy on the cluster's own nodes), so resolve the pod IP directly instead.
    # microk8s's CoreDNS deployment keeps the legacy "kube-dns" label; Canonical K8s (the
    # `k8s` snap) labels it "coredns".
    dns_label = "k8s-app=kube-dns" if shutil.which("microk8s") else "k8s-app=coredns"
    cmd = (
        f"kubectl get pods -n kube-system -l {dns_label} "
        "-o jsonpath='{.items[0].status.podIP}' 2>/dev/null"
    )
    try:
        dns_ip = subprocess.check_output(cmd, shell=True).decode().strip().strip("'\"")
    except Exception:
        dns_ip = ""
    dns_ip = dns_ip or "10.152.183.10"
    logger.info("Configuring K8s DNS on VM units using server: %s", dns_ip)

    status = juju.status()
    for unit_name in status.apps[app_name].units:
        try:
            juju.ssh(
                unit_name,
                f"sudo resolvectl dns eth0 {dns_ip} && "
                f"sudo resolvectl domain eth0 ~cluster.local && "
                f"sudo resolvectl flush-caches && "
                f"(sudo snap restart opentelemetry-collector 2>/dev/null || "
                f"sudo systemctl restart snap.opentelemetry-collector.opentelemetry-collector "
                f"2>/dev/null || true)",
            )
        except Exception as e:
            logger.warning("Could not set DNS on %s: %s", unit_name, e)
