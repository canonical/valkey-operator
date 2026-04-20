# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Topology observer class for checking changes in Primary/Replica topology."""

import subprocess
import sys

def dispatch(unit, charm_dir, custom_event) -> None:
    """Dispatch a Juju custom event."""
    juju_run_command = "/usr/bin/juju-exec"
    dispatch_command = f"JUJU_DISPATCH_PATH=hooks/{custom_event} {charm_dir}/dispatch"

    subprocess.run([juju_run_command, "-u", unit, dispatch_command])


def callback() -> None:
    """Handle received event messages and trigger a Juju event."""
    pass


def main() -> None:
    """Start a Valkey client and subscribe to Sentinel event messages."""
    valkey_hosts, username, password, tls, tls_ca_cert_file, unit_name, charm_dir = sys.argv[1:]


if __name__ == "__main__":
    main()
