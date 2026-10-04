#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the K8s workload's Pebble service lifecycle settings."""

from literals import PEBBLE_SERVICE_TIMEOUT_SECONDS, VALKEY_KILL_DELAY
from src.workload_k8s import ValkeyK8sWorkload


def _workload(mocker) -> ValkeyK8sWorkload:
    wl = ValkeyK8sWorkload.__new__(ValkeyK8sWorkload)
    wl.container = mocker.Mock()
    wl.valkey_service = "valkey"
    wl.sentinel_service = "sentinel"
    wl.metrics_service = "metrics-exporter"
    wl.valkey_logs_service = "valkey-logs"
    wl.sentinel_logs_service = "sentinel-logs"
    wl.config_file = mocker.Mock()
    wl.config_file.as_posix.return_value = "/var/lib/valkey/valkey.conf"
    wl.sentinel_config_file = mocker.Mock()
    wl.sentinel_config_file.as_posix.return_value = "/var/lib/valkey/sentinel.conf"
    wl.log_dir = mocker.MagicMock()
    wl.user = "_daemon_"
    wl._metrics_env = {}
    return wl


def test_valkey_service_gets_a_kill_delay_long_enough_for_the_shutdown_save(mocker):
    """Pebble must not SIGKILL Valkey before the replica wait plus RDB save finish.

    The default 5 s kill-delay lands inside shutdown-timeout, so a lagging
    replica would leave the primary with no final snapshot at all. 25 s stays
    under the 30 s pod termination grace period Juju fixes on the pod.
    """
    layer = _workload(mocker).pebble_layer.to_dict()

    assert layer["services"]["valkey"]["kill-delay"] == VALKEY_KILL_DELAY
    assert VALKEY_KILL_DELAY == "25s"
    # Sentinel exits immediately on SIGTERM; it keeps the Pebble default.
    assert "kill-delay" not in layer["services"]["sentinel"]


def test_full_start_waits_longer_than_the_kill_delay(mocker):
    """A full (re)start must outwait the 25 s stop, so it bypasses ops' 30 s default."""
    wl = _workload(mocker)
    mocker.patch.object(ValkeyK8sWorkload, "alive", return_value=True)

    wl.start()

    wl.container.add_layer.assert_called_once()
    wl.container.pebble.restart_services.assert_called_once_with(
        ["valkey", "sentinel", "metrics-exporter", "valkey-logs", "sentinel-logs"],
        timeout=PEBBLE_SERVICE_TIMEOUT_SECONDS,
    )
    wl.container.restart.assert_not_called()
    assert PEBBLE_SERVICE_TIMEOUT_SECONDS > 25


def test_single_service_start_uses_the_long_timeout(mocker):
    wl = _workload(mocker)
    mocker.patch.object(ValkeyK8sWorkload, "alive", return_value=True)

    wl.start("valkey")

    wl.container.pebble.start_services.assert_called_once_with(
        ["valkey"], timeout=PEBBLE_SERVICE_TIMEOUT_SECONDS
    )
    wl.container.start.assert_not_called()


def test_single_service_restart_uses_the_long_timeout(mocker):
    wl = _workload(mocker)

    wl.restart("valkey")

    wl.container.pebble.restart_services.assert_called_once_with(
        ["valkey"], timeout=PEBBLE_SERVICE_TIMEOUT_SECONDS
    )
    wl.container.restart.assert_not_called()
